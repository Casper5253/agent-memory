"""Lifecycle hook entry for Claude Code and Codex (same JSON contract). Must be fast and never fail the agent.

Stop / PreCompact -> enqueue "turn"; SessionEnd -> enqueue "end"; SessionStart -> small recall pointer.
Every event also kicks a detached worker. Output is always valid hook JSON ({} unless adding context).
"""
import json
import os
import subprocess
import sys

from .config import CAPTURE_ENV

CONTEXT_CAP = 1500
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
    if backlog:
        import re
        reason = re.search(r'status update pending \(([^;\n)]+)', pending)
        why = reason[1][:100] if reason else 'reconciliation pending'
        parts.append(f"{_plural(backlog, 'captured session')} awaiting consolidation ({why}); see {note or 'project notes'}")
    elif 'status update pending' in pending:
        parts.append(f"status update pending; see {note}")
    return f"Capture health: {', '.join(parts)}" if parts else ""


def startup_package(cfg, cwd: str) -> str:
    """Reserve health and omission pointer first, then admit complete status items in priority order."""
    from . import notes, projects, status
    from .manifest import Manifest
    project = projects.resolve_session(cwd, cfg.projects_map, create=False)
    head = [f"Agent memory: Obsidian vault at {cfg.vault}. Session notes (0-Sessions) are HISTORICAL; project notes (1-Projects) are CURRENT state.",
            'When asked what was previously decided, implemented or configured, search the vault first: '
            '`mem search "<terms>"` (memory skill), never guess.']
    page, pending, note = status.empty(), '', None
    man = Manifest(cfg.manifest_path)
    try:
        if project != projects.UNSORTED:
            note = projects.note_path(cfg.vault, project)
            head.append(f"Project: {project} - {note}")
            if note.exists():
                try:
                    text = note.read_text(encoding='utf-8')
                    pending = notes.get_block(text, notes.PENDING_BEGIN, notes.PENDING_END)
                    page = status.live_page(notes.get_block(text, notes.STATE_BEGIN, notes.STATE_END))
                except (ValueError, OSError):
                    pending += ' status update pending (page not migrated)'
        head += [line for line in (_git_line(cwd), _health_line(man, cfg.max_attempts, note, pending)) if line]
    finally:
        man.close()
    count = sum(len(items) for items in page.values())
    pointer = f"{count} more items in {note}" if count else ''
    fixed = '\n'.join(head)
    # Abnormally long configured paths must not consume the health line or omission pointer.
    if len(fixed) + len(pointer) + 1 > CONTEXT_CAP:
        health = next((line for line in head if line.startswith('Capture health:')), '')
        priority = '\n'.join(line for line in (health, pointer) if line)
        room = max(0, CONTEXT_CAP - len(priority) - 1)
        return ('\n'.join(head[:3])[:room] + '\n' + priority)[-CONTEXT_CAP:]
    lines, selected = list(head), 0
    for key in status.STARTUP_ORDER:
        heading = f"### {status.SECTIONS[key][0]}"
        emitted_heading = False
        for item in page[key]:
            if len(item.text) > 280:
                continue
            addition = ([heading] if not emitted_heading else []) + [f"- {item.text} {item.ref}"]
            candidate = '\n'.join(lines + addition)
            if len(candidate) + len(pointer) + 1 <= CONTEXT_CAP:
                lines += addition
                selected += 1
                emitted_heading = True
    if selected < count:
        lines.append(f"{count - selected} more items in {note}")
    return '\n'.join(lines)


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
