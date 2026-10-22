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
import time
import traceback
from datetime import datetime
from pathlib import Path

from . import adapters, extract, llm, notes, projects
from .config import Config
from .fsutil import create_once, file_lock
from .logutil import log
from .manifest import Manifest
from .redact import redact, redact_session_note


def _parse_time(value: str) -> datetime:
    if value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
        except ValueError:
            pass
    return datetime.now().astimezone()


def _source_for(row) -> str:
    if row["agent"] == "codex" and not (row["source"] and Path(row["source"]).exists()):
        return adapters.find_codex_transcript(row["session_id"]) or ""
    if row["agent"] == "opencode":
        return row["session_id"]
    return row["source"] or ""


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


def _receipt_path(cfg, agent, sid, part):
    return cfg.meta / "capture-receipts" / (_identity_key(agent, sid, part) + ".json")


def _receipt_hash(receipt):
    return _hash(json.dumps({k: v for k, v in receipt.items() if k != "receipt_sha"}, sort_keys=True, ensure_ascii=False))


def _load_receipt(cfg, agent, sid, part, start):
    path = _receipt_path(cfg, agent, sid, part)
    if not path.exists():
        return None
    try:
        r = json.loads(path.read_text(encoding="utf-8"))
        if (r["version"] != 1 or (r["agent"], r["session_id"], r["part"], r["start"]) != (agent, sid, part, start)
                or r["receipt_sha"] != _receipt_hash(r) or _hash(r["text"]) != r["content_sha"]):
            raise ValueError("receipt identity or hash mismatch")
        _validated_path(cfg, r["note_path"])
        _validate_note(r["text"], agent, sid, part)
        return r
    except (ValueError, KeyError, TypeError) as error:
        raise ValueError("capture_identity_conflict: invalid receipt") from error


def _validated_path(cfg, relative):
    path = (cfg.vault / relative).resolve()
    if not path.is_relative_to(cfg.sessions_dir.resolve()):
        raise ValueError("capture_identity_conflict: note outside session directory")
    return path


def _validate_note(text, agent, sid, part):
    fields = notes.session_metadata(text)
    if (fields.get("agent"), fields.get("session_id"), fields.get("part")) != (agent, sid, str(part)):
        raise ValueError("capture_identity_conflict: note identity mismatch")
    return fields


def _legacy_evidence(cfg, agent, sid, part):
    suffix = "".join(ch for ch in sid if ch.isalnum())[-8:]
    tail = f"-p{part}" if part > 1 else ""
    matches = []
    for path in cfg.sessions_dir.glob(f"*/*/*-{suffix}{tail}.md"):
        text = path.read_text(encoding="utf-8")
        fields = notes.session_metadata(text)
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
        return _finalize_locked(cfg, man, fresh, summarizer or llm.summarize, consolidator)


