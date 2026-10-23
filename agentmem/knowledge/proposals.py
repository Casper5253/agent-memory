"""Model-facing proposal pipeline: build input, call an isolated extractor, validate,
persist, and apply. All authority stays in the store; this module only bridges.
"""
import json
from pathlib import Path

from . import policy as p
from .models import ApplyResult, KnowledgeError, ValidationError
from .store import KnowledgeStore

_MAX_OUTPUT_BYTES = 32 * 1024
_DEFAULT_INPUT_UNITS = 12000
_BYTES_PER_UNIT = 3  # conservative offline fallback count


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


def build_proposal_input(store: KnowledgeStore, source_id: str, max_input_units: int = _DEFAULT_INPUT_UNITS) -> dict:
    """Read-only snapshot of one source group plus up to 20 relevant existing claims."""
    row = store.db.execute("SELECT project_id FROM sources WHERE id=?", (source_id,)).fetchone()
    if row is None:
        raise KnowledgeError("source_scope")
    project_id = row["project_id"]
    sources = store._input_sources(project_id, source_id)
    source_list = []
    for sid in sorted(sources):
        s = sources[sid]
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
    payload = {"sources": source_list, "existing_claims": existing}
    # Trim claims if the serialized input would exceed the conservative byte budget.
    while existing and len((_prompt_text() + json.dumps(payload, ensure_ascii=False) +
                            json.dumps(_output_schema(), ensure_ascii=False)).encode("utf-8")) > budget_bytes:
        existing.pop()
        payload = {"sources": source_list, "existing_claims": existing}
    return {
        "project_id": project_id,
        "source_id": source_id,
        "sources": source_list,
        "existing_claims": existing,
        "base_revision": store.get_project(project_id)["revision"],
        "input_sha": input_sha,
        "max_input_units": max_input_units,
    }


def serialize_model_input(input_dict: dict) -> str:
    """Serialize prompt + data + service-owned output schema for the extractor."""
    payload = {"sources": input_dict["sources"], "existing_claims": input_dict["existing_claims"]}
    return (
        _prompt_text() + "\n\n"
        "EXTRACTION INPUT:\n" + json.dumps(payload, ensure_ascii=False, indent=1) + "\n\n"
        "OUTPUT SCHEMA (return only this shape):\n" + json.dumps(_output_schema(), ensure_ascii=False, indent=2)
    )


def extract_and_apply(store_path: str, source_id: str, model_callable, *,
                      max_input_units: int = _DEFAULT_INPUT_UNITS,
                      max_output_bytes: int = _MAX_OUTPUT_BYTES,
                      extractor_version: str = "v1", prompt_version: str = "v1") -> ApplyResult:
    """Isolated model call outside DB locks, then persist and apply as reported."""
    path = Path(store_path).resolve()
    with KnowledgeStore(path) as reader:
        input_dict = build_proposal_input(reader, source_id, max_input_units=max_input_units)
        project_id = input_dict["project_id"]
        if not _privacy_ok(reader, project_id, source_id):
            raise KnowledgeError("model_disallowed")
    prompt = serialize_model_input(input_dict)
    raw_output = model_callable(prompt)
    if not isinstance(raw_output, str):
        raise ValidationError("invalid_model_output")
    raw_bytes = raw_output.encode("utf-8")
    if len(raw_bytes) > max_output_bytes:
        raise ValidationError("proposal_limit")
    with KnowledgeStore(path, writable=True) as writer:
        base_revision = input_dict["base_revision"]
        # Re-check privacy and revision inside the write lock.
        current = writer.get_project(project_id)["revision"]
        if current != base_revision:
            raise KnowledgeError("revision_conflict")
        if not _privacy_ok(writer, project_id, source_id):
            raise KnowledgeError("model_disallowed")
        proposal_id = writer.save_proposal(project_id, source_id, raw_output, base_revision,
                                           extractor_version=extractor_version, prompt_version=prompt_version,
                                           input_sha=input_dict["input_sha"])
        return writer.apply_proposal(project_id, proposal_id, base_revision, proposal_id)


def process_pending_extract_jobs(store_path: str, model_callable, *, limit: int = 10,
                                 max_input_units: int = _DEFAULT_INPUT_UNITS) -> list:
    """Durable extraction runner for queued jobs. Returns one result per processed job."""
    path = Path(store_path).resolve()
    results = []
    with KnowledgeStore(path, writable=True) as store:
        rows = store.db.execute(
            "SELECT id, project_id, source_id FROM jobs WHERE kind='extract' AND state='pending' "
            "ORDER BY next_try_at NULLS LAST, attempts LIMIT ?", (limit,)).fetchall()
        jobs = [(r["id"], r["project_id"], r["source_id"]) for r in rows]
    for job_id, project_id, source_id in jobs:
        try:
            result = extract_and_apply(str(path), source_id, model_callable, max_input_units=max_input_units)
            with KnowledgeStore(path, writable=True) as store:
                store.db.execute("UPDATE jobs SET state='done', proposal_id=? WHERE id=?",
                                 (result.operation_id, job_id))
            results.append({"job_id": job_id, "status": "applied", "result": result})
        except KnowledgeError as exc:
            with KnowledgeStore(path, writable=True) as store:
                store.db.execute("UPDATE jobs SET state='error', error_code=? WHERE id=?", (exc.code, job_id))
            results.append({"job_id": job_id, "status": "error", "error_code": exc.code})
        except ValidationError as exc:
            with KnowledgeStore(path, writable=True) as store:
                store.db.execute("UPDATE jobs SET state='error', error_code=? WHERE id=?", (exc.code, job_id))
            results.append({"job_id": job_id, "status": "error", "error_code": exc.code})
    return results
