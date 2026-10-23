"""Bridge completed captures into the project-scoped knowledge store.

Builds redacted, bounded source packets from transcripts or legacy notes,
resolves project identity and privacy, and imports at most once per capture.
The post-capture seam and durable scan live here; capture itself never waits
on the knowledge store.
"""
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..adapters import Transcript
from ..config import Config
from ..knowledge.models import CaptureIdentity, SourceInput
from ..knowledge.policy import canonical, sha_text
from ..knowledge.store import KnowledgeStore, import_capture
from ..projects import load_map
from ..redact import redact

_PACKET_LIMIT = 32 * 1024
_EXCERPT_LIMIT = 6000
_FORMAT_VERSION = "v1"
_REDACTOR_VERSION = "agentmem-v1"


class KnowledgeImportError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _identity_key(agent: str, session_id: str, part: int) -> str:
    return hashlib.sha256(json.dumps([agent, session_id, part], separators=(",", ":")).encode("utf-8")).hexdigest()


def _speaker(event) -> str:
    """Speaker classification per 04 heuristics."""
    if event.role == "assistant":
        return "assistant"
    if event.role != "user":
        return "unknown"
    text = event.text
    # pasted heuristics: fenced/quoted blocks or very long single messages
    if text.startswith(("```", "> ")) or len(text) > 1500:
        return "pasted"
    return "user"


def _truncate_excerpt(text: str) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= _EXCERPT_LIMIT:
        return text
    return raw[:_EXCERPT_LIMIT].decode("utf-8", errors="ignore")


def _packet_dict(event, *, agent: str, session_id: str, part: int, note_path: str,
                 repo_head: str | None, origin_group: str, recorded_at: str,
                 privacy: str, ordinal: int, excerpt: str | None = None) -> dict:
    original = event.text
    excerpt = redact(original) if excerpt is None else excerpt
    excerpt = _truncate_excerpt(excerpt)
    cursor_kind = "messages" if agent == "opencode" else "bytes"
    locator = {
        "format_version": _FORMAT_VERSION,
        "provider_record_type": "message",
        "event_ordinal": event.ordinal,
        "message_ids": [event.message_id] if event.message_id else [],
        "redactor_version": _REDACTOR_VERSION,
        "truncated": False,
    }
    return {
        "agent": agent,
        "session_id": session_id,
        "part": part,
        "kind": "transcript_span",
        "cursor_kind": cursor_kind,
        "start": event.cursor_start,
        "end": event.cursor_end,
        "span_ordinal": ordinal,
        "locator_json": locator,
        "original_range_sha": sha_text(original),
        "note_path": note_path,
        "content_sha": sha_text(excerpt),
        "recorded_at": recorded_at,
        "occurred_at": event.ts,
        "occurred_precision": "instant" if event.ts else "unknown",
        "excerpt": excerpt,
        "origin_group": origin_group,
        "privacy": privacy,
        "speaker": _speaker(event),
        "repo_head": repo_head,
    }


def _prioritized_text_events(events) -> list:
    """User prompts and final assistant answers, first and last turns first."""
    candidates = []
    for ev in events:
        if ev.role == "user" and ev.kind == "text":
            candidates.append(ev)
        elif ev.role == "assistant" and ev.kind == "text" and ev.final:
            candidates.append(ev)
    n = len(candidates)
    if n <= 2:
        return candidates
    # Treat consecutive pairs as turns: keep first turn, last turn, then middle.
    order = [0, 1, n - 2, n - 1]
    for i in range(2, n - 2):
        order.append(i)
    seen, result = set(), []
    for i in order:
        if i not in seen:
            seen.add(i)
            result.append(candidates[i])
    return result


