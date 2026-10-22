"""Explicit memory: `/remember` and `/obsidian` both land here.

remember_item(): one stated fact / decision / preference / next step
  -> immutable evidence note in 0-Sessions (type: memory)
  -> reconciled into the project's current state (or 3-Resources/Remembered.md when no project applies)
capture_current(): flush the agent session that is running right now into the vault.
The project is routed automatically: --project, else cwd, else file locations / paths / names in the text.
"""
import json
import os
import shutil
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import notes, pipeline, projects
from .config import Config
from .fsutil import atomic_write, create_once, file_lock
from .logutil import log
from .manifest import Manifest
from .redact import redact, redact_session_note

KINDS = ("fact", "decision", "preference", "next")
REMEMBERED = ("3-Resources", "Remembered.md")
LIST_BEGIN, LIST_END = "<!-- agentmem:remembered:begin -->", "<!-- agentmem:remembered:end -->"


def _summary(text: str, kind: str, rationale: str) -> dict:
    s = {"title": f"Remembered: {notes.slug(text, 8).replace('-', ' ')}", "summary": text, "what_happened": [],
         "decisions": [], "facts": [], "open_questions": [], "next_steps": []}
    if kind == "decision":
        s["decisions"] = [{"decision": text, "rationale": rationale or "stated explicitly by the user"}]
    elif kind == "next":
        s["next_steps"] = [text]
    else:
        s["facts"] = [text if kind == "fact" else f"Preference: {text}"]
    return s


def _evidence_note(cfg: Config, *, when, agent, project, kind, text, rationale, item_id, plink, attribution) -> Path:
    path = cfg.sessions_dir / f"{when:%Y}" / f"{when:%m}" / f"{when:%Y-%m-%d-%H%M}-{agent}-remember-{notes.slug(text, 6)}-{item_id}.md"
    body = "\n".join([
        "---", "type: memory", f"agent: {agent}", f'session_id: "remember-{item_id}"', f'project: "{project}"',
        f"date: {when:%Y-%m-%d}", f"kind: {kind}", "immutable: true",
        f"attribution_method: {attribution.method}",
        "attribution_signals: " + json.dumps(attribution.signals, ensure_ascii=False, sort_keys=True),
        "related_projects: " + json.dumps(attribution.related_projects, ensure_ascii=False),
        "tags:", "  - memory", f"  - agent/{agent}",
        f"  - project/{notes.slug(project, 8)}", "---", "",
        f"# Remembered ({kind}): {text[:80]}", "",
        f"> Explicitly remembered on {when:%Y-%m-%d %H:%M} via `/remember`. Current state lives in {plink}.", "",
        "## Statement", "", text, "",
        *(["## Rationale", "", rationale, ""] if rationale else []),
    ])
    create_once(path, redact_session_note(body))
    return path


def _remembered_list(cfg: Config, line: str) -> Path:
    path = cfg.vault.joinpath(*REMEMBERED)
    with file_lock(cfg.locks_dir / "remembered.lock", timeout=cfg.lock_timeout_s):
        text = path.read_text(encoding="utf-8") if path.exists() else "\n".join([
            "---", "type: resource", "status: active", "tags:", "  - memory", "---", "", "# Remembered",
            "", "Things explicitly remembered that belong to no single project. Newest first.", "", LIST_BEGIN, LIST_END, ""])
        if LIST_BEGIN not in text:
            text = text.rstrip("\n") + f"\n\n{LIST_BEGIN}\n{LIST_END}\n"
        existing = notes.get_block(text, LIST_BEGIN, LIST_END).splitlines()
        atomic_write(path, notes.set_block(text, LIST_BEGIN, LIST_END, "\n".join([line] + [l for l in existing if l.strip()])))
    return path


