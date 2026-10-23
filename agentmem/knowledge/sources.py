"""Bridge completed captures into the project-scoped knowledge store.

Builds redacted, bounded source packets from transcripts or legacy notes,
resolves project identity and privacy, and imports at most once per capture.
The post-capture seam catches store errors after immutable note publication.
"""
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..adapters import Transcript
from ..config import Config
from ..knowledge.models import CaptureIdentity, SourceInput
from ..knowledge.policy import canonical, sha_text
from ..knowledge.store import KnowledgeStore, import_capture
from ..projects import load_map
from ..redact import redact
from ..logutil import log

_PACKET_LIMIT = 32 * 1024
_EXCERPT_LIMIT = 6000
_FORMAT_VERSION = "v1"
_REDACTOR_VERSION = "agentmem-v1"
_CLAUDE_PASTE = re.compile(r'<pasted_content id="([^"<>\r\n]+)">(.*?)</pasted_content id="\1">', re.S)


class KnowledgeImportError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _identity_key(agent: str, session_id: str, part: int) -> str:
    return hashlib.sha256(json.dumps([agent, session_id, part], separators=(",", ":")).encode("utf-8")).hexdigest()


def _speaker(event, agent: str = "") -> str:
    """Speaker classification per 04 heuristics."""
    if event.role == "assistant":
        return "assistant"
    if event.role != "user":
        return "unknown"
    if agent == "claude":
        return "user"
    text = event.text
    # pasted heuristics: fenced/quoted blocks or very long single messages
    if re.search(r"```|~~~|(?m:^\s*>\s?)", text) or len(text) > 1500:
        return "pasted"
    return "user"


def _text_spans(event, agent: str) -> list[tuple[str, str]]:
    if agent != "claude" or event.role != "user":
        return [(event.text, _speaker(event, agent))]
    spans, end = [], 0
    for match in _CLAUDE_PASTE.finditer(event.text):
        if match.start() > end:
            spans.append((event.text[end:match.start()], "user"))
        spans.append((match.group(2), "pasted"))
        end = match.end()
    if end < len(event.text):
        spans.append((event.text[end:], "user"))
    return spans


def _truncate_excerpt(text: str) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= _EXCERPT_LIMIT:
        return text
    return raw[:_EXCERPT_LIMIT].decode("utf-8", errors="ignore")