def _finalize_locked(cfg, man, row, summarizer, consolidator):
    agent, sid, part = row["agent"], row["session_id"], row["parts"] + 1
    receipt = _load_receipt(cfg, agent, sid, part, row["captured_until"])
    if receipt is None:
        legacy = _legacy_evidence(cfg, agent, sid, part)
        if legacy:
            path, text, fields = legacy
            summary = notes.summary_from_session(text)
            start, end = notes.source_range(text)
            if start != row["captured_until"]:
                raise ValueError("capture_identity_conflict: legacy range mismatch")
            receipt = dict(version=1, agent=agent, session_id=sid, part=part, start=start, end=end,
                           project=fields["project"], summary=summary, digest_sha=_hash(text),
                           note_path=path.relative_to(cfg.vault).as_posix(), text=text, content_sha=_hash(text),
                           when=fields["date"], summarizer=fields.get("summarizer", "legacy"), cost=0,
                           prefix_sha="", evidence="legacy-note", related_projects=[])
        else:
            source = _source_for(row)
            if not source:
                raise FileNotFoundError(f"no transcript for {agent}:{sid}")
            if row["parts"] and agent == "opencode":
                previous = _load_receipt(cfg, agent, sid, part - 1, man.capture(agent, sid, part - 1)["start_offset"])
                if previous and previous.get("prefix_sha"):
                    prefix = adapters.read(agent, sid, source, end=row["captured_until"])
                    if prefix.prefix_sha != previous["prefix_sha"]:
                        raise ValueError("source_change: earlier OpenCode messages mutated")
            t = adapters.read(agent, sid, source, start=row["captured_until"])
            if t.session_id != sid:
                raise ValueError("capture_identity_conflict: transcript session differs")
            cwd = t.cwd or row["cwd"] or ""
            done_status = "captured" if row["parts"] else "skipped"
            if t.end <= row["captured_until"] or not t.events:
                man.mark(agent, sid, status=done_status, end_seen=0, skip_reason=row["skip_reason"] or "no new content")
                return "no-new-content"
            if _excluded(cfg, cwd):
                man.mark(agent, sid, status="skipped", end_seen=0, skip_reason="excluded directory")
                return "excluded"
            d = extract.build(t, cfg.min_user_prompts, cfg.min_assistant_chars, cfg.digest_char_cap)
            if not d.substantial:
                man.mark(agent, sid, status=done_status, end_seen=0, skip_reason=d.gate_reason, cwd=cwd)
                return "trivial"
            edited = [p for ev in t.events if ev.kind == "edit" for p in ev.paths]
            project = projects.resolve_session(cwd, cfg.projects_map, files=edited, text="\n".join(d.prompts))
            when = _parse_time(t.started)
            cost, kind, summary = 0.0, "deterministic", None
            if cfg.summarizer == "claude-haiku":
                try:
                    summary = summarizer(d.text, timeout=cfg.llm_timeout_s)
                    cost = summary.pop("_cost_usd", 0.0)
                    kind = "claude-haiku"
                except llm.LLMError as error:
                    log(cfg, "llm-fallback", agent=agent, session=sid, step="summarize", error=str(error))
            if summary is None:
                summary = extract.fallback_summary(t, d)
            summary = _redact_values(summary)
            previous = man.capture(agent, sid, part - 1) if part > 1 else None
            path = notes.session_note_path(cfg.sessions_dir, when, agent, summary.get("title") or t.title or "session", sid, part)
            text = notes.render_session(agent=agent, session_id=sid, project=project, part=part, when=when,
                        started=t.started, ended=t.ended, source=t.source, start=t.start, end=t.end, summarizer=kind,
                        summary=summary, files=d.files, commands=d.commands, gate_reason=d.gate_reason,
                        prev_link=Path(previous["note_path"]).with_suffix("").as_posix() if previous else None,
                        project_note=_link(cfg, projects.note_path(cfg.vault, project)))
            text = redact_session_note(text)
            receipt = dict(version=1, agent=agent, session_id=sid, part=part, start=t.start, end=t.end,
                           project=project, summary=summary, digest_sha=d.sha, note_path=path.relative_to(cfg.vault).as_posix(),
                           text=text, content_sha=_hash(text), when=when.isoformat(), summarizer=kind, cost=cost,
                           prefix_sha=t.prefix_sha, evidence="transcript", related_projects=[])
        receipt["receipt_sha"] = _receipt_hash(receipt)
        create_once(_receipt_path(cfg, agent, sid, part), json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n")
        receipt = _load_receipt(cfg, agent, sid, part, row["captured_until"])
    path = _validated_path(cfg, receipt["note_path"])
    create_once(path, receipt["text"])
    persisted = path.read_bytes()
    if hashlib.sha256(persisted).hexdigest() != receipt["content_sha"]:
        raise ValueError("capture_identity_conflict: persisted note bytes differ")
    _validate_note(persisted.decode("utf-8"), agent, sid, part)
    link = _link(cfg, path)
    project, summary, kind = receipt["project"], receipt["summary"], receipt["summarizer"]
    when = _parse_time(receipt["when"])
    cost, consolidated = receipt["cost"], False
    if project != projects.UNSORTED:
        extra_cost, consolidated = consolidate_into_project(cfg, project, summary, link, when, kind, consolidator)
        cost += extra_cost
    man.record_capture(agent=agent, session_id=sid, part=part, note_path=receipt["note_path"], project=project,
                       title=summary.get("title", ""), date=f"{when:%Y-%m-%d}", start=receipt["start"], end=receipt["end"],
                       summarizer=kind, cost=cost, sha=receipt["digest_sha"], consolidated=consolidated,
                       summary_json=json.dumps(summary, ensure_ascii=False))
    log(cfg, "captured", agent=agent, session=sid, part=part, project=project, note=link, summarizer=kind,
        consolidated=consolidated, cost_usd=round(cost, 5))
    return "captured"


def _redact_values(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _redact_values(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_values(v) for v in value]
    return value


def _pending_lines(summary: dict, link: str, date: str) -> list:
    out = [f"- [ ] {date} [[{link}]] — {d['decision']}" for d in summary.get("decisions", [])[:6]]
    out += [f"- [ ] {date} [[{link}]] — fact: {s}" for s in summary.get("facts", [])[:4]]
    out += [f"- [ ] {date} [[{link}]] — next: {s}" for s in summary.get("next_steps", [])[:4]]
    return out or [f"- [ ] {date} [[{link}]] — review session for state changes"]


def consolidate_into_project(cfg: Config, project: str, summary: dict, link: str, when: datetime,
                             summary_kind: str, consolidator=None) -> tuple:
    """Reconcile one session into its project note. Returns (cost, consolidated?)."""
    consolidator = consolidator or llm.consolidate
    note_path = projects.note_path(cfg.vault, project)
    date = f"{when:%Y-%m-%d}"
    session_line = f"- {date} [[{link}|{summary.get('title') or 'session'}]]"
    with file_lock(cfg.locks_dir / f"project-{notes.slug(project, 12)}.lock", timeout=cfg.lock_timeout_s):
        if not note_path.exists():
            notes.update_project_note(note_path, project, date)
        text = notes.ensure_blocks(note_path.read_text(encoding="utf-8"))
        if summary_kind != "deterministic" and cfg.summarizer != "none":
            try:
                state, cost = consolidator(notes.get_block(text, notes.STATE_BEGIN, notes.STATE_END), summary,
                                           f"[[{link}]]", date, timeout=cfg.llm_timeout_s)
                notes.update_project_note(note_path, project, date, state=state, pending_clear=f"[[{link}]]",
                                          session_link=link, session_line=session_line)
                return cost, True
            except llm.LLMError as e:
                log(cfg, "llm-fallback", step="consolidate", project=project, note=link, error=str(e))
        notes.update_project_note(note_path, project, date, pending_add=_pending_lines(summary, link, date),
                                  session_link=link, session_line=session_line)
        return 0.0, False


def consolidate_pending(cfg: Config, man: Manifest, summarizer=None, consolidator=None, limit: int = 20) -> dict:
    """Retry reconciliation for captures that fell back (LLM down, or deterministic summary)."""
    summarizer = summarizer or llm.summarize
    done = failed = 0
    for row in man.unconsolidated()[:limit]:
        summary = json.loads(row["summary_json"] or "{}")
        kind = row["summarizer"]
        # Reconciliation uses the summary that produced the immutable note.
        if kind == "deterministic" and cfg.summarizer != "none":
            kind = "persisted"
        link = Path(row["note_path"]).with_suffix("").as_posix()
        cost, ok = consolidate_into_project(cfg, row["project"], summary, link, _parse_time(row["date"]), kind, consolidator)
        if ok:
            man.set_consolidated(row["agent"], row["session_id"], row["part"], 1, cost)
            done += 1
        else:
            failed += 1
    return {"consolidated": done, "still_pending": failed}


def process_ready(cfg: Config, man: Manifest, budget_s: float = 900, summarizer=None, consolidator=None) -> list:
    results = []
    deadline = time.monotonic() + budget_s
    for row in man.ready(cfg.settle_minutes * 60, cfg.max_attempts):
        if time.monotonic() > deadline or cfg.paused():
            break
        try:
            with file_lock(cfg.locks_dir / f"session-{row['agent']}-{notes.slug(row['session_id'], 12)}.lock", timeout=5):
                fresh = man.get(row["agent"], row["session_id"])  # re-read under the lock
                outcome = finalize(cfg, man, fresh, summarizer, consolidator)
        except Exception as e:  # noqa: BLE001 - any failure is recorded and retried with backoff
            man.fail(row["agent"], row["session_id"], f"{type(e).__name__}: {e}")
            log(cfg, "error", agent=row["agent"], session=row["session_id"], error=f"{type(e).__name__}: {e}",
                trace=traceback.format_exc(limit=3))
            outcome = "error"
        results.append((row["agent"], row["session_id"], outcome))
    return results
