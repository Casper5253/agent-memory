"""Lifecycle hook entry for Claude Code and Codex (same JSON contract). Must be fast and never fail the agent.

Stop / PreCompact -> enqueue "turn"; SessionEnd -> enqueue "end"; SessionStart -> small recall pointer.
Every event also kicks a detached worker. Output is always valid hook JSON ({} unless adding context).
"""
import json
import os
import re
import subprocess
import sys

from .config import CAPTURE_ENV

CONTEXT_CAP = 4000
GIT_TIMEOUT_S = 2
STATE_CAP = 600


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _git_line(cwd: str) -> str:
    """Live Git facts, read now and never written to the vault. Empty when cwd is not a readable repository."""
    from . import projects
    if not projects.repository_root(cwd):
        return ""

    def run(*args):
        try:
            result = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=GIT_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    branch, head, status = run("rev-parse", "--abbrev-ref", "HEAD"), run("rev-parse", "--short", "HEAD"), run("status", "--porcelain")
    if branch is None or head is None or status is None:
        return ""
    line = f"Git (live at start): branch {branch}, HEAD {head}, {'dirty' if status else 'clean'}"
    counts = run("rev-list", "--left-right", "--count", "@{u}...HEAD")  # fails without an upstream: omitted
    if counts:
        behind, ahead = counts.split()
        line += f", ahead {ahead} / behind {behind}"
    return line


def _health_line(man, max_attempts: int, note=None, pending='') -> str:
    health = man.health(max_attempts)
    parts = []
    if health["pending"]:
        parts.append(f"{_plural(health['pending'], 'session')} not yet captured")
    if health["failed"]:
        reason = man.failed(max_attempts)[0]["last_error"] or "unknown error"
        parts.append(f"{health['failed']} failed: {reason[:160]}")
    backlog = len(man.unconsolidated())
    reason = re.search(r'status update pending \(([^;\n)]+)', pending)
    if backlog:
        why = reason[1][:100] if reason else 'reconciliation pending'
        parts.append(f"{_plural(backlog, 'captured session')} awaiting consolidation ({why}); see {note or 'project notes'}")
        preview = next((line for line in pending.splitlines() if 'status update pending' not in line and line.strip()), '')
        if preview:
            preview = re.sub(r'^- \[ \] \S+ \[\[[^\]]+\]\]\s*[^\w]*', '', preview)
            parts.append('pending: ' + preview[:100])
    elif 'status update pending' in pending:
        why = f' ({reason[1][:100]})' if reason else ''
        parts.append(f"status update pending{why}; see {note}")
    if 'status replay refused' in pending:
        parts.append(f'status replay refused; see {note}')
    return f"Capture health: {', '.join(parts)}" if parts else ""


