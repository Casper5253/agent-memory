"""Capture pipeline: settled session -> digest -> gate -> summary -> immutable note -> reconciled project note.

Order of effects is chosen so any crash is safe to retry:
  1. a versioned receipt freezes the source range, summary, identity and note hash
  2. note is created once and its persisted bytes are checked against the receipt
  3. project note is reconciled under the project lock
  4. manifest records the capture (one transaction) -> cursor advances
A retry after a crash between 1 and 3 finds the note, re-reconciles (idempotent by session link), records.
"""
import hashlib
import json
import os
import re
import tempfile
import time
import traceback
from datetime import datetime
from pathlib import Path

from . import adapters, extract, llm, notes, projects, status
from .config import Config
from .fsutil import atomic_write, create_once, file_lock, LockTimeout
from .knowledge import sources as ksources
from .logutil import log
from .manifest import Manifest, RetryPolicy, REVIEW_HOLD
from .redact import redact, redact_session_note


class NoProgress(llm.LLMError):
    """The model answered, but nothing new was accepted. Retrying the same input cannot change that."""


class OpenCodeSourceChanging(Exception):
    """The live OpenCode source moved before capture evidence was committed."""



def _parse_time(value: str) -> datetime:
    if value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
        except ValueError:
            pass
    return datetime.now().astimezone()


def _source_for(cfg: Config, row) -> str:
    """The live transcript if it is there, else the end-hook copy, else the agent finder."""
    recorded = row["source"] or ""
    if recorded and Path(recorded).exists():
        return recorded
    snap = cfg.snapshot_path(row["agent"], row["session_id"])
    if snap.exists():
        return str(snap)
    if row["agent"] == "codex":
        return adapters.find_codex_transcript(row["session_id"]) or ""
    if row["agent"] == "opencode":
        return row["session_id"]
    if row["agent"] == "devin":
        return adapters.find_devin_transcript(row["session_id"]) or ""
    return recorded


def open_manifest(cfg: Config) -> Manifest:
    """Manifest carrying the configured retry policy: bounds for the first-run error migration
    and the transcript locator that verifies missing-source errors against real state."""
    return Manifest(cfg.manifest_path, migrate=RetryPolicy(
        max_attempts=cfg.max_attempts,
        max_age_s=cfg.error_max_age_days * 86400,
        grace_s=cfg.error_grace_hours * 3600,
        locate=lambda row: transcript_located(cfg, row)))


def _opencode_session_exists(session_id: str, db_path: "Path | None" = None) -> "bool | None":
    """Whether the OpenCode store still holds the session; None when the store is unreachable."""
    import sqlite3
    path = Path(db_path) if db_path else Path.home() / ".local" / "share" / "opencode" / "opencode.db"
    if not path.exists():
        return None
    try:
        db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            return db.execute("SELECT 1 FROM session WHERE id=?", (session_id,)).fetchone() is not None
        finally:
            db.close()
    except (sqlite3.Error, OSError):
        return None


def _epoch_seconds(value) -> float:
    return value / 1000 if value > 1e11 else float(value)


def opencode_message_activity(cfg: Config, session_id: str) -> "float | None":
    """Latest OpenCode message or part write time, or None when the store cannot prove one."""
    import sqlite3
    path = Path(cfg.opencode_db)
    if not path.exists():
        return None
    try:
        db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            latest = 0
            for table in ("message", "part"):
                row = db.execute(
                    f"SELECT max(time_created), max(time_updated) FROM {table} WHERE session_id=?",
                    (session_id,)).fetchone()
                for value in row:
                    if isinstance(value, (int, float)):
                        latest = max(latest, value)
        finally:
            db.close()
    except (sqlite3.Error, OSError):
        return None
    return _epoch_seconds(latest) if latest else None


def _opencode_retry_at(cfg: Config, session_id: str, settle_s: float, now: float) -> "tuple[float | None, str]":
    latest = opencode_message_activity(cfg, session_id)
    if latest is None:
        # No store at all can never prove quiet, so waiting would defer forever.
        if not Path(cfg.opencode_db).exists():
            return None, ""
        if _opencode_session_exists(session_id, cfg.opencode_db) is False:
            return None, ""
        return now + settle_s, "opencode message activity is unknown"
    if now - latest < settle_s:
        return latest + settle_s, "opencode messages changed inside the settle window"
    return None, ""


def transcript_located(cfg: Config, row) -> "bool | None":
    """Whether the transcript still exists anywhere the adapters look: the recorded path, the
    agent roots (Claude projects, Codex sessions and archived_sessions, Devin transcripts) or
    the OpenCode store. False means absent everywhere; None means the check could not tell."""
    agent, sid = row["agent"], row["session_id"]
    source = row["source"] or ""
    try:
        if source and agent != "opencode" and Path(source).exists():
            return True
        if cfg.snapshot_path(agent, sid).exists():
            return True
        if agent == "claude":
            return next(Path(cfg.claude_transcripts_root).glob(f"*/{sid}.jsonl"), None) is not None
        if agent == "codex":
            if adapters.find_codex_transcript(sid):
                return True
            archive = Path(cfg.codex_transcripts_root).parent / "archived_sessions"
            return next(archive.glob(f"rollout-*{sid}*.zst"), None) is not None
        if agent == "devin":
            root = cfg.devin_transcripts_root
            if root is None and adapters.devin_data_dir() is not None:
                root = adapters.devin_data_dir() / "transcripts"
            if root is not None and Path(root, f"{sid}.json").exists():
                return True
            return bool(adapters.find_devin_transcript(sid))
        if agent == "opencode":
            return _opencode_session_exists(sid)
    except OSError:
        return None
    return None


def _normalized_cwd(cwd: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.expandvars(os.path.expanduser(cwd))))


def _codex_memory_curation(cwd: str) -> bool:
    if not cwd:
        return False
    actual = _normalized_cwd(cwd)
    base = Path.home() / ".codex"
    roots = [_normalized_cwd(str(base / name)) for name in ("memories", "memories_v2")]
    return any(actual == root or actual.startswith(root + os.sep) for root in roots)


def _excluded(cfg: Config, cwd: str) -> bool:
    if not cwd:
        return False
    c = os.path.normcase(os.path.normpath(cwd))
    roots = [cfg.meta, *map(Path, cfg.excluded_roots)]
    return any(c == os.path.normcase(str(r)) or c.startswith(os.path.normcase(str(r)) + os.sep) for r in roots)


def _identity_key(agent, sid, part):
    return hashlib.sha256(json.dumps([agent, sid, part], separators=(",", ":")).encode("utf-8")).hexdigest()


