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


def _health_line(man, max_attempts: int) -> str:
    health = man.health(max_attempts)
    parts = []
    if health["pending"]:
        parts.append(f"{_plural(health['pending'], 'session')} not yet captured")
    if health["failed"]:
        reason = man.failed(max_attempts)[0]["last_error"] or "unknown error"
        parts.append(f"{health['failed']} failed: {reason[:160]}")
    return f"Capture health: {', '.join(parts)}" if parts else ""


def startup_package(cfg, cwd: str) -> str:
    """Bounded startup context: header, project, live Git, capture health, current state, recent sessions."""
    from . import notes, projects
    from .manifest import Manifest
    project = projects.resolve_session(cwd, cfg.projects_map, create=False)
    head = [f"Agent memory: Obsidian vault at {cfg.vault}. Session notes (0-Sessions) are HISTORICAL; project notes (1-Projects) are CURRENT state.",
            "When asked what was previously decided, implemented or configured, search the vault first: "
            '`mem search "<terms>"` (memory skill), never guess.']
    state_text, tail = "", []
    man = Manifest(cfg.manifest_path)
    try:
        if project != projects.UNSORTED:
            note = projects.note_path(cfg.vault, project)
            head.append(f"Project: {project} — {note}")
            if note.exists():
                try:
                    state = notes.get_block(note.read_text(encoding="utf-8"), notes.STATE_BEGIN, notes.STATE_END)
                    cur = state.split("### Active decisions")[0].replace("### Current state", "").strip()
                    if cur and cur != "- none yet":
                        state_text = "Current state:\n" + cur[:STATE_CAP]
                except (ValueError, OSError):
                    pass
            recent = man.query_captures(project=project, limit=3)
            if recent:
                tail.append("Recent sessions: " + "; ".join(f"{r['date']} {r['title']}" for r in recent))
        head += [line for line in (_git_line(cwd), _health_line(man, cfg.max_attempts)) if line]
    finally:
        man.close()
    fixed = "\n".join([*head, *tail])
    room = CONTEXT_CAP - len(fixed) - 1
    if state_text and room > len("Current state:\n"):
        return "\n".join([*head, state_text[:room], *tail])[:CONTEXT_CAP]
    return fixed[:CONTEXT_CAP]


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