def startup_package(cfg, cwd: str) -> str:
    """Reserve health and omission pointer first, then admit complete status items in priority order."""
    from . import notes, projects, status
    from .manifest import Manifest
    project = projects.resolve_session(cwd, cfg.projects_map, create=False)
    head = [f"Agent memory: Obsidian vault at {cfg.vault}. Session notes (0-Sessions) are HISTORICAL; project notes (1-Projects) are CURRENT state.",
            'When asked what was previously decided, implemented or configured, search the vault first: '
            '`mem search "<terms>"` (memory skill), never guess.']
    page, pending, note, legacy, recent = status.empty(), '', None, '', ''
    man = Manifest(cfg.manifest_path)
    try:
        if project != projects.UNSORTED:
            note = projects.note_path(cfg.vault, project)
            head.append(f"Project: {project} - {note}")
            if note.exists():
                try:
                    text = note.read_text(encoding='utf-8')
                    pending = notes.get_block(text, notes.PENDING_BEGIN, notes.PENDING_END)
                    block = notes.get_block(text, notes.STATE_BEGIN, notes.STATE_END)
                    try:
                        page = status.live_page(block)
                    except ValueError:
                        current = re.search(r'(?ms)^### Current state\s*\n(.*?)(?=^### |\Z)', block)
                        if current:
                            legacy = 'Legacy current state (page not migrated):\n' + current[1].strip()[:STATE_CAP]
                        pending += ' status update pending (page not migrated)'
                except (ValueError, OSError):
                    pending += ' status update pending (page not migrated)'
            entries = []
            for row in man.query_captures(project=project, limit=3):
                title = ' '.join(row['title'].split())[:80]
                link = row['note_path'].removesuffix('.md')
                source = f'[[{link}|{title}]]' if len(link) <= 220 and not re.search(r'[\r\n\[\]|]', link) else title
                entries.append(f"{row['date'][:10]} {source}")
            if entries:
                recent = 'Recent sessions: ' + '; '.join(entries)
        head += [line for line in (_git_line(cwd), _health_line(man, cfg.max_attempts, note, pending)) if line]
    finally:
        man.close()
    count = sum(len(items) for items in page.values())
    pointer = f"{count} more items in {note}" if count else ''
    reserved = '\n'.join(value for value in (pointer, recent) if value)
    fixed = '\n'.join(head)
    # Abnormally long configured paths must not consume the health line or omission pointer.
    if len(fixed) + len(reserved) + 1 > CONTEXT_CAP:
        health = next((line for line in head if line.startswith('Capture health:')), '')
        priority = '\n'.join(line for line in (health, pointer, recent) if line)
        room = max(0, CONTEXT_CAP - len(priority) - 1)
        return ('\n'.join(head[:3])[:room] + '\n' + priority)[-CONTEXT_CAP:]
    eligible = {key: [item for item in page[key] if len(item.text) <= 280] for key in status.STARTUP_ORDER}
    selection = {key: [] for key in status.STARTUP_ORDER}
    # Round-robin admission: every section gets its first item before any section gets a second.
    for rank in range(max((len(items) for items in eligible.values()), default=0)):
        for key in status.STARTUP_ORDER:
            if rank < len(eligible[key]):
                trial = {**selection, key: selection[key] + [eligible[key][rank]]}
                if len('\n'.join(_startup_lines(head, trial))) + len(reserved) + 1 <= CONTEXT_CAP:
                    selection = trial
    lines, selected = _startup_lines(head, selection), sum(len(items) for items in selection.values())
    if legacy and len('\n'.join(lines + [legacy])) + len(reserved) + 1 <= CONTEXT_CAP:
        lines.append(legacy)
    if selected < count:
        lines.append(f"{count - selected} more items in {note}")
    if recent:
        lines.append(recent)
    return '\n'.join(lines)


def _startup_lines(head, selection):
    """Section headings in startup order, with compact source refs so each item takes less of the cap."""
    from . import status
    lines = list(head)
    for key in status.STARTUP_ORDER:
        if selection[key]:
            lines.append(f"### {status.SECTIONS[key][0]}")
            lines += [f"- {item.text} {_compact_ref(item.ref)}" for item in selection[key]]
    return lines


def _compact_ref(ref):
    """Session date and evidence anchor, e.g. (2026-10-17#e2). The project page keeps the full link."""
    from . import status
    date, anchor = status._ref_date(ref), re.search(r'#e\d+', ref)
    return f"({date}{anchor[0]})" if date and anchor else ref


def session_start(cfg, agent: str, sid: str, cwd: str) -> dict:
    """Record the start as a hook event (no capture is queued) and return the startup context, if any."""
    from .manifest import Manifest
    if sid:
        man = Manifest(cfg.manifest_path)
        try:
            man.record_event(agent, sid, "start")
        finally:
            man.close()
    ctx = startup_package(cfg, cwd)
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}} if ctx else {}


def main(agent: str) -> int:
    out = {}
    try:
        if os.environ.get(CAPTURE_ENV):
            return 0  # inside our own pipeline: never capture the capturer
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        from . import config, worker
        cfg = config.load()
        if cfg.paused():
            return 0
        event = payload.get("hook_event_name") or payload.get("hookEventName") or ""
        sid = payload.get("session_id") or payload.get("sessionId") or ""
        cwd = payload.get("cwd") or os.getcwd()
        src = payload.get("transcript_path") or ""
        if event == "SessionStart":
            out = session_start(cfg, agent, sid, cwd)
        elif sid and event in ("Stop", "PreCompact", "SessionEnd", "SubagentStop"):
            if event == "SubagentStop":
                return 0
            worker.enqueue(cfg, agent, sid, event="end" if event == "SessionEnd" else "turn", source=src, cwd=cwd)
        worker.spawn_detached()
    except Exception as e:  # noqa: BLE001 - a memory hook must never break an agent turn
        try:
            from . import config
            from .logutil import log
            log(config.load(), "hook-error", agent=agent, error=f"{type(e).__name__}: {e}")
        except Exception:  # noqa: BLE001
            pass
    finally:
        sys.stdout.write(json.dumps(out))
        sys.stdout.flush()
    return 0
