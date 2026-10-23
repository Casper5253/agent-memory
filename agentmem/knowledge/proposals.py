"""Model-facing proposal pipeline: build input, call an isolated extractor, validate,
persist, and apply. All authority stays in the store; this module only bridges.
"""
import json
import queue
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import Config
from . import policy as p
from .models import ApplyResult, KnowledgeError, ValidationError
from .store import KnowledgeStore

_MAX_OUTPUT_BYTES = 32 * 1024
_DEFAULT_INPUT_UNITS = 12000
_BYTES_PER_UNIT = 3  # conservative offline fallback count
_LEASE_SECONDS = 240
_MODEL_TIMEOUT = 180


@dataclass(frozen=True)
class ExtractionLease:
    job_id: str
    token: str
    until: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _after(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _check_lease(store, lease):
    row = store.db.execute("SELECT * FROM jobs WHERE id=?", (lease.job_id,)).fetchone()
    now = _now()
    if (row is None or row["state"] != "running" or row["lease_token"] != lease.token
            or not row["lease_until"] or row["lease_until"] <= now or lease.until <= now):
        raise KnowledgeError("lease_lost")


class _LeasedStore(KnowledgeStore):
    """Check ownership inside the store's save/apply transactions, before commit."""

    def __init__(self, path, lease):
        self.lease = lease
        self.completed = None
        super().__init__(path, writable=True)

    @contextmanager
    def _tx(self):
        with super()._tx():
            _check_lease(self, self.lease)
            self.completed = None
            yield
            _check_lease(self, self.lease)
            if self.completed is not None:
                self.db.execute("UPDATE jobs SET state='done',proposal_id=?,error_code=NULL WHERE id=?",
                                (self.completed.operation_id, self.lease.job_id))

    def _finish(self, result, *args, **kwargs):
        super()._finish(result, *args, **kwargs)
        self.completed = result


def _claim_lease(path, source_id, job_id=None):
    with KnowledgeStore(path, writable=True) as store:
        with store._tx():
            now = _now()
            rows = store.db.execute("SELECT id,attempts FROM jobs WHERE kind='extract' AND state='running' "
                                    "AND lease_until<=?", (now,)).fetchall()
            for row in rows:
                store.db.execute("UPDATE jobs SET state=?,lease_token=NULL,lease_until=NULL,error_code='lease_expired' WHERE id=?",
                                 ("pending" if row["attempts"] < 3 else "quarantined", row["id"]))
            sql = "SELECT * FROM jobs WHERE kind='extract' AND source_id=? AND state='pending' AND (next_try_at IS NULL OR next_try_at<=?)"
            args = [source_id, now]
            if job_id is not None:
                sql += " AND id=?"
                args.append(job_id)
            row = store.db.execute(sql + " ORDER BY attempts,id LIMIT 1", args).fetchone()
            attempts = store.db.execute("SELECT coalesce(sum(attempts),0) FROM jobs WHERE kind='extract' AND source_id=?",
                                        (source_id,)).fetchone()[0]
            if row is None:
                if attempts >= 3:
                    return None
                raise KnowledgeError("job_unavailable")
            if attempts >= 3:
                store.db.execute("UPDATE jobs SET state='quarantined',error_code='attempt_limit' WHERE id=?", (row["id"],))
                return None
            lease = ExtractionLease(row["id"], uuid.uuid4().hex, _after(_LEASE_SECONDS))
            store.db.execute("UPDATE jobs SET state='running',attempts=attempts+1,lease_token=?,lease_until=?,error_code=NULL WHERE id=?",
                             (lease.token, lease.until, lease.job_id))
            return lease


def _record_failure(path, lease, code):
    with KnowledgeStore(path, writable=True) as store:
        with store._tx():
            row = store.db.execute("SELECT * FROM jobs WHERE id=?", (lease.job_id,)).fetchone()
            try:
                _check_lease(store, lease)
            except KnowledgeError:
                return
            attempts = store.db.execute("SELECT coalesce(sum(attempts),0) FROM jobs WHERE kind='extract' AND source_id=?",
                                        (row["source_id"],)).fetchone()[0]
            retry = code in ("model_timeout", "model_unavailable", "revision_conflict", "store_busy")
            state = "quarantined" if attempts >= 3 else "pending" if retry else "error"
            next_try = _after(60 if attempts == 1 else 300) if state == "pending" else None
            store.db.execute("UPDATE jobs SET state=?,next_try_at=?,lease_token=NULL,lease_until=NULL,error_code=? WHERE id=?",
                             (state, next_try, code, lease.job_id))


def _call_model(model, prompt, timeout_s):
    if timeout_s <= 0:
        raise KnowledgeError("model_timeout")
    result = queue.Queue(maxsize=1)
    def invoke():
        try:
            result.put((True, model(prompt)))
        except Exception as error:
            result.put((False, error))
    threading.Thread(target=invoke, daemon=True).start()
    try:
        ok, value = result.get(timeout=timeout_s)
    except queue.Empty:
        raise KnowledgeError("model_timeout") from None
    if not ok:
        if isinstance(value, (TimeoutError, subprocess.TimeoutExpired)):
            raise KnowledgeError("model_timeout") from None
        raise KnowledgeError("model_unavailable") from value
    return value


def _prompt_text() -> str:
    path = Path(__file__).with_suffix("").parent / "prompts" / "extract_v1.txt"
    return path.read_text(encoding="utf-8")


def _output_schema() -> dict:
    """Service-owned output schema from 04a; appended to every model prompt."""
    return {
        "schema_version": 1,
        "entities": [
            {"ref": "e1", "kind": "component", "canonical_key": "storage", "display_name": "Storage", "aliases": []}
        ],
        "claims": [
            {"ref": "c1", "subject_ref": "e1", "kind": "decision", "predicate": "storage_backend", "value": "sqlite",
             "statement": "New work uses SQLite.", "rationale": "Atomic cursor updates.",
             "valid_from": None, "valid_to": None, "valid_precision": "unknown", "valid_end_state": "unknown",
             "evidence": [{"source_id": "<source-id>", "start": 0, "end": 42, "role": "supports"}]}
        ],
        "relations": [
            {"from": {"ref": "c1"}, "kind": "contradicts",
             "to": {"claim_id": "<existing-claim-id>", "expected_revision": 1}, "rationale": "Same slot, different value."}
        ],
    }


def _privacy_ok(store: KnowledgeStore, project_id: str, source_id: str) -> bool:
    project = store.get_project(project_id)
    if project["privacy"] != "model_allowed":
        return False
    sources = store._input_sources(project_id, source_id)
    return all(source["privacy"] == "model_allowed" for source in sources.values())


def _check_config_privacy(cfg: Config, project_id: str) -> None:
    names = [name for name, pid in cfg.knowledge_project_ids.items() if pid == project_id]
    if not names or any(cfg.project_privacy.get(name) != "model_allowed" for name in names):
        raise KnowledgeError("model_disallowed")


def build_proposal_input(store: KnowledgeStore, source_id: str, max_input_units: int = _DEFAULT_INPUT_UNITS) -> dict:
    """Read-only snapshot of one source group plus up to 20 relevant existing claims."""
    row = store.db.execute("SELECT project_id FROM sources WHERE id=?", (source_id,)).fetchone()
    if row is None:
        raise KnowledgeError("source_scope")
    project_id = row["project_id"]
    sources = store._input_sources(project_id, source_id)
    source_list = []
    for sid, s in sources.items():
        source_list.append({
            "source_id": sid,
            "excerpt": s["excerpt"],
            "speaker": s["speaker"],
            "cursor_kind": s["cursor_kind"],
            "start": s["start"],
            "end": s["end"],
            "span_ordinal": s["span_ordinal"],
            "truncated": (json.loads(s["locator_json"]) if isinstance(s["locator_json"], str) else s["locator_json"]).get("truncated", False),
        })
    input_sha = p.identity([[sid, s["content_sha"]] for sid, s in sources.items()])
    budget_bytes = max_input_units * _BYTES_PER_UNIT
    claim_rows = store.db.execute(
        "SELECT id, kind, predicate, value_json, statement, status, evidence_level, recorded_at "
        "FROM claims WHERE project_id=? AND status IN ('reported','accepted','disputed') "
        "ORDER BY recorded_at DESC LIMIT 20", (project_id,)).fetchall()
    existing = []
    for c in claim_rows:
        item = {
            "claim_id": c["id"],
            "kind": c["kind"],
            "predicate": c["predicate"],
            "value": json.loads(c["value_json"]),
            "statement": c["statement"],
            "status": c["status"],
            "evidence_level": c["evidence_level"],
            "recorded_at": c["recorded_at"],
        }
        existing.append(item)
    result = {
        "project_id": project_id,
        "source_id": source_id,
        "sources": source_list,
        "existing_claims": existing,
        "base_revision": store.get_project(project_id)["revision"],
        "input_sha": input_sha,
        "max_input_units": max_input_units,
    }
    def size():
        return len(serialize_model_input(result).encode("utf-8"))
    while existing and size() > budget_bytes:
        existing.pop()
    while size() > budget_bytes:
        longest = max(source_list, key=lambda source: len(source["excerpt"]), default=None)
        if longest is None or not longest["excerpt"]:
            raise KnowledgeError("input_limit")
        excess = size() - budget_bytes
        longest["excerpt"] = longest["excerpt"][:-max(1, min(len(longest["excerpt"]), excess))]
        longest["truncated"] = True
    return result


def serialize_model_input(input_dict: dict) -> str:
    """Serialize prompt + data + service-owned output schema for the extractor."""
    payload = {"sources": input_dict["sources"], "existing_claims": input_dict["existing_claims"]}
    return (
        _prompt_text() + "\n\n"
        "EXTRACTION INPUT:\n" + json.dumps(payload, ensure_ascii=False, indent=1) + "\n\n"
        "OUTPUT SCHEMA (return only this shape):\n" + json.dumps(_output_schema(), ensure_ascii=False, indent=2)
    )


def extract_and_apply(store_path: str, source_id: str, model_callable, *,
                      cfg: Config,
                      max_input_units: int = _DEFAULT_INPUT_UNITS,
                      max_output_bytes: int = _MAX_OUTPUT_BYTES,
                      extractor_version: str = "v1", prompt_version: str = "v1",
                      timeout_s: float = _MODEL_TIMEOUT, _lease: ExtractionLease | None = None) -> ApplyResult:
    """Check current privacy, call the model outside DB locks, then apply as reported."""
    path = Path(store_path).resolve()
    with KnowledgeStore(path) as reader:
        input_dict = build_proposal_input(reader, source_id, max_input_units=max_input_units)
        project_id = input_dict["project_id"]
        if not _privacy_ok(reader, project_id, source_id):
            raise KnowledgeError("model_disallowed")
    lease = _lease or _claim_lease(path, source_id)
    if lease is None:
        raise KnowledgeError("attempt_limit")
    try:
        with KnowledgeStore(path) as reader:
            _check_lease(reader, lease)
            if not _privacy_ok(reader, project_id, source_id):
                raise KnowledgeError("model_disallowed")
        prompt = serialize_model_input(input_dict)
        _check_config_privacy(cfg, project_id)
        started = time.monotonic()
        raw_output = _call_model(model_callable, prompt, min(timeout_s, _MODEL_TIMEOUT))
        if time.monotonic() - started >= min(timeout_s, _MODEL_TIMEOUT):
            raise KnowledgeError("model_timeout")
        if not isinstance(raw_output, str):
            raise ValidationError("invalid_model_output")
        if len(raw_output.encode("utf-8")) > max_output_bytes:
            raise ValidationError("proposal_limit")
        validated = p.validate_proposal(raw_output)
        allowed = {source["source_id"]: source["excerpt"] for source in input_dict["sources"]}
        for claim in validated.claims:
            for span in claim.evidence:
                if span.source_id not in allowed:
                    raise KnowledgeError("evidence_scope")
                if span.end > len(allowed[span.source_id]):
                    raise ValidationError("evidence_range")
        with _LeasedStore(path, lease) as writer:
            base_revision = input_dict["base_revision"]
            proposal_id = writer.save_proposal(project_id, source_id, raw_output, base_revision,
                                               extractor_version=extractor_version, prompt_version=prompt_version,
                                               input_sha=input_dict["input_sha"])
            return writer.apply_proposal(project_id, proposal_id, base_revision, proposal_id)
    except KnowledgeError as error:
        _record_failure(path, lease, error.code)
        raise


def process_pending_extract_jobs(store_path: str, model_callable, *, cfg: Config, limit: int = 10,
                                 max_input_units: int = _DEFAULT_INPUT_UNITS,
                                 max_calls: int = 2, budget_s: float = 300,
                                 timeout_s: float = _MODEL_TIMEOUT) -> list:
    """Durable extraction runner for queued jobs. Returns one result per processed job.
    
    max_calls counts only jobs that actually invoked the model (not blocked by privacy).
    """
    path = Path(store_path).resolve()
    results = []
    model_call_count = 0
    deadline = time.monotonic() + budget_s
    with KnowledgeStore(path, writable=True) as store:
        rows = store.db.execute(
            "SELECT id, project_id, source_id FROM jobs WHERE kind='extract' AND "
            "((state='pending' AND (next_try_at IS NULL OR next_try_at<=?)) OR (state='running' AND lease_until<=?)) "
            "ORDER BY next_try_at NULLS LAST, attempts LIMIT ?", (_now(), _now(), limit)).fetchall()
        jobs = [(r["id"], r["project_id"], r["source_id"]) for r in rows]
    for job_id, project_id, source_id in jobs:
        if model_call_count >= max_calls or time.monotonic() >= deadline:
            break
        lease = None
        try:
            lease = _claim_lease(path, source_id, job_id)
            if lease is None:
                raise KnowledgeError("attempt_limit")
            result = extract_and_apply(str(path), source_id, model_callable, max_input_units=max_input_units,
                                       timeout_s=min(timeout_s, deadline-time.monotonic()), _lease=lease, cfg=cfg)
            model_call_count += 1
            results.append({"job_id": job_id, "status": "applied", "result": result})
        except KnowledgeError as exc:
            if lease is not None:
                _record_failure(path, lease, exc.code)
            results.append({"job_id": job_id, "status": "error", "error_code": exc.code})
    return results
