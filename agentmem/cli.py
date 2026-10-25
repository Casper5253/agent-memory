"""`mem` — retrieval and operations CLI shared by every agent.

Retrieval:  search, sessions, note, projects
Operations: stats, index, capture, consolidate, retry, pause, resume, doctor
Plumbing:   hook, enqueue, worker
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

from . import config as config_mod


def _cfg(args):
    return config_mod.load(getattr(args, "vault", None))


def _rel(cfg, p) -> str:
    try:
        return Path(p).resolve().relative_to(cfg.vault.resolve()).as_posix()
    except ValueError:
        return str(p)


# ----------------------------------------------------------------------------- retrieval
def cmd_search(args):
    from .search import search
    cfg = _cfg(args)
    layers = {"current": ["CURRENT"], "history": ["HISTORICAL"], "reference": ["REFERENCE"], "archive": ["ARCHIVED"]}.get(args.layer)
    hits = search(cfg, " ".join(args.terms), n=args.n, layers=layers, semantic=args.semantic)
    if args.json:
        print(json.dumps(hits, indent=2, ensure_ascii=False))
        return 0
    if not hits:
        print("no matches")
        return 1
    if hits and hits[0].get("fallback"):
        print(f"(qmd unavailable, bounded scan used: {hits[0]['fallback']})")
    for h in hits:
        loc = f"{h['path']}" + (f":{h['line']}" if h.get("line") else "")
        print(f"[{h['label']}] {h['title'] or Path(h['path']).stem}  (score {h['score']})\n  {loc}\n  {h['snippet']}\n")
    print("CURRENT = reconciled project state (authoritative now). HISTORICAL = what a past session did/decided; may be superseded.")
    return 0


def cmd_sessions(args):
    from .manifest import Manifest
    cfg = _cfg(args)
    man = Manifest(cfg.manifest_path)
    try:
        rows = man.query_captures(project=args.project, agent=args.agent, since=args.since, until=args.until, limit=args.n)
    finally:
        man.close()
    if args.json:
        print(json.dumps([dict(r) for r in rows], indent=2, default=str))
        return 0
    for r in rows:
        print(f"{r['date']}  {r['agent']:<8} {str(r['project'])[:28]:<28} {r['title'][:60]}\n            {r['note_path']}")
    if not rows:
        print("no sessions match")
    return 0


def _find_note(cfg, name: str) -> "Path | None":
    p = Path(name)
    for cand in (p, cfg.vault / name, cfg.vault / f"{name}.md"):
        if cand.is_file():
            return cand
    stem = Path(name).stem.lower()
    best = None
    for f in cfg.vault.rglob("*.md"):
        if "_meta" in f.parts or ".obsidian" in f.parts:
            continue
        s = f.stem.lower()
        if s == stem:
            return f
        if stem in s and (best is None or len(f.stem) < len(best.stem)):
            best = f
    return best


def cmd_note(args):
    cfg = _cfg(args)
    path = _find_note(cfg, args.name)
    if not path:
        print(f"no note matching {args.name!r}", file=sys.stderr)
        return 1
    text = path.read_text(encoding="utf-8")
    if args.section:
        m = re.search(rf"(?ms)^(#{{1,4}}) [^\n]*{re.escape(args.section)}[^\n]*\n(.*?)(?=^\1 |\Z)", text, re.I)
        if not m:
            print(f"section {args.section!r} not found in {path}", file=sys.stderr)
            return 1
        text = m.group(0)
    print(f"# source: {path}\n# layer: {_layer(cfg, path)}\n")
    print(text)
    return 0


def _layer(cfg, path: Path) -> str:
    from .search import _label
    return _label(_rel(cfg, path))


def cmd_projects(args):
    from . import notes
    from .manifest import Manifest
    from .projects import load_map
    cfg = _cfg(args)
    data = load_map(cfg.projects_map)
    man = Manifest(cfg.manifest_path)
    try:
        for proj in sorted(data["projects"], key=lambda p: p["name"].lower()):
            if proj.get("status") != "active" and not args.all:
                continue
            from .projects import note_path
            note = note_path(cfg.vault, proj["name"])
            last = man.query_captures(project=proj["name"], limit=1)
            pend = 0
            state = ""
            if note.exists():
                text = note.read_text(encoding="utf-8")
                try:
                    state = notes.get_block(text, notes.STATE_BEGIN, notes.STATE_END).split("### Active decisions")[0]
                    state = state.replace("### Current state", "").strip()
                    pend = len(notes.pending_lines(note, text))
                except ValueError:
                    state = "(no managed state block)"
            print(f"## {proj['name']}  [{proj.get('status','active')}]  last session: {last[0]['date'] if last else '-'}"
                  f"{f'  pending reconciliation: {pend}' if pend else ''}\n   {note}")
            for line in state.splitlines()[: args.lines]:
                print(f"   {line}")
            print()
    finally:
        man.close()
    return 0


# ----------------------------------------------------------------------------- operations
def cmd_stats(args):
    from .manifest import Manifest
    from .search import qmd_cmd, _qmd
    cfg = _cfg(args)
    man = Manifest(cfg.manifest_path)
    try:
        s = man.stats()
    finally:
        man.close()
    s["paused"] = cfg.paused()
    s["queue"] = len(list(cfg.queue_dir.glob("*.json")))
    s["vault"] = str(cfg.vault)
    s["summarizer"] = cfg.summarizer
    if s["last_capture"]:
        s["last_capture"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["last_capture"]))
    if qmd_cmd():
        try:
            out = _qmd(["status"], timeout=60, index=cfg.qmd_index).stdout.decode("utf-8", "replace")
            s["qmd"] = {k: v.strip() for k, v in re.findall(r"(?m)^\s*(Total|Vectors|Size):\s*(.+)$", out)}
        except Exception as e:  # noqa: BLE001
            s["qmd"] = f"error: {e}"
    else:
        s["qmd"] = "not installed"
    print(json.dumps(s, indent=2, default=str))
    return 0


def cmd_index(args):
    from .search import ensure_collections, update_index
    cfg = _cfg(args)
    made = ensure_collections(cfg)
    res = update_index(cfg, embed=not args.no_embed)
    print(json.dumps({"collections_added": made, **res}))
    return 0 if not res.get("error") else 1


def cmd_remember(args):
    from .remember import capture_current, remember_item
    cfg = _cfg(args)
    text = " ".join(args.text).strip()
    out = {}
    if text:
        out["remembered"] = remember_item(cfg, text, kind=args.kind, project=args.project or "", cwd=args.cwd or "",
                                          agent=args.agent or _detect_agent(), rationale=args.why or "")
    if args.session or not text:
        out["session"] = capture_current(cfg, agent=args.agent or "", cwd=args.cwd or "")
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def _detect_agent() -> str:
    if os.environ.get("CLAUDE_CODE_SESSION_ID"):
        return "claude"
    if os.environ.get("CODEX_THREAD_ID"):
        return "codex"
    return "opencode" if os.environ.get("OPENCODE") or os.environ.get("OPENCODE_SESSION") else "user"


def cmd_capture(args):
    from . import worker
    from .manifest import Manifest
    cfg = _cfg(args)
    if args.current:
        from .remember import capture_current
        print(json.dumps(capture_current(cfg, agent=args.agent if args.agent_given else "", cwd=args.cwd or "",
                                         budget_s=args.budget), indent=2))
        return 0
    if args.session:
        man = Manifest(cfg.manifest_path)
        try:
            man.touch(args.agent, args.session, cwd=args.cwd, source=args.transcript, end=True)
        finally:
            man.close()
    res = worker.run(cfg, budget_s=args.budget)
    print(json.dumps(res, indent=2, default=str))
    return 0


def cmd_consolidate(args):
    from .manifest import Manifest
    from .pipeline import consolidate_pending
    cfg = _cfg(args)
    man = Manifest(cfg.manifest_path)
    try:
        print(json.dumps(consolidate_pending(cfg, man, limit=args.limit)))
    finally:
        man.close()
    return 0


def cmd_retry(args):
    from . import worker
    from .manifest import Manifest
    cfg = _cfg(args)
    man = Manifest(cfg.manifest_path)
    try:
        with man.tx() as db:
            sql = "UPDATE sessions SET status='pending', attempts=0, next_try=0, end_seen=1 WHERE status='error'"
            params = ()
            if args.session:
                sql += " AND session_id=?"
                params = (args.session,)
            n = db.execute(sql, params).rowcount
    finally:
        man.close()
    print(f"reset {n} failed session(s)")
    print(json.dumps(worker.run(cfg), default=str))
    return 0


def cmd_pause(args):
    cfg = _cfg(args)
    from .fsutil import atomic_write
    atomic_write(cfg.pause_file, f"paused {time.strftime('%Y-%m-%d %H:%M')}\n")
    print(f"capture PAUSED ({cfg.pause_file}). Hooks record nothing until `mem resume`.")
    return 0


def cmd_resume(args):
    cfg = _cfg(args)
    cfg.pause_file.unlink(missing_ok=True)
    print("capture resumed")
    return 0


def cmd_doctor(args):
    import shutil
    from .search import qmd_cmd
    cfg = _cfg(args)
    checks = {
        "vault": cfg.vault.is_dir(),
        "manifest_writable": os.access(cfg.meta, os.W_OK),
        "claude_cli": bool(shutil.which("claude")),
        "opencode_cli": bool(shutil.which("opencode")),
        "qmd": bool(qmd_cmd()),
        "paused": cfg.paused(),
        "stale_locks": [p.name for p in cfg.locks_dir.glob("*.lock") if time.time() - p.stat().st_mtime > 86400],
        "queue_backlog": len(list(cfg.queue_dir.glob("*.json"))),
    }
    print(json.dumps(checks, indent=2))
    return 0 if checks["vault"] and checks["manifest_writable"] else 1


# ----------------------------------------------------------------------------- plumbing
def cmd_hook(args):
    from .hook import main
    return main(args.agent)


def cmd_enqueue(args):
    if os.environ.get(config_mod.CAPTURE_ENV):
        return 0
    from . import worker
    cfg = _cfg(args)
    if cfg.paused():
        return 0
    worker.enqueue(cfg, args.agent, args.session, event=args.event, source=args.transcript or "", cwd=args.cwd or "")
    worker.spawn_detached()
    return 0


def cmd_worker(args):
    from . import worker
    cfg = _cfg(args)
    if args.delay:
        worker.sleep_then_run(cfg, args.delay)
        return 0
    res = worker.run(cfg)
    if res.get("status") == "ok" and res.get("unsettled"):
        worker.spawn_detached(["--delay", str(cfg.settle_minutes * 60 + 60)])
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="mem", description="Agent memory: search and maintain the shared Obsidian vault.")
    p.add_argument("--vault", help="override OBSIDIAN_VAULT_PATH")
    sub = p.add_subparsers(dest="cmd", required=True)
    from .knowledge.cli import register
    register(sub)

    s = sub.add_parser("search", help="ranked full-text search with excerpts and source paths")
    s.add_argument("terms", nargs="+")
    s.add_argument("-n", type=int, default=8)
    s.add_argument("--layer", choices=["current", "history", "reference", "archive"])
    s.add_argument("--semantic", action="store_true", help="vector search (needs embeddings)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("sessions", help="list captured sessions by project / agent / date")
    s.add_argument("--project"), s.add_argument("--agent"), s.add_argument("--since"), s.add_argument("--until")
    s.add_argument("-n", type=int, default=20)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_sessions)

    s = sub.add_parser("note", help="print a note (or one section) by path, name or partial name")
    s.add_argument("name")
    s.add_argument("--section")
    s.set_defaults(fn=cmd_note)

    s = sub.add_parser("projects", help="active projects and their current state")
    s.add_argument("--all", action="store_true")
    s.add_argument("--lines", type=int, default=6)
    s.set_defaults(fn=cmd_projects)

    sub.add_parser("stats", help="capture and index health").set_defaults(fn=cmd_stats)

    s = sub.add_parser("index", help="create collections if needed and update the QMD index")
    s.add_argument("--no-embed", action="store_true")
    s.set_defaults(fn=cmd_index)

    s = sub.add_parser("capture", help="capture now: a given session, or everything ready")
    s.add_argument("--agent", choices=["claude", "codex", "opencode"])
    s.add_argument("--session"), s.add_argument("--transcript"), s.add_argument("--cwd")
    s.add_argument("--current", action="store_true", help="capture the agent session running this command, now")
    s.add_argument("--budget", type=float, default=900)
    s.set_defaults(fn=cmd_capture)

    s = sub.add_parser("remember", help="record something now (routed to the right project); no text = sync this session")
    s.add_argument("text", nargs="*")
    s.add_argument("--kind", choices=["fact", "decision", "preference", "next"], default="fact")
    s.add_argument("--why", help="rationale (for decisions)")
    s.add_argument("--project", help="override automatic routing")
    s.add_argument("--session", action="store_true", help="also sync the current session")
    s.add_argument("--agent", choices=["claude", "codex", "opencode"])
    s.add_argument("--cwd")
    s.set_defaults(fn=cmd_remember)

    s = sub.add_parser("consolidate", help="retry reconciliation of pending captures")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(fn=cmd_consolidate)

    s = sub.add_parser("retry", help="reset failed captures and run the worker")
    s.add_argument("--session")
    s.set_defaults(fn=cmd_retry)

    sub.add_parser("pause", help="stop all capture (PAUSE switch)").set_defaults(fn=cmd_pause)
    sub.add_parser("resume", help="resume capture").set_defaults(fn=cmd_resume)
    sub.add_parser("doctor", help="health checks").set_defaults(fn=cmd_doctor)

    s = sub.add_parser("hook", help=argparse.SUPPRESS)
    s.add_argument("agent", choices=["claude", "codex"])
    s.set_defaults(fn=cmd_hook)

    s = sub.add_parser("enqueue", help=argparse.SUPPRESS)
    s.add_argument("--agent", required=True, choices=["claude", "codex", "opencode"])
    s.add_argument("--session", required=True)
    s.add_argument("--event", default="turn", choices=["turn", "end"])
    s.add_argument("--transcript"), s.add_argument("--cwd")
    s.set_defaults(fn=cmd_enqueue)

    s = sub.add_parser("worker", help=argparse.SUPPRESS)
    s.add_argument("--delay", type=float, default=0)
    s.set_defaults(fn=cmd_worker)

    args = p.parse_args(argv)
    if args.cmd == "capture":
        args.agent_given = args.agent is not None
        args.agent = args.agent or "claude"
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    return args.fn(args) or 0