def _hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _source_range_hash(transcript):
    if transcript.agent == "opencode":
        data = adapters.export_opencode(transcript.session_id)
        messages = data.get("messages") or []
        selected = messages[transcript.start:transcript.end]
        raw = json.dumps(selected, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    elif transcript.agent == "devin":
        data = json.loads(Path(transcript.source).read_text(encoding="utf-8"))
        selected = (data.get("steps") or [])[transcript.start:transcript.end]
        raw = json.dumps(selected, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    else:
        path = Path(transcript.source)
        with path.open("rb") as source:
            source.seek(transcript.start)
            raw = source.read(transcript.end - transcript.start)
    return hashlib.sha256(raw).hexdigest()


def _assert_opencode_source_unchanged(agent, sid, source, transcript) -> None:
    if agent != "opencode":
        return
    current = adapters.read(agent, sid, source)
    if current.prefix_sha != transcript.prefix_sha:
        raise OpenCodeSourceChanging("OpenCode source changed before capture commit")


def _receipt_path(cfg, agent, sid, part):
    return cfg.meta / "capture-receipts" / (_identity_key(agent, sid, part) + ".json")


def _receipt_hash(receipt):
    return _hash(json.dumps({k: v for k, v in receipt.items() if k != "receipt_sha"}, sort_keys=True, ensure_ascii=False))


def _note_project(receipt):
    # A separate note belongs to this capture, even when an older writer copied the prior project's label.
    if int(receipt.get('note_part', receipt['part'])) == int(receipt['part']):
        return receipt['project']
    return receipt.get('note_project', receipt['project'])


def _load_receipt(cfg, agent, sid, part, start):
    path = _receipt_path(cfg, agent, sid, part)
    if not path.exists():
        return None
    try:
        r = json.loads(path.read_text(encoding="utf-8"))
        if (r["version"] != 1 or (r["agent"], r["session_id"], r["part"], r["start"]) != (agent, sid, part, start)
                or r["receipt_sha"] != _receipt_hash(r) or _hash(r["text"]) != r["content_sha"]):
            raise ValueError("receipt identity or hash mismatch")
        note_part = int(r.get("note_part", part))
        if note_part < part and notes.continuation_project(r["text"], part) != r.get("project"):
            raise ValueError("capture_identity_conflict: continuation project differs from note receipt")
        _validated_path(cfg, r["note_path"])
        _validate_note(r["text"], agent, sid, note_part,
                       expected_project=_note_project(r))
        return r
    except (ValueError, KeyError, TypeError) as error:
        raise ValueError("capture_identity_conflict: invalid receipt") from error


def _validated_path(cfg, relative):
    path = (cfg.vault / relative).resolve()
    if not path.is_relative_to(cfg.sessions_dir.resolve()):
        raise ValueError("capture_identity_conflict: note outside session directory")
    return path


def _validate_note(text, agent, sid, part, expected_project=None):
    fields = notes.session_metadata(text)
    if (fields.get("agent"), fields.get("session_id"), fields.get("part")) != (agent, sid, str(part)):
        raise ValueError("capture_identity_conflict: note identity mismatch")
    if expected_project is not None and fields.get("project") != expected_project:
        raise ValueError("capture_identity_conflict: note project mismatch")
    return fields


def _legacy_evidence(cfg, agent, sid, part):
    suffix = "".join(ch for ch in sid if ch.isalnum())[-8:]
    tail = f"-p{part}" if part > 1 else ""
    matches = []
    for path in cfg.sessions_dir.glob(f"*/*/*-{suffix}{tail}.md"):
        text = path.read_text(encoding="utf-8")
        fields = notes.session_metadata(text)
        if (fields.get("agent"), fields.get("part"), fields.get("session_id")) == (agent, str(part), "[REDACTED]"):
            raise ValueError("capture_identity_conflict: legacy session identity is masked")
        if (fields.get("agent"), fields.get("session_id"), fields.get("part")) == (agent, sid, str(part)):
            matches.append((path, text, fields))
    if len(matches) > 1:
        raise ValueError("capture_identity_conflict: ambiguous legacy notes")
    return matches[0] if matches else None


def _link(cfg: Config, path: Path) -> str:
    return path.relative_to(cfg.vault).with_suffix("").as_posix()


def finalize(cfg: Config, man: Manifest, row, summarizer=None, consolidator=None) -> str:
    """Recover the frozen receipt, or prepare one before publishing immutable evidence."""
    agent, sid, part = row["agent"], row["session_id"], row["parts"] + 1
    key = _identity_key(agent, sid, part)
    with file_lock(cfg.locks_dir / f"capture-{key}.lock", timeout=cfg.lock_timeout_s):
        fresh = man.get(agent, sid)
        if fresh["parts"] != row["parts"]:
            return "already-captured"
        configured_summarizer = summarizer or (
            lambda digest, timeout: llm.summarize(digest, timeout=timeout,
                                                  email_allowlist=cfg.privacy_email_allowlist))
        return _finalize_locked(cfg, man, fresh, configured_summarizer, consolidator)


def _finalize_locked(cfg, man, row, summarizer, consolidator):
    agent, sid, part = row["agent"], row["session_id"], row["parts"] + 1
    if agent == "codex" and _codex_memory_curation(row["cwd"] or ""):
        man.mark(agent, sid, status="skipped", end_seen=0, skip_reason="internal memory curation",
                 last_error=None, deferred_start=None)
        log(cfg, "capture-skipped", agent=agent, session=sid, reason="internal memory curation")
        return "skipped-internal"
    # Content judged trivial earlier is re-judged together with what followed it.
    range_start = row["deferred_start"] if row["deferred_start"] is not None else row["captured_until"]
    receipt = _load_receipt(cfg, agent, sid, part, range_start)
    if receipt is None:
        legacy = _legacy_evidence(cfg, agent, sid, part)
        if legacy:
            path, text, fields = legacy
            summary = notes.summary_from_session(text)
            start, end = notes.source_range(text)
            if start != range_start:
                raise ValueError("capture_identity_conflict: legacy range mismatch")
            receipt = dict(version=1, agent=agent, session_id=sid, part=part, start=start, end=end,
                           project=fields["project"], summary=summary, digest_sha=_hash(text),
                           note_path=path.relative_to(cfg.vault).as_posix(), text=text, content_sha=_hash(text),
                           when=fields["date"], summarizer=fields.get("summarizer", "legacy"), cost=0,
                           prefix_sha="", evidence="legacy-note", related_projects=[])
        else:
            source = _source_for(cfg, row)
            if not source:
                raise FileNotFoundError(f"no transcript for {agent}:{sid}")
            if row["parts"] and agent == "opencode":
                previous = _load_receipt(cfg, agent, sid, part - 1, man.capture(agent, sid, part - 1)["start_offset"])
                if previous and previous.get("prefix_sha"):
                    prefix = adapters.read(agent, sid, source, end=previous.get("end", range_start))
                    if prefix.prefix_sha != previous["prefix_sha"]:
                        raise ValueError("source_change: earlier OpenCode messages mutated")
            original_messages = None
            if agent == "opencode" and cfg.knowledge_mode != "off":
                exported = adapters.export_opencode(sid)
                t = adapters.read_opencode(sid, start=range_start, data=exported)
                original_messages = exported.get("messages") or []
            else:
                t = adapters.read(agent, sid, source, start=range_start)
            source_error = _source_error(t.warnings)
            for warning in t.warnings:
                log(cfg, "capture-source-warning", agent=agent, session=sid, **warning)
            if t.session_id != sid:
                raise ValueError("capture_identity_conflict: transcript session differs")
            cwd = t.cwd or row["cwd"] or ""
            done_status = "captured" if row["parts"] else "skipped"
            if t.end <= row["captured_until"] or not t.events:
                man.mark(agent, sid, status=done_status, captured_until=max(row["captured_until"], t.end),
                         end_seen=0, skip_reason=row["skip_reason"] or "no new content",
                         last_error=source_error)
                return "no-new-content"
            if _excluded(cfg, cwd):
                man.mark(agent, sid, status="skipped", captured_until=t.end, end_seen=0, skip_reason="excluded directory",
                         deferred_start=None)
                log(cfg, "capture-skipped", agent=agent, session=sid, reason="excluded directory",
                    start=range_start, end=t.end)
                return "excluded"
            d = extract.build(t, cfg.min_user_prompts, cfg.min_assistant_chars, cfg.digest_char_cap,
                              email_allowlist=cfg.privacy_email_allowlist)
            if not d.substantial:
                man.mark(agent, sid, status=done_status, captured_until=t.end, end_seen=0,
                         skip_reason=d.gate_reason, cwd=cwd,
                         last_error=source_error, deferred_start=range_start)
                log(cfg, "capture-deferred", agent=agent, session=sid, reason=d.gate_reason,
                    start=range_start, end=t.end, parts=row["parts"])
                return "trivial"
            edited = [p for ev in t.events if ev.kind == "edit" for p in ev.paths]
            attribution = projects.attribute(cwd, cfg.projects_map, files=edited, text="\n".join(d.prompts))
            project = attribution.project
            if ksources._privacy_for(cfg, project) == "excluded":
                man.mark(agent, sid, status="skipped", captured_until=t.end, end_seen=0,
                         project=project, skip_reason="project excluded", deferred_start=None)
                log(cfg, "capture-skipped", agent=agent, session=sid, reason="project excluded",
                    start=range_start, end=t.end)
                return "excluded"
            repo_head = projects.repo_head(cwd)
            when = _parse_time(t.started)
            cost, kind, summary = 0.0, "deterministic", None
            if (cfg.summarizer == "claude-haiku"
                    and ksources._privacy_for(cfg, project) not in ("local_only", "excluded")):
                try:
                    summary = summarizer(d.text, timeout=cfg.llm_timeout_s)
                    cost = summary.pop("_cost_usd", 0.0)
                    kind = "claude-haiku"
                except llm.LLMError as error:
                    log(cfg, "llm-fallback", agent=agent, session=sid, step="summarize", error=str(error))
            if summary is None:
                summary = extract.fallback_summary(t, d)
            summary = _redact_values(summary, cfg.privacy_email_allowlist)
            summary["repo_head"] = repo_head
            source_hash = _source_range_hash(t)
            previous = man.capture(agent, sid, part - 1) if part > 1 else None
            previous_path = _validated_path(cfg, previous["note_path"]) if previous else None
            previous_text = previous_path.read_text(encoding="utf-8") if previous_path and previous_path.exists() else ""
            previous_fields = notes.session_metadata(previous_text) if previous_text else {}
            is_continuation = previous_fields.get("logical_session") == "true"
            if is_continuation:
                # Reserve even legacy continuation ids whose evidence headings were never rendered.
                prior_summaries = [json.loads(row['summary_json'] or '{}') for row in man.db.execute(
                    'SELECT summary_json FROM captures WHERE agent=? AND session_id=? AND note_path=?',
                    (agent, sid, previous['note_path']))]
                summary['_evidence_offset'] = max(notes.evidence_end(previous_text),
                    max((item.get('_evidence_offset', 0) + len(item.get('citations', []))
                         for item in prior_summaries), default=0))
            note_part = int(previous_fields.get("part", "1")) if is_continuation else part
            path = previous_path if is_continuation else notes.session_note_path(
                cfg.sessions_dir, when, agent, summary.get("title") or t.title or "session", sid, part)
            if is_continuation:
                text = notes.append_session_continuation(previous_text, when=when, source=t.source, start=t.start,
                        end=t.end, source_hash=source_hash, summarizer=kind, summary=summary, files=d.files,
                        commands=d.commands, gate_reason=d.gate_reason, capture_part=part, project=project,
                        email_allowlist=cfg.privacy_email_allowlist)
            else:
                text = notes.render_session(agent=agent, session_id=sid, project=project, part=part, when=when,
                        started=t.started, ended=t.ended, source=t.source, start=t.start, end=t.end, summarizer=kind,
                        summary=summary, files=d.files, commands=d.commands, gate_reason=d.gate_reason,
                        prev_link=Path(previous["note_path"]).with_suffix("").as_posix() if previous else None,
                        project_note=_link(cfg, projects.note_path(cfg.vault, project)), repo_head=repo_head,
                        attribution=attribution, source_hash=source_hash, email_allowlist=cfg.privacy_email_allowlist)
            text = redact_session_note(text, cfg.privacy_email_allowlist)
            receipt = dict(version=1, agent=agent, session_id=sid, part=part, start=t.start, end=t.end,
                           project=project, summary=summary, digest_sha=d.sha, note_path=path.relative_to(cfg.vault).as_posix(),
                           note_part=note_part, note_project=previous_fields["project"] if is_continuation else project,
                           base_content_sha=_hash(previous_text) if is_continuation else None,
                           text=text, content_sha=_hash(text), when=when.isoformat(), summarizer=kind, cost=cost,
                           prefix_sha=t.prefix_sha, evidence="transcript", related_projects=attribution.related_projects,
                           source_warnings=_redact_values(t.warnings, cfg.privacy_email_allowlist))
            if cfg.knowledge_mode != "off":
                try:
                    receipt["sources"] = ksources.build_receipt_packets(t, repo_head, part=part,
                        note_path=receipt["note_path"], origin_group=d.sha,
                        privacy=ksources._privacy_for(cfg, project) or "model_allowed", original_messages=original_messages,
                        email_allowlist=cfg.privacy_email_allowlist)
                except Exception:
                    log(cfg, "knowledge-packet-error", agent=agent, session=sid, part=part)
            _assert_opencode_source_unchanged(agent, sid, source, t)
        if ksources._privacy_for(cfg, receipt["project"]) == "excluded":
            man.mark(agent, sid, status="skipped", end_seen=0, project=receipt["project"], skip_reason="project excluded",
                     deferred_start=None)
            return "excluded"
        receipt["receipt_sha"] = _receipt_hash(receipt)
        create_once(_receipt_path(cfg, agent, sid, part), json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n")
        receipt = _load_receipt(cfg, agent, sid, part, range_start)
    if ksources._privacy_for(cfg, receipt["project"]) == "excluded":
        man.mark(agent, sid, status="skipped", end_seen=0, project=receipt["project"], skip_reason="project excluded",
                     deferred_start=None)
        return "excluded"
    path = _validated_path(cfg, receipt["note_path"])
    base_sha = receipt.get("base_content_sha")
    if base_sha:
        if not path.exists():
            raise ValueError("capture_identity_conflict: continuation base note is missing")
        current_sha = _hash(path.read_text(encoding="utf-8"))
        if current_sha == base_sha:
            atomic_write(path, receipt["text"])
        elif current_sha != receipt["content_sha"]:
            raise ValueError("capture_identity_conflict: continuation base note changed")
    else:
        create_once(path, receipt["text"])
    persisted = path.read_bytes()
    if hashlib.sha256(persisted).hexdigest() != receipt["content_sha"]:
        raise ValueError("capture_identity_conflict: persisted note bytes differ")
    _validate_note(persisted.decode("utf-8"), agent, sid, receipt.get("note_part", part),
                   expected_project=_note_project(receipt))
    link = _link(cfg, path)
    project, summary, kind = receipt["project"], receipt["summary"], receipt["summarizer"]
    when = _parse_time(receipt["when"])
    cost, consolidated = receipt["cost"], False
    if project != projects.UNSORTED:
        capture_id = _capture_identity(agent, sid, part)
        extra_cost, consolidated = consolidate_into_project(cfg, project, summary, link, when, kind, consolidator,
            order_part=part, capture_id=capture_id,
            evidence_available=receipt.get('note_part', part) == part or '_evidence_offset' in summary)
        cost += extra_cost
    for related in receipt["related_projects"]:
        note_path = projects.note_path(cfg.vault, related)
        with file_lock(cfg.locks_dir / f"project-{notes.slug(related, 12)}.lock", timeout=cfg.lock_timeout_s):
            notes.update_project_note(note_path, related, f"{when:%Y-%m-%d}", session_link=link,
                     session_line=f"- {when:%Y-%m-%d} [[{link}|{summary.get('title') or 'session'}]] (related)",
                     email_allowlist=cfg.privacy_email_allowlist)
    man.record_capture(agent=agent, session_id=sid, part=part, note_path=receipt["note_path"], project=project,
                       title=summary.get("title", ""), date=f"{when:%Y-%m-%d}", start=receipt["start"], end=receipt["end"],
                       summarizer=kind, cost=cost, sha=receipt["digest_sha"], consolidated=consolidated,
                       summary_json=json.dumps(summary, ensure_ascii=False),
                       error=_source_error(receipt.get("source_warnings", [])))
    if project != projects.UNSORTED:
        exclude = {(agent, sid, part)} if not consolidated else set()
        consolidate_pending(cfg, man, summarizer=summarizer, consolidator=consolidator, exclude=exclude)
    if cfg.knowledge_mode != "off" and project != projects.UNSORTED:
        try:
            outcome = ksources.maybe_import_after_capture(cfg, receipt)
            if outcome.status == "error":
                log(cfg, "knowledge-import-error", agent=agent, session=sid, part=part, reason=outcome.reason)
        except Exception as e:  # noqa: BLE001 - knowledge must never break capture
            log(cfg, "knowledge-import-error", agent=agent, session=sid, part=part, project=project,
                error=f"{type(e).__name__}: {e}")
    log(cfg, "captured", agent=agent, session=sid, part=part, project=project, note=link, summarizer=kind,
        consolidated=consolidated, cost_usd=round(cost, 5))
    return "captured"


def _source_error(warnings):
    return json.dumps({"source_warnings": warnings}, separators=(",", ":")) if warnings else None


def _redact_values(value, email_allowlist=None):
    if isinstance(value, str):
        return redact(value, email_allowlist=email_allowlist)
    if isinstance(value, dict):
        return {k: _redact_values(v, email_allowlist) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_values(v, email_allowlist) for v in value]
    return value


def _discovery_candidates(cfg: Config, man: Manifest, now, settle_s, discovery_since) -> list:
    """Aged transcripts not yet registered, oldest first. Each item: (modified, agent, path, known_id, row)."""
    roots = [("claude", Path(cfg.claude_transcripts_root)), ("codex", Path(cfg.codex_transcripts_root))]
    # This Devin build loads no hook importer reliably (none since 18 Nov), so its
    # transcripts are discovered here instead of waiting for a hook.
    devin_root = cfg.devin_transcripts_root
    if devin_root is None and adapters.devin_data_dir() is not None:
        devin_root = adapters.devin_data_dir() / "transcripts"
    if devin_root is not None:
        roots.append(("devin", devin_root))
    candidates = []
    for agent, root in roots:
        if not root.is_dir():
            continue
        if agent == "claude":
            paths = root.glob("*/*.jsonl")
        elif agent == "devin":
            paths = root.glob("*.json")
        else:
            paths = root.rglob("rollout-*.jsonl")
        for source in paths:
            try:
                modified = source.stat().st_mtime
                if agent == "claude":
                    uuid_pattern = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
                    if not re.fullmatch(uuid_pattern, source.stem):
                        continue
                    known_id = source.stem
                elif agent == "devin":
                    known_id = source.stem
                else:
                    match = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", source.name)
                    known_id = match.group(0) if match else ""
                if not known_id or modified > now - settle_s:
                    continue
                known = man.get(agent, known_id)
                if known and known["status"] not in ("captured", "skipped"):
                    continue
                if not known and modified <= discovery_since:
                    continue
                candidates.append((modified, agent, source, known_id, known))
            except OSError:
                continue
    return sorted(candidates, key=lambda item: (item[0], str(item[2])))


def discovery_backlog(cfg: Config, man: Manifest, now=None) -> dict:
    """Captures still waiting, labelled by kind: transcripts queued behind the per-drain
    discovery limit, manifest sessions already pending, and live errors awaiting retry."""
    now = time.time() if now is None else now
    candidates = _discovery_candidates(cfg, man, now, cfg.settle_minutes * 60, man.discovery_since(now))
    counts = {"unregistered": 0, "pending": 0, "retrying": 0}
    counts["unregistered"] = sum(1 for item in candidates if item[4] is None)
    for row in man.db.execute(
            "SELECT status, count(*) AS c FROM sessions WHERE status IN ('pending','error') GROUP BY status"):
        counts["pending" if row["status"] == "pending" else "retrying"] = row["c"]
    return counts


def _discover_orphan_transcripts(cfg: Config, man: Manifest, now=None) -> int:
    """Register aged Claude and Codex JSONL transcripts before the ordinary settle-window drain.

    It uses the configured roots and file modification time for the existing settle rule, then lets
    normal parsing, gating, privacy, and retry handling decide whether to capture. OpenCode exports
    require a known session id, so they remain hook-bound.
    """
    now = time.time() if now is None else now
    settle_s = cfg.settle_minutes * 60
    discovery_since = man.discovery_since(now)
    discovered = 0
    registered = 0
    candidates = _discovery_candidates(cfg, man, now, settle_s, discovery_since)
    for modified, agent, source, known_id, known in candidates:
        if known is None and registered >= cfg.discovery_max_per_drain:
            continue
        try:
            transcript = adapters.read(agent, source.stem, str(source))
            if not transcript.session_id or not transcript.cwd or not transcript.events:
                continue
            if _summarizer_cwd(transcript.cwd):
                continue
            known = man.get(agent, transcript.session_id)
            if known:
                if known["status"] in ("captured", "skipped") and transcript.end > known["captured_until"]:
                    discovered += int(man.reopen_for_growth(agent, transcript.session_id, end=transcript.end,
                        source=str(source), cwd=transcript.cwd, modified=modified))
                continue
            man.touch(agent, transcript.session_id, cwd=transcript.cwd, source=str(source), now=modified)
            discovered += 1
            registered += 1
        except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
            continue
    return discovered


def _summarizer_cwd(cwd: str) -> bool:
    """Match the neutral cwd used by llm._run without excluding other temp paths."""
    try:
        relative = Path(cwd).resolve().relative_to(Path(tempfile.gettempdir()).resolve())
    except (OSError, ValueError):
        return False
    return bool(relative.parts and relative.parts[0].startswith("agentmem-llm-"))


def _pending_lines(summary: dict, link: str, date: str) -> list:
    out = [f"- [ ] {date} [[{link}]] — {d['decision']}" for d in summary.get("decisions", [])[:6]]
    out += [f"- [ ] {date} [[{link}]] — fact: {s}" for s in summary.get("facts", [])[:4]]
    out += [f"- [ ] {date} [[{link}]] — next: {s}" for s in summary.get("next_steps", [])[:4]]
    out += [f"- [ ] {date} [[{link}]] — question: {s}" for s in summary.get("open_questions", [])[:4]]
    return out or [f"- [ ] {date} [[{link}]] — review session for state changes"]


def _pending_marker(pending: str, link: str, capture_id=None):
    identity = f"capture-ref:{capture_id}" if capture_id else f"[[{link}]]"
    for line in pending.splitlines():
        if f"[[{link}]]" not in line or identity not in line or "status update pending" not in line:
            continue
        base = re.search(r"\bbase=([0-9a-f]{64})\b", line)
        order = re.search(r"\border=(\d+):(\d+)\b", line)
        return (base[1] if base else None, (int(order[1]), int(order[2])) if order else None)
    return None, None


def _capture_identity(agent, sid, part):
    return hashlib.sha256(json.dumps([agent, sid, int(part)], separators=(",", ":")).encode("utf-8")).hexdigest()


def consolidate_into_project(cfg: Config, project: str, summary: dict, link: str, when: datetime,
                             summary_kind: str, consolidator=None, order_override=None, order_part: int = 1,
                             capture_id=None, retry: bool = False, held: list = None, evidence_available=True,
                             complete_summary: bool = False, failure: list = None) -> tuple:
    """Reconcile one session into its project note. Returns (cost, consolidated?)."""
    from . import status
    if consolidator is None:
        consolidator = lambda state, current_summary, link, date, timeout: llm.consolidate(
            state, current_summary, link, date, timeout=timeout, email_allowlist=cfg.privacy_email_allowlist)
    if ksources._privacy_for(cfg, project) == "excluded":
        if failure is not None:
            failure.append("invalid")
        return 0.0, False
    note_path = projects.note_path(cfg.vault, project)
    date = f"{when:%Y-%m-%d}"
    capture_marker = f"<!-- agentmem:capture:{capture_id} -->" if capture_id else ""
    accepted_marker = f"<!-- agentmem:accepted:{_hash(capture_id or link)} -->"
    pending_ref = f"capture-ref:{capture_id}" if capture_id else f"[[{link}]]"
    session_line = f"- {date} [[{link}|{summary.get('title') or 'session'}]]"
    consolidated_session_line = f"{session_line} {capture_marker} {accepted_marker}".rstrip()
    with file_lock(cfg.locks_dir / f"project-{notes.slug(project, 12)}.lock", timeout=cfg.lock_timeout_s):
        refused = notes.recover_status_update(note_path)
        if refused:
            log(cfg, 'status-replay-refused', project=project, journal=refused.name)
        if not note_path.exists():
            notes.update_project_note(note_path, project, date, email_allowlist=cfg.privacy_email_allowlist)
        text = notes.ensure_blocks(note_path.read_text(encoding="utf-8"))
        current_state = notes.get_block(text, notes.STATE_BEGIN, notes.STATE_END)
        pending = '\n'.join(notes.pending_lines(note_path, text))
        order_time = int(order_override if order_override is not None else when.timestamp() * 1_000_000)
        order = (order_time, int(order_part))
        base_revision = hashlib.sha256(current_state.encode("utf-8")).hexdigest()
        pending_base, pending_order = _pending_marker(pending, link, capture_id)
        session_block = notes.get_block(text, notes.SESS_BEGIN, notes.SESS_END)
        session_marker = capture_marker if capture_id else f"[[{link}|"
        if (evidence_available and session_marker in session_block and accepted_marker in session_block
                and pending_ref not in pending):
            return 0.0, True
        if (summary_kind != "deterministic" and cfg.summarizer != "none"
                and ksources._privacy_for(cfg, project) not in ("local_only", "excluded")):
            try:
                old = status.live_page(current_state)
                if not evidence_available:
                    if failure is not None:
                        failure.append("review")
                    raise NoProgress('legacy continuation lacks distinct evidence anchors; manual evidence repair required')
                if not status.evidence_refs(summary, link):
                    raise llm.LLMError('uncited summary, status not updated')
                result, cost = consolidator(current_state, summary,
                                           f"[[{link}]]", date, timeout=cfg.llm_timeout_s)
                source_orders = notes.evidence_orders(text, old)
                if isinstance(result, status.Result):
                    result = status.proposed_update(result, old)
                allowed = status.evidence_support(summary, link)
                if complete_summary:
                    result, decision_events = status.include_summary_items(old, result, summary, allowed, f"[[{link}]]")
                else:
                    result, decision_events = status.include_summary_decisions(old, result, summary, allowed)
                result = status.reconcile(old, result, allowed,
                                          source_orders=source_orders, new_order=order)
                result.events.extend(decision_events)
                retained_orders = {item.ref: source_orders.get(item.ref, order)
                                   for items in result.sections.values() for item in items}
                if ksources._privacy_for(cfg, project) == "excluded":
                    return cost, False
                old_ids = {item.id for items in old.values() for item in items}
                for displaced in result.displaced:
                    if displaced.item.id not in old_ids:
                        result.events.append(('rejected', f'{displaced.section}: new item removed ({displaced.reason})'))
                for action, item in result.events:
                    log(cfg, 'status-item', project=project, note=link, action=action, item=item)
                accepted = any(item.ref in allowed and status._matches_claim(item.text, item.ref, allowed)
                               for items in result.sections.values() for item in items)
                if not accepted:
                    reason = 'no current item cites accepted new evidence (model omission or retention)'
                    raise NoProgress(f'{reason}; new status items not accepted, reconciliation remains pending')
                notes.update_project_note(note_path, project, date, status_result=result,
                                          update_key=f'{capture_id or link}:{order[0]}:{order[1]}', pending_clear=pending_ref,
                                          session_link=link, session_line=consolidated_session_line,
                                          state_order_value=max(notes.state_order(text), order),
                                          evidence_order_values=retained_orders,
                                          email_allowlist=cfg.privacy_email_allowlist)
                return cost, True
            except (llm.LLMError, ValueError) as e:
                log(cfg, "llm-fallback", step="consolidate", project=project, note=link, error=str(e))
                if isinstance(e, NoProgress):
                    if held is not None and (failure is None or "review" not in failure):
                        held.append(str(e))
                    if failure is not None and "review" not in failure:
                        failure.append("omission")
                elif isinstance(e, llm.LLMError):
                    if failure is not None:
                        failure.append("transient")
                elif failure is not None:
                    failure.append("invalid")
                reason = str(e)
        else:
            reason = "summary is deterministic or project consolidation is unavailable"
            if failure is not None:
                failure.append("deterministic")
        reason = " ".join(reason.split())
        pending_order = pending_order or order
        pending_lines = [f"- [ ] {date} [[{link}]] {pending_ref} — status update pending ({reason[:180]}; "
                         f"base={pending_base or base_revision}; order={pending_order[0]}:{pending_order[1]})"]
        pending_lines += [f"{line} {pending_ref}" for line in _pending_lines(summary, link, date)
                          if "status update pending" not in line]
        notes.update_project_note(note_path, project, date, pending_clear=pending_ref,
                                  pending_add=pending_lines,
                                  session_link=link, session_line=session_line,
                                  email_allowlist=cfg.privacy_email_allowlist, touch=not retry)
        return 0.0, False


def _validate_pending_identity(cfg, man, row, text, summary, receipt=None):
    memory = row["summarizer"] == "remember"
    fields = notes.session_metadata(text, note_type="memory" if memory else "session")
    actual_part = fields.get("part", "1" if memory else None)
    expected_part = int(receipt.get("note_part", row["part"])) if receipt else int(row["part"])
    expected_project = (_note_project(receipt) if receipt else row["project"])
    if (fields.get("agent") != row["agent"] or fields.get("project") != expected_project
            or actual_part != str(expected_part)):
        raise ValueError("capture_identity_conflict: pending note identity or project differs")
    sid = fields.get("session_id")
    if sid == row["session_id"]:
        return
    if receipt:
        raise ValueError("capture_identity_conflict: pending receipt session differs")
    if sid != "[REDACTED]":
        raise ValueError("capture_identity_conflict: pending session differs")
    path = _validated_path(cfg, row["note_path"])
    matches = sum(_validated_path(cfg, r[0]) == path for r in man.db.execute("SELECT note_path FROM captures"))
    statement = summary.get("summary", "")
    body = text.split("\n---\n", 1)[1]
    if matches != 1 or not isinstance(statement, str) or not statement.strip() or " ".join(statement.split()) not in " ".join(body.split()):
        raise ValueError("capture_identity_conflict: masked pending identity lacks unique supported manifest")
    if not memory and notes.source_range(text) != (row["start_offset"], row["end_offset"]):
        raise ValueError("capture_identity_conflict: masked pending source range differs")


def _receipt_section_present(text, receipt, part):
    receipt_text = receipt["text"]
    if int(part) == int(receipt.get("note_part", part)):
        return text.startswith(receipt_text)
    sections = re.split(r"(?m)(?=^## Continued )", receipt_text)
    for section in sections:
        match = re.search(r"(?m)^- Capture part: (\d+)$", section)
        if match and int(match[1]) == int(part):
            return section.rstrip() in text
    return False


def consolidate_pending(cfg: Config, man: Manifest, summarizer=None, consolidator=None, limit: int = 20,
                        exclude: set = None) -> dict:
    """Retry reconciliation for captures that fell back (LLM down, or deterministic summary)."""
    if summarizer is None:
        summarizer = lambda digest, timeout: llm.summarize(
            digest, timeout=timeout, email_allowlist=cfg.privacy_email_allowlist)
    from . import status
    done = failed = 0
    excluded = exclude or set()
    rows = [row for row in man.retryable() if (row["agent"], row["session_id"], row["part"]) not in excluded]
    for row in rows[:limit]:
        if ksources._privacy_for(cfg, row["project"]) == "excluded":
            failed += 1
            continue
        summary = json.loads(row["summary_json"] or "{}")
        kind = row["summarizer"]
        try:
            path = _validated_path(cfg, row["note_path"])
            persisted = path.read_bytes()
            text = persisted.decode("utf-8")
            receipt = _load_receipt(cfg, row["agent"], row["session_id"], row["part"], row["start_offset"] or 0)
            _validate_pending_identity(cfg, man, row, text, summary, receipt)
            fields = notes.session_metadata(text, note_type="memory" if row["summarizer"] == "remember" else "session")
            if receipt and (receipt.get("project") != row["project"]
                            or receipt.get("note_path") != row["note_path"]
                            or receipt.get("start") != row["start_offset"]
                            or receipt.get("end") != row["end_offset"]
                            or not _receipt_section_present(text, receipt, row["part"])
                            or summary != receipt["summary"]):
                raise ValueError("capture_identity_conflict: pending evidence mismatch")
        except (OSError, ValueError) as error:
            log(cfg, "pending-retry-failed", session=row["session_id"], error=str(error))
            failed += 1
            continue
        # Reconciliation uses the summary that produced the immutable note.
        if kind == "deterministic" and cfg.summarizer != "none":
            kind = "persisted"
        link = Path(row["note_path"]).with_suffix("").as_posix()
        session_when = _parse_time(fields.get("started")) if fields.get("started") else _parse_time(row["date"])
        capture_id = (None if row["summarizer"] == "remember" else
                      _capture_identity(row["agent"], row["session_id"], row["part"]))
        held = []
        cost, ok = consolidate_into_project(cfg, row["project"], summary, link, session_when, kind, consolidator,
                                            order_override=int(session_when.timestamp() * 1_000_000),
                                            order_part=int(row["part"]), capture_id=capture_id, retry=True, held=held,
                                            evidence_available=int(fields.get('part', row['part'])) == row['part']
                                            or '_evidence_offset' in summary)
        if ok:
            man.set_consolidated(row["agent"], row["session_id"], row["part"], 1, cost)
            done += 1
        else:
            failed += 1
            if held:
                man.hold_retry(row["agent"], row["session_id"], row["part"], "no new item accepted")
                log(cfg, "pending-retry-held", session=row["session_id"], part=row["part"], reason="no new item accepted")
            # An uncited summary fails the same way on every retry, so hold it instead of letting it fill the batch.
            elif (kind != "deterministic" and ksources._privacy_for(cfg, row["project"]) != "local_only"
                    and not status.evidence_refs(summary, link)):
                man.hold_retry(row["agent"], row["session_id"], row["part"], "uncited summary")
                log(cfg, "pending-retry-held", session=row["session_id"], part=row["part"], reason="uncited summary")
    return {"consolidated": done, "still_pending": failed}


_STATUS_EVIDENCE_SECTIONS = frozenset((
    "decision", "decisions", "rationale", "facts", "what_happened", "latest",
    "next_steps", "next", "open_questions", "questions",
))


def _project_status_keys(cfg, project):
    path = projects.note_path(cfg.vault, project)
    if not path.exists():
        return set()
    try:
        text = path.read_text(encoding="utf-8")
        page = status.live_page(notes.get_block(text, notes.STATE_BEGIN, notes.STATE_END))
        return {status._claim_key(item.text) for items in page.values() for item in items}
    except Exception:
        return set()


def _held_cause(cfg, row, summary, link):
    """Why a held summary cannot become a cited status item, or promotable when it can."""
    decisions = summary.get("decisions") or []
    facts = summary.get("facts") or []
    next_steps = summary.get("next_steps") or []
    questions = summary.get("open_questions") or []
    if not any((decisions, facts, next_steps, questions)):
        return "trivial"
    try:
        citations = status.evidence_refs(summary, f"[[{link}]]")
        allowed = status.evidence_support(summary, f"[[{link}]]")
    except Exception as error:
        log(cfg, "held-cause-invalid", session=row["session_id"], part=row["part"], error=str(error))
        return "invalid-evidence"
    if not citations:
        return "missing-evidence"
    supported = {e.section for e in allowed.values() if e.section in _STATUS_EVIDENCE_SECTIONS}
    if not supported:
        return "nothing-new"
    if supported & {"facts", "what_happened", "latest"}:
        return "promotable"
    keys = _project_status_keys(cfg, row["project"])
    new_dec = dup_dec = new_nq = dup_nq = 0
    for evidence in allowed.values():
        if evidence.section in ("decision", "decisions", "rationale"):
            text = evidence.decision
            if not text:
                continue
            if status._claim_key(text) in keys:
                dup_dec += 1
            else:
                new_dec += 1
    target = link[2:-2] if link.startswith("[[") and link.endswith("]]") else link
    offset = summary.get("_evidence_offset") or 0
    if type(offset) is int and offset >= 0:
        refs = [(f"[[{target}#e{n}]]", citation) for n, citation in enumerate(summary.get("citations") or [], offset + 1)
                if isinstance(citation, dict) and isinstance(citation.get("claim"), str) and citation["claim"].strip()]
        for source, cited_sections in (("next_steps", ("next_steps", "next")),
                                       ("open_questions", ("open_questions", "questions"))):
            for item in summary.get(source) or []:
                text = item if isinstance(item, str) else item.get("text") if isinstance(item, dict) else None
                if not isinstance(text, str) or not text.strip():
                    continue
                key = status._claim_key(text)
                ref = next((ref for ref, citation in refs
                            if citation.get("section") in cited_sections and status._claim_key(citation.get("claim", "")) == key), "")
                if not ref:
                    continue
                if key in keys:
                    dup_nq += 1
                else:
                    new_nq += 1
    if new_dec or new_nq:
        return "promotable"
    if dup_dec or dup_nq:
        return "duplicate"
    return "nothing-new"


def _clear_held_pending(cfg, row):
    link = Path(row["note_path"]).with_suffix("").as_posix()
    capture_id = (None if row["summarizer"] == "remember" else
                  _capture_identity(row["agent"], row["session_id"], row["part"]))
    pending_ref = f"capture-ref:{capture_id}" if capture_id else f"[[{link}]]"
    note_path = projects.note_path(cfg.vault, row["project"])
    if not note_path.exists():
        return True
    try:
        with file_lock(cfg.locks_dir / f"project-{notes.slug(row['project'], 12)}.lock", timeout=cfg.lock_timeout_s):
            text = note_path.read_text(encoding="utf-8")
            if not any(pending_ref in line for line in notes.pending_lines(note_path, text)):
                return True
            notes.update_project_note(note_path, row["project"], row["date"] or time.strftime("%Y-%m-%d"),
                                      pending_clear=pending_ref, touch=False)
            return True
    except (OSError, ValueError, LockTimeout) as error:
        log(cfg, "held-cleanup-failed", session=row["session_id"], part=row["part"], error=str(error))
        return False


def _hold_review(cfg, man, row, attempts=None):
    man.hold_for_review(row["agent"], row["session_id"], row["part"], REVIEW_HOLD, attempts=attempts)
    log(cfg, "held-review", session=row["session_id"], part=row["part"], attempts=attempts)


def _retry_held(cfg, man, row, reason, now):
    attempts = row["retry_attempts"] or 0
    man.hold_retry(row["agent"], row["session_id"], row["part"], reason, attempts=attempts,
                   next_try=now + min(3600, 60 * 2 ** max(0, attempts - 1)))
    log(cfg, "held-retry", session=row["session_id"], part=row["part"], reason=reason, attempt=attempts)
    return attempts


def _close_held(cfg, man, row, reason):
    if not _clear_held_pending(cfg, row):
        return False
    man.close_capture(row["agent"], row["session_id"], row["part"], reason)
    log(cfg, "held-closed", session=row["session_id"], part=row["part"], reason=reason)
    return True


def triage_held(cfg: Config, man: Manifest, consolidator=None, limit: int = 20,
                max_attempts: int = 3, now=None) -> dict:
    """Give every held capture one deterministic outcome, with bounded retries for a real omission."""
    now = time.time() if now is None else float(now)
    counts = {"promoted": 0, "closed": 0, "retrying": 0, "review": 0, "by_reason": {}}
    for row in man.held(now)[:limit]:
        link = Path(row["note_path"]).with_suffix("").as_posix()
        try:
            summary = json.loads(row["summary_json"] or "{}")
            if not isinstance(summary, dict):
                raise ValueError("invalid summary")
            path = _validated_path(cfg, row["note_path"])
            persisted = path.read_bytes()
            text = persisted.decode("utf-8")
            receipt = _load_receipt(cfg, row["agent"], row["session_id"], row["part"], row["start_offset"] or 0)
            _validate_pending_identity(cfg, man, row, text, summary, receipt)
            fields = notes.session_metadata(text, note_type="memory" if row["summarizer"] == "remember" else "session")
            if receipt and (receipt.get("project") != row["project"]
                            or receipt.get("note_path") != row["note_path"]
                            or receipt.get("start") != row["start_offset"]
                            or receipt.get("end") != row["end_offset"]
                            or not _receipt_section_present(text, receipt, row["part"])
                            or summary != receipt["summary"]):
                raise ValueError("capture_identity_conflict: held evidence mismatch")
        except OSError as error:
            log(cfg, "held-triage-read-error", session=row["session_id"], part=row["part"], error=str(error))
            _retry_held(cfg, man, row, "transient-error", now)
            counts["retrying"] += 1
            continue
        except (ValueError, json.JSONDecodeError) as error:
            log(cfg, "held-triage-invalid", session=row["session_id"], part=row["part"], error=str(error))
            if _close_held(cfg, man, row, "invalid-evidence"):
                counts["closed"] += 1
                counts["by_reason"]["invalid-evidence"] = counts["by_reason"].get("invalid-evidence", 0) + 1
            else:
                _retry_held(cfg, man, row, "cleanup-pending", now)
                counts["retrying"] += 1
            continue
        cause = _held_cause(cfg, row, summary, link)
        if cause != "promotable":
            if cause in ("missing-evidence", "trivial", "invalid-evidence"):
                if _close_held(cfg, man, row, cause):
                    counts["closed"] += 1
                    counts["by_reason"][cause] = counts["by_reason"].get(cause, 0) + 1
                else:
                    _retry_held(cfg, man, row, "cleanup-pending", now)
                    counts["retrying"] += 1
            else:
                _hold_review(cfg, man, row, row["retry_attempts"] or 0)
                counts["review"] += 1
                counts["by_reason"]["needs-review"] = counts["by_reason"].get("needs-review", 0) + 1
            continue
        kind = row["summarizer"]
        if kind == "deterministic" and cfg.summarizer != "none":
            kind = "persisted"
        session_when = _parse_time(fields.get("started")) if fields.get("started") else _parse_time(row["date"])
        capture_id = (None if row["summarizer"] == "remember" else
                      _capture_identity(row["agent"], row["session_id"], row["part"]))
        held = []
        failure = []
        try:
            cost, ok = consolidate_into_project(
                cfg, row["project"], summary, link, session_when, kind, consolidator,
                order_override=int(session_when.timestamp() * 1_000_000), order_part=int(row["part"]),
                capture_id=capture_id, retry=True, held=held, complete_summary=True, failure=failure,
                evidence_available=int(fields.get("part", row["part"])) == row["part"] or "_evidence_offset" in summary,
            )
        except Exception as error:  # noqa: BLE001 - a held row must never crash the worker
            log(cfg, "held-triage-error", session=row["session_id"], part=row["part"], error=str(error))
            cost, ok = 0.0, False
            failure.append("transient")
        if ok:
            man.promote_capture(row["agent"], row["session_id"], row["part"], cost)
            counts["promoted"] += 1
            log(cfg, "held-promoted", session=row["session_id"], part=row["part"])
            continue
        if not failure or failure[0] == "transient":
            _retry_held(cfg, man, row, "transient-error", now)
            counts["retrying"] += 1
            continue
        if failure[0] == "invalid":
            if _close_held(cfg, man, row, "invalid-evidence"):
                counts["closed"] += 1
                counts["by_reason"]["invalid-evidence"] = counts["by_reason"].get("invalid-evidence", 0) + 1
            else:
                _retry_held(cfg, man, row, "cleanup-pending", now)
                counts["retrying"] += 1
            continue
        if failure[0] == "review":
            _hold_review(cfg, man, row, row["retry_attempts"] or 0)
            counts["review"] += 1
            counts["by_reason"]["needs-review"] = counts["by_reason"].get("needs-review", 0) + 1
            continue
        if failure[0] == "deterministic":
            _hold_review(cfg, man, row, row["retry_attempts"] or 0)
            counts["review"] += 1
            counts["by_reason"]["needs-review"] = counts["by_reason"].get("needs-review", 0) + 1
            continue
        cause = _held_cause(cfg, row, summary, link)
        if cause != "promotable":
            if cause in ("missing-evidence", "trivial", "invalid-evidence"):
                if _close_held(cfg, man, row, cause):
                    counts["closed"] += 1
                    counts["by_reason"][cause] = counts["by_reason"].get(cause, 0) + 1
                else:
                    _retry_held(cfg, man, row, "cleanup-pending", now)
                    counts["retrying"] += 1
            else:
                _hold_review(cfg, man, row, row["retry_attempts"] or 0)
                counts["review"] += 1
                counts["by_reason"]["needs-review"] = counts["by_reason"].get("needs-review", 0) + 1
            continue
        attempts = (row["retry_attempts"] or 0) + 1
        if attempts >= max_attempts:
            _hold_review(cfg, man, row, attempts)
            counts["review"] += 1
            counts["by_reason"]["needs-review"] = counts["by_reason"].get("needs-review", 0) + 1
        else:
            man.hold_retry(row["agent"], row["session_id"], row["part"], "model omission",
                           attempts=attempts, next_try=now + min(3600, 60 * 2 ** (attempts - 1)))
            counts["retrying"] += 1
            log(cfg, "held-retry", session=row["session_id"], part=row["part"], attempt=attempts)
    return counts


def process_ready(cfg: Config, man: Manifest, budget_s: float = 900, summarizer=None, consolidator=None) -> list:
    results = []
    deadline = time.monotonic() + budget_s
    settle_s = cfg.settle_minutes * 60
    _discover_orphan_transcripts(cfg, man)
    for row in man.ready(settle_s, cfg.max_attempts):
        if time.monotonic() > deadline or cfg.paused():
            break
        try:
            with file_lock(cfg.locks_dir / f"session-{row['agent']}-{notes.slug(row['session_id'], 12)}.lock", timeout=5):
                fresh = man.get(row["agent"], row["session_id"])  # re-read under the lock
                retry_at = reason = None
                if fresh["agent"] == "opencode":
                    retry_at, reason = _opencode_retry_at(cfg, fresh["session_id"], settle_s, time.time())
                if retry_at is None:
                    outcome = finalize(cfg, man, fresh, summarizer, consolidator)
                else:
                    man.mark(fresh["agent"], fresh["session_id"], next_try=retry_at)
                    log(cfg, "capture-deferred", agent=fresh["agent"], session=fresh["session_id"], reason=reason)
                    outcome = "deferred-active-source"
                settled = man.get(row["agent"], row["session_id"])
                if (settled and settled["status"] in ("captured", "skipped")
                        and settled["deferred_start"] is None):
                    cfg.snapshot_path(row["agent"], row["session_id"]).unlink(missing_ok=True)
        except OpenCodeSourceChanging as e:
            retry_at, reason = _opencode_retry_at(cfg, row["session_id"], settle_s, time.time())
            if retry_at is None:
                retry_at = time.time() + settle_s
                reason = "opencode source changed before capture commit"
            man.mark(row["agent"], row["session_id"], next_try=retry_at)
            log(cfg, "capture-deferred", agent=row["agent"], session=row["session_id"], reason=reason)
            outcome = "deferred-source-change"
        except Exception as e:  # noqa: BLE001 - any failure is recorded and retried with backoff
            man.fail(row["agent"], row["session_id"], f"{type(e).__name__}: {e}",
                     max_attempts=cfg.max_attempts, max_age_s=cfg.error_max_age_days * 86400,
                     grace_s=cfg.error_grace_hours * 3600,
                     exists=lambda r: transcript_located(cfg, r))
            log(cfg, "error", agent=row["agent"], session=row["session_id"], error=f"{type(e).__name__}: {e}",
                trace=traceback.format_exc(limit=3))
            outcome = "error"
        results.append((row["agent"], row["session_id"], outcome))
    return results