def _packet_dict(event, *, agent: str, session_id: str, part: int, note_path: str,
                 repo_head: str | None, origin_group: str, recorded_at: str,
                 privacy: str, ordinal: int, excerpt: str | None = None,
                 original_sha: str | None = None, speaker: str | None = None) -> dict:
    original = event.text
    excerpt = redact(original) if excerpt is None else excerpt
    truncated = len(excerpt.encode("utf-8")) > _EXCERPT_LIMIT
    excerpt = _truncate_excerpt(excerpt)
    cursor_kind = "messages" if agent == "opencode" else "bytes"
    locator = {
        "format_version": _FORMAT_VERSION,
        "provider_record_type": "message",
        "event_ordinal": event.ordinal,
        "message_ids": [event.message_id] if event.message_id else [],
        "redactor_version": _REDACTOR_VERSION,
        "truncated": truncated,
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
        "original_range_sha": original_sha,
        "note_path": note_path,
        "content_sha": sha_text(excerpt),
        "recorded_at": recorded_at,
        "occurred_at": event.ts or None,
        "occurred_precision": "instant" if event.ts else "unknown",
        "excerpt": excerpt,
        "origin_group": origin_group,
        "privacy": privacy,
        "speaker": speaker or _speaker(event, agent),
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


def _cap_packets(packets: list[dict]) -> list[dict]:
    selected = []
    for packet in packets:
        if len(canonical(selected + [packet]).encode("utf-8")) > _PACKET_LIMIT:
            if selected:
                selected[-1]["locator_json"]["truncated"] = True
            break
        selected.append(packet)
    return selected


def build_receipt_packets(t: Transcript, repo_head: str | None = None, *, part: int = 1,
                          note_path: str = "", origin_group: str = "", privacy: str = "model_allowed",
                          original_messages: list | None = None) -> list[dict]:
    """Build capped redacted source packets for storage in a capture receipt.

    No raw transcript is persisted; only redacted excerpts and original-range
    hashes are kept. Tool-only events are skipped.
    """
    events = _prioritized_text_events(t.events)
    packets = []
    recorded_at = _utc_now()
    original_bytes = None
    if t.agent != "opencode" and Path(t.source).is_file():
        original_bytes = Path(t.source).read_bytes()
    spans = {}
    ordinal = 0
    for event in t.events:
        spans[id(event)] = []
        for text, speaker in _text_spans(event, t.agent):
            spans[id(event)].append((ordinal, text, speaker))
            ordinal += 1
    for ev in events:
        original_sha = None
        if original_bytes is not None:
            original_sha = hashlib.sha256(original_bytes[ev.cursor_start:ev.cursor_end]).hexdigest()
        elif original_messages is not None:
            original_sha = sha_text(canonical(original_messages[ev.cursor_start:ev.cursor_end]))
        for ordinal, text, speaker in spans[id(ev)]:
            packet = _packet_dict(ev, agent=t.agent, session_id=t.session_id, part=part,
                                  note_path=note_path, repo_head=repo_head, origin_group=origin_group,
                                  recorded_at=recorded_at, privacy=privacy,
                                  ordinal=ordinal, excerpt=redact(text), original_sha=original_sha, speaker=speaker)
            if t.agent == "opencode" and not ev.message_id:
                packet["locator_json"]["format_version"] = "v1-missing-message-ids"
            packets.append(packet)
    selected = sorted(_cap_packets(packets), key=lambda packet: packet["span_ordinal"])
    if selected and len(selected) < len(packets):
        selected[-1]["locator_json"]["truncated"] = True
    return selected


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
    return receipt["content_sha"]


def _project_id_for(cfg: Config, project_name: str) -> str | None:
    return cfg.knowledge_project_ids.get(project_name)


def _privacy_for(cfg: Config, project_name: str) -> str | None:
    pid = _project_id_for(cfg, project_name)
    return cfg.project_privacy.get(pid, cfg.project_privacy.get(project_name))


def _ensure_project(store: KnowledgeStore, cfg: Config, project_name: str) -> str | None:
    """Return project_id if enrolled, registering the project on first import.
    
    Also updates stored privacy when config privacy differs from stored.
    """
    pid = _project_id_for(cfg, project_name)
    if pid is None:
        return None
    privacy = _privacy_for(cfg, project_name)
    if privacy is None:
        return None
    try:
        project = store.get_project(pid)
        # Update privacy if config changed
        if project["privacy"] != privacy:
            store.update_project_privacy(pid, privacy)
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
    recorded = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
    if recorded.tzinfo is None:
        recorded = recorded.replace(tzinfo=timezone.utc)
    recorded_at = recorded.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if not packets:
        packets = [_legacy_source(
            receipt["text"], receipt["note_path"], receipt["agent"],
            receipt["session_id"], receipt["part"], receipt.get("repo_head"),
            receipt["digest_sha"], recorded_at, privacy)]
    out = []
    for p in packets:
        p = dict(p)
        p["locator_json"] = dict(p["locator_json"])
        p["privacy"] = privacy
        if not p.get("note_path"):
            p["note_path"] = receipt["note_path"]
        if not p.get("origin_group"):
            p["origin_group"] = receipt["digest_sha"]
        if p.get("recorded_at") is None:
            p["recorded_at"] = recorded_at
        out.append(SourceInput(**p))
    return [SourceInput(**packet) for packet in _cap_packets([asdict(source) for source in out])]


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
    if _privacy_for(cfg, project_name) == "excluded":
        return ImportOutcome("skipped", project_id=_project_id_for(cfg, project_name), reason="project_excluded")
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
    from ..pipeline import _load_receipt as validated_receipt
    raw = json.loads(path.read_text(encoding="utf-8"))
    return validated_receipt(cfg, agent, session_id, part, raw["start"])


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
            if _privacy_for(cfg, row["project"]) == "excluded":
                outcomes.append(ImportOutcome("skipped", reason="project_excluded"))
                continue
            try:
                receipt = _load_receipt(cfg, row["agent"], row["session_id"], row["part"])
                from ..pipeline import _validated_path
                path = _validated_path(cfg, row["note_path"])
                persisted = path.read_text(encoding="utf-8")
                from ..pipeline import _validate_pending_identity
                _validate_pending_identity(cfg, man, row, persisted, json.loads(row["summary_json"] or "{}"))
                if receipt is not None:
                    binding = (receipt["project"], receipt["note_path"], receipt["start"], receipt["end"], receipt["digest_sha"])
                    expected = (row["project"], row["note_path"], row["start_offset"], row["end_offset"], row["digest_sha"])
                    if binding != expected or sha_text(persisted) != receipt["content_sha"]:
                        raise ValueError("capture_identity_conflict")
            except (OSError, ValueError, KeyError, TypeError):
                log(cfg, "knowledge-import-skip", agent=row["agent"], session=row["session_id"],
                    part=row["part"], reason="invalid_receipt")
                outcomes.append(ImportOutcome("skipped", reason="invalid_receipt"))
                continue
            if receipt is None:
                # legacy note without receipt: synthesize a minimal receipt
                receipt = {
                    "agent": row["agent"], "session_id": row["session_id"], "part": row["part"],
                    "project": row["project"], "digest_sha": row["digest_sha"],
                    "note_path": row["note_path"], "text": "",
                    "when": datetime.fromtimestamp(row["captured_at"] or 0, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
                    "repo_head": None,
                }
                receipt["text"] = persisted
                receipt["content_sha"] = sha_text(persisted)
            try:
                outcome = import_capture_for_receipt(cfg, store, receipt)
            except Exception:
                log(cfg, "knowledge-import-skip", agent=row["agent"], session=row["session_id"],
                    part=row["part"], reason="import_failed")
                outcome = ImportOutcome("error", reason="import_failed")
            outcomes.append(outcome)
        return outcomes
    finally:
        if own_store:
            store.close()