def remember_item(cfg: Config, text: str, *, kind: str = "fact", project: str = "", cwd: str = "", agent: str = "user",
                  rationale: str = "", consolidator=None) -> dict:
    text, rationale = redact(" ".join(text.split())), redact(rationale.strip())
    if not text:
        raise ValueError("nothing to remember")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    attribution = projects.attribute(cwd or os.getcwd(), cfg.projects_map, text=text)
    if project:
        attribution.project, attribution.method = project, "pinned"
        attribution.related_projects = sorted(name for name, signal in attribution.signals.items()
                                              if name != project and signal["score"] >= 3)
    project = attribution.project
    when = datetime.now().astimezone()
    item_id = uuid.uuid4().hex[:8]
    plink = (f"[[{projects.note_path(cfg.vault, project).relative_to(cfg.vault).with_suffix('').as_posix()}|{project}]]"
             if project != projects.UNSORTED else f"[[{'/'.join(REMEMBERED)[:-3]}|Remembered]]")
    note = _evidence_note(cfg, when=when, agent=agent, project=project, kind=kind, text=text, rationale=rationale,
                          item_id=item_id, plink=plink, attribution=attribution)
    link = note.relative_to(cfg.vault).with_suffix("").as_posix()
    summary = _summary(text, kind, rationale)
    consolidated, cost, target = False, 0.0, None
    if project != projects.UNSORTED:
        kind_for_merge = "claude-haiku" if cfg.summarizer == "claude-haiku" else "deterministic"
        cost, consolidated = pipeline.consolidate_into_project(cfg, project, summary, link, when, kind_for_merge, consolidator)
        target = projects.note_path(cfg.vault, project)
    else:
        target = _remembered_list(cfg, f"- {when:%Y-%m-%d} ({kind}) {text} ([[{link}|source]])")
        consolidated = True
    man = Manifest(cfg.manifest_path)
    try:
        man.record_capture(agent=agent, session_id=f"remember-{item_id}", part=1, note_path=note.relative_to(cfg.vault).as_posix(),
                           project=project, title=summary["title"], date=f"{when:%Y-%m-%d}", start=0, end=0,
                           summarizer="remember", cost=cost, sha="", consolidated=consolidated,
                           summary_json=json.dumps(summary, ensure_ascii=False))
    finally:
        man.close()
    log(cfg, "remembered", agent=agent, project=project, kind=kind, note=link, consolidated=consolidated)
    return {"project": project, "evidence": str(note), "updated": str(target), "consolidated": consolidated}


# ------------------------------------------------------------------------- current session
def current_session(agent: str = "", cwd: str = "") -> "tuple[str, str, str] | None":
    """(agent, session_id, transcript) for the agent session running this command, best effort."""
    cwd = cwd or os.getcwd()
    if (agent in ("", "claude")) and os.environ.get("CLAUDE_CODE_SESSION_ID"):
        sid = os.environ["CLAUDE_CODE_SESSION_ID"]
        hits = sorted((Path.home() / ".claude" / "projects").glob(f"*/{sid}.jsonl"), key=lambda p: p.stat().st_mtime)
        if hits:
            return "claude", sid, str(hits[-1])
    if (agent in ("", "codex")) and os.environ.get("CODEX_THREAD_ID"):
        from .adapters import find_codex_transcript
        sid = os.environ["CODEX_THREAD_ID"]
        return "codex", sid, find_codex_transcript(sid) or ""
    if agent in ("", "opencode") and shutil.which("opencode"):
        try:
            out = subprocess.run([shutil.which("opencode"), "session", "list", "--format", "json", "-n", "5", "--pure"],
                                 capture_output=True, timeout=60, cwd=cwd)
            rows = json.loads(out.stdout.decode("utf-8", "replace") or "[]")
            rows = [r for r in rows if os.path.normcase(r.get("directory", "")) == os.path.normcase(cwd)] or rows
            if rows and (agent == "opencode" or time.time() * 1000 - rows[0].get("updated", 0) < 15 * 60_000):
                return "opencode", rows[0]["id"], ""
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
            pass
    return None


def capture_current(cfg: Config, agent: str = "", cwd: str = "", budget_s: float = 600) -> dict:
    found = current_session(agent, cwd)
    if not found:
        return {"status": "no-current-session", "hint": "run inside a Claude Code, Codex or OpenCode session, or pass --agent/--session"}
    agent, sid, transcript = found
    from . import worker
    man = Manifest(cfg.manifest_path)
    try:
        man.touch(agent, sid, cwd=cwd or os.getcwd(), source=transcript or None, end=True)
    finally:
        man.close()
    res = worker.run(cfg, budget_s=budget_s)
    man = Manifest(cfg.manifest_path)
    try:
        row = man.get(agent, sid)
        latest = man.capture(agent, sid, row["parts"]) if row and row["parts"] else None
    finally:
        man.close()
    return {"agent": agent, "session": sid, "status": row["status"] if row else "unknown",
            "skip_reason": row["skip_reason"] if row else None, "project": row["project"] if row else None,
            "note": str(cfg.vault / latest["note_path"]) if latest else None, "worker": res.get("status")}
