"""Capture pipeline: settled session -> digest -> gate -> summary -> immutable note -> reconciled project note.

Order of effects is chosen so any crash is safe to retry:
  1. note is created once (deterministic part index; an existing note for that part is reused)
  2. project note is reconciled under the project lock
  3. manifest records the capture (one transaction) -> cursor advances
A retry after a crash between 1 and 3 finds the note, re-reconciles (idempotent by session link), records.
"""
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
from .redact import contains_secret, redact


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


def _existing_note(cfg: Config, session_id: str, part: int) -> "Path | None":
    id8 = "".join(ch for ch in session_id if ch.isalnum())[-8:]
    suffix = f"-p{part}" if part > 1 else ""
    hits = [p for p in cfg.sessions_dir.glob(f"*/*/*-{id8}{suffix}.md")]
    return hits[0] if hits else None


def _link(cfg: Config, path: Path) -> str:
    return path.relative_to(cfg.vault).with_suffix("").as_posix()


def finalize(cfg: Config, man: Manifest, row, summarizer=None, consolidator=None) -> str:
    """Capture one settled session. Returns an outcome string; raises on retryable failure."""
    summarizer = summarizer or llm.summarize
    consolidator = consolidator or llm.consolidate
    agent, sid = row["agent"], row["session_id"]
    source = _source_for(row)
    if not source:
        raise FileNotFoundError(f"no transcript for {agent}:{sid}")
    t = adapters.read(agent, sid, source, start=row["captured_until"])
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
        # cursor stays put: if the session grows later, the whole range is judged again
        man.mark(agent, sid, status=done_status, end_seen=0, skip_reason=d.gate_reason, cwd=cwd)
        log(cfg, "skip", agent=agent, session=sid, reason=d.gate_reason)
        return "trivial"

    project = projects.resolve(cwd, cfg.projects_map)
    part = row["parts"] + 1
    when = _parse_time(t.started)
    cost = 0.0
    summary_kind = "deterministic"
    summary = None
    if cfg.summarizer == "claude-haiku":
        try:
            summary = summarizer(d.text, timeout=cfg.llm_timeout_s)
            cost = summary.pop("_cost_usd", 0.0)
            summary_kind = "claude-haiku"
        except llm.LLMError as e:
            log(cfg, "llm-fallback", agent=agent, session=sid, step="summarize", error=str(e))
    if summary is None:
        summary = extract.fallback_summary(t, d)

    path = _existing_note(cfg, sid, part)
    if path is None:
        prev = man.capture(agent, sid, part - 1) if part > 1 else None
        path = notes.session_note_path(cfg.sessions_dir, when, agent, summary.get("title") or t.title or "session", sid, part)
        text = notes.render_session(
            agent=agent, session_id=sid, project=project, part=part, when=when, started=t.started, ended=t.ended,
            source=t.source, start=t.start, end=t.end, summarizer=summary_kind, summary=summary, files=d.files,
            commands=d.commands, gate_reason=d.gate_reason,
            prev_link=Path(prev["note_path"]).with_suffix("").as_posix() if prev else None)
        if contains_secret(text):  # belt and braces: everything was redacted upstream
            text = redact(text)
        create_once(path, text)
    link = _link(cfg, path)

    consolidated = False
    if project != projects.UNSORTED:
        cost_c, consolidated = consolidate_into_project(cfg, project, summary, link, when, summary_kind, consolidator)
        cost += cost_c

    man.record_capture(agent=agent, session_id=sid, part=part, note_path=path.relative_to(cfg.vault).as_posix(),
                       project=project, title=summary.get("title", ""), date=f"{when:%Y-%m-%d}", start=t.start,
                       end=t.end, summarizer=summary_kind, cost=cost, sha=d.sha, consolidated=consolidated,
                       summary_json=json.dumps(summary, ensure_ascii=False))
    log(cfg, "captured", agent=agent, session=sid, part=part, project=project, note=link,
        summarizer=summary_kind, consolidated=consolidated, cost_usd=round(cost, 5))
    return "captured"


def _pending_lines(summary: dict, link: str, date: str) -> list:
    out = [f"- [ ] {date} [[{link}]] — {d['decision']}" for d in summary.get("decisions", [])[:6]]
    out += [f"- [ ] {date} [[{link}]] — next: {s}" for s in summary.get("next_steps", [])[:4]]
    return out or [f"- [ ] {date} [[{link}]] — review session for state changes"]


def consolidate_into_project(cfg: Config, project: str, summary: dict, link: str, when: datetime,
                             summary_kind: str, consolidator=None) -> tuple:
    """Reconcile one session into its project note. Returns (cost, consolidated?)."""
    consolidator = consolidator or llm.consolidate
    note_path = cfg.projects_dir / project / f"{project}.md"
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
        if kind == "deterministic" and cfg.summarizer == "claude-haiku":
            try:  # re-derive a model summary for reconciliation only; the session note stays as written
                srow = man.get(row["agent"], row["session_id"])
                t = adapters.read(row["agent"], row["session_id"], _source_for(srow), start=row["start_offset"] or 0)
                d = extract.build(t, cfg.min_user_prompts, cfg.min_assistant_chars, cfg.digest_char_cap)
                summary = summarizer(d.text, timeout=cfg.llm_timeout_s)
                summary.pop("_cost_usd", None)
                kind = "claude-haiku"
            except Exception as e:  # noqa: BLE001 - transcript gone or model down: leave it pending
                log(cfg, "pending-retry-failed", session=row["session_id"], error=str(e))
                failed += 1
                continue
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