def build_receipt_packets(t: Transcript, repo_head: str | None = None) -> list[dict]:
    """Build capped redacted source packets for storage in a capture receipt.

    No raw transcript is persisted; only redacted excerpts and original-range
    hashes are kept. Tool-only events are skipped.
    """
    events = _prioritized_text_events(t.events)
    packets = []
    total = 0
    recorded_at = _utc_now()
    truncated = False
    for ordinal, ev in enumerate(events):
        excerpt = redact(ev.text)
        excerpt_bytes = len(excerpt.encode("utf-8"))
        if excerpt_bytes > _EXCERPT_LIMIT:
            excerpt = _truncate_excerpt(excerpt)
            excerpt_bytes = len(excerpt.encode("utf-8"))
            truncated = True
        if packets and total + excerpt_bytes > _PACKET_LIMIT:
            truncated = True
            break
        total += excerpt_bytes
        packets.append(_packet_dict(ev, agent=t.agent, session_id=t.session_id, part=1,
                                    note_path="", repo_head=repo_head, origin_group="",
                                    recorded_at=recorded_at, privacy="model_allowed",
                                    ordinal=ordinal, excerpt=excerpt))
    if packets and truncated:
        packets[-1]["locator_json"]["truncated"] = True
    return packets


def _legacy_source(note_text: str, note_path: str, agent: str, session_id: str, part: int,
                   repo_head: str | None, origin_group: str, recorded_at: str,
                   privacy: str) -> dict:
    excerpt = redact(note_text)
    excerpt = _truncate_excerpt(excerpt)
    lines = note_text.count("\n") + 1
    locator = {
        "format_version": _FORMAT_VERSION,
        "provider_record_type": "legacy_note",
        "event_ordinal": 0,
        "message_ids": [],
        "redactor_version": _REDACTOR_VERSION,
        "truncated": len(note_text.encode("utf-8")) > _EXCERPT_LIMIT,
    }
    return {
        "agent": agent,
        "session_id": session_id,
        "part": part,
        "kind": "legacy_note",
        "cursor_kind": "note_lines",
        "start": 0,
        "end": lines,
        "span_ordinal": 0,
        "locator_json": locator,
        "original_range_sha": sha_text(note_text),
        "note_path": note_path,
        "content_sha": sha_text(excerpt),
        "recorded_at": recorded_at,
        "occurred_at": None,
        "occurred_precision": "unknown",
        "excerpt": excerpt,
        "origin_group": origin_group,
        "privacy": privacy,
        "speaker": "unknown",
        "repo_head": repo_head,
    }


def capture_identity_from_receipt(receipt: dict) -> CaptureIdentity:
    return CaptureIdentity(receipt["agent"], receipt["session_id"], receipt["part"])


def capture_fingerprint_from_receipt(receipt: dict) -> str:
    return receipt["digest_sha"]


def _project_id_for(cfg: Config, project_name: str) -> str | None:
    return cfg.knowledge_project_ids.get(project_name)


def _privacy_for(cfg: Config, project_name: str) -> str | None:
    return cfg.project_privacy.get(project_name)


def _ensure_project(store: KnowledgeStore, cfg: Config, project_name: str) -> str | None:
    """Return project_id if enrolled, registering the project on first import."""
    pid = _project_id_for(cfg, project_name)
    if pid is None:
        return None
    privacy = _privacy_for(cfg, project_name)
    if privacy is None:
        return None
    try:
        store.get_project(pid)
    except Exception as exc:
        if getattr(exc, "code", "") != "project_not_found":
            raise
        data = load_map(cfg.projects_map)
        roots = []
        for proj in data.get("projects", []):
            if proj["name"] == project_name:
                roots = proj.get("roots", [])
                break
        store.register_project(pid, project_name, roots, privacy=privacy)
    return pid


def _sources_from_receipt(receipt: dict, privacy: str) -> list[SourceInput]:
    packets = receipt.get("sources") or []
    recorded_at = receipt.get("when") or _utc_now()
    if not packets:
        packets = [_legacy_source(
            receipt["text"], receipt["note_path"], receipt["agent"],
            receipt["session_id"], receipt["part"], receipt.get("repo_head"),
            receipt["digest_sha"], recorded_at, privacy)]
    out = []
    for p in packets:
        p = dict(p)
        p["privacy"] = privacy
        if not p.get("note_path"):
            p["note_path"] = receipt["note_path"]
        if not p.get("origin_group"):
            p["origin_group"] = receipt["digest_sha"]
        if p.get("recorded_at") is None:
            p["recorded_at"] = recorded_at
        out.append(SourceInput(**p))
    return out


