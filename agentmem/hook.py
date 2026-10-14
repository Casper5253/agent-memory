"""Lifecycle hook entry for Claude Code and Codex (same JSON contract). Must be fast and never fail the agent.

Stop / PreCompact -> enqueue "turn"; SessionEnd -> enqueue "end"; SessionStart -> small recall pointer.
Every event also kicks a detached worker. Output is always valid hook JSON ({} unless adding context).
"""
import json
import os
import sys

from .config import CAPTURE_ENV

CONTEXT_CAP = 1500


def _context(cfg, cwd: str) -> str:
    from . import notes, projects
    from .manifest import Manifest
    project = projects.resolve(cwd, cfg.projects_map, create=False)
    lines = [f"Agent memory: Obsidian vault at {cfg.vault}. Session notes (0-Sessions) are HISTORICAL; project notes (1-Projects) are CURRENT state."]
    lines.append("When asked what was previously decided, implemented or configured, search the vault first: "
                 '`mem search "<terms>"` (memory skill), never guess.')
    if project != projects.UNSORTED:
        note = cfg.projects_dir / project / f"{project}.md"
        lines.append(f"Project: {project} — {note}")
        if note.exists():
            try:
                state = notes.get_block(note.read_text(encoding="utf-8"), notes.STATE_BEGIN, notes.STATE_END)
                cur = state.split("### Active decisions")[0].replace("### Current state", "").strip()
                if cur and cur != "- none yet":
                    lines.append("Current state:\n" + cur[:600])
            except (ValueError, OSError):
                pass
        man = Manifest(cfg.manifest_path)
        try:
            recent = man.query_captures(project=project, limit=3)
        finally:
            man.close()
        if recent:
            lines.append("Recent sessions: " + "; ".join(f"{r['date']} {r['title']}" for r in recent))
    return "\n".join(lines)[:CONTEXT_CAP]


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
            ctx = _context(cfg, cwd)
            if ctx:
                out = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}}
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