@dataclass(frozen=True)
class ImportOutcome:
    status: str  # imported | skipped | error
    project_id: str | None = None
    import_result: object = None
    reason: str = ""


def import_capture_for_receipt(cfg: Config, store: KnowledgeStore, receipt: dict) -> ImportOutcome:
    """Import one completed capture into the knowledge store under policy."""
    project_name = receipt.get("project")
    if not project_name or project_name == "Unsorted":
        return ImportOutcome("skipped", reason="no_project")
    pid = _ensure_project(store, cfg, project_name)
    if pid is None:
        return ImportOutcome("skipped", reason="not_enrolled")
    privacy = _privacy_for(cfg, project_name)
    if privacy is None:
        return ImportOutcome("skipped", reason="privacy_not_set")
    if privacy == "excluded":
        return ImportOutcome("skipped", project_id=pid, reason="project_excluded")
    sources = _sources_from_receipt(receipt, privacy)
    identity = capture_identity_from_receipt(receipt)
    fingerprint = capture_fingerprint_from_receipt(receipt)
    result = import_capture(store, pid, identity, fingerprint, sources)
    return ImportOutcome("imported", project_id=pid, import_result=result)


def _store_for_cfg(cfg: Config, writable: bool = True) -> KnowledgeStore:
    path = (cfg.vault / cfg.knowledge_db).resolve()
    if not path.exists():
        if not writable:
            raise KnowledgeImportError("store_unavailable")
        return KnowledgeStore.create(path)
    return KnowledgeStore(path, writable=writable)


def maybe_import_after_capture(cfg: Config, receipt: dict,
                               store: KnowledgeStore | None = None) -> ImportOutcome:
    """Post-capture seam. No effect when knowledge_mode is off."""
    if cfg.knowledge_mode == "off":
        return ImportOutcome("skipped", reason="knowledge_mode_off")
    try:
        if store is None:
            store = _store_for_cfg(cfg, writable=True)
            try:
                return import_capture_for_receipt(cfg, store, receipt)
            finally:
                store.close()
        return import_capture_for_receipt(cfg, store, receipt)
    except Exception as exc:
        return ImportOutcome("error", reason=f"{type(exc).__name__}: {exc}")


def _load_receipt(cfg: Config, agent: str, session_id: str, part: int) -> dict | None:
    path = cfg.meta / "capture-receipts" / ("{}.json".format(_identity_key(agent, session_id, part)))
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def scan_unimported_captures(cfg: Config, man, store: KnowledgeStore | None = None) -> list[ImportOutcome]:
    """Durable scan of manifest captures that are not yet knowledge imports."""
    if cfg.knowledge_mode == "off":
        return []
    own_store = store is None
    if own_store:
        store = _store_for_cfg(cfg, writable=True)
    try:
        outcomes = []
        for row in man.query_captures(limit=1000):
            receipt = _load_receipt(cfg, row["agent"], row["session_id"], row["part"])
            if receipt is None:
                # legacy note without receipt: synthesize a minimal receipt
                receipt = {
                    "agent": row["agent"], "session_id": row["session_id"], "part": row["part"],
                    "project": row["project"], "digest_sha": row["digest_sha"],
                    "note_path": row["note_path"], "text": "",
                    "when": datetime.fromtimestamp(row["captured_at"] or 0, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
                    "repo_head": None,
                }
                try:
                    receipt["text"] = (cfg.vault / row["note_path"]).read_text(encoding="utf-8")
                except OSError:
                    continue
            outcome = import_capture_for_receipt(cfg, store, receipt)
            outcomes.append(outcome)
        return outcomes
    finally:
        if own_store:
            store.close()
