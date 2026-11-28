"""Queue + single-flight worker. Hooks only enqueue; the worker does everything else, then exits.

No daemon: every hook spawns a detached worker; if one is already running it takes the new jobs.
"""
import json
import math
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import pipeline, search
from .config import AGENTS, CAPTURE_ENV, Config
from .fsutil import atomic_write, try_lock
from .logutil import log
from .manifest import Manifest
from .pipeline import open_manifest
from .redact import redact


def snapshot(cfg: Config, agent: str, session_id: str, source: str) -> "Path | None":
    """Copy the transcript next to the queue so a later cleanup cannot take it.

    Runs on the end hook, while the agent still knows the path. The copy holds the
    transcript as the agent wrote it; capture redacts when it reads it.
    """
    path = Path(source)
    if not path.is_file():
        return None
    dest = cfg.snapshot_path(agent, session_id)
    if dest.resolve().is_relative_to(cfg.vault.resolve()):
        log(cfg, "snapshot-refused", agent=agent, session=session_id, reason="inside vault")
        return None
    try:
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = dest.with_suffix(".tmp")
        shutil.copyfile(path, tmp)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass  # Windows ignores most of the mode; never lose the copy over it
        os.replace(tmp, dest)
    except OSError as error:
        log(cfg, "snapshot-error", agent=agent, session=session_id, error=str(error))
        return None
    return dest


def enqueue(cfg: Config, agent: str, session_id: str, *, event: str, source: str = "", cwd: str = "") -> Path:
    """Queue one hook event, redacted before it touches disk and written atomically."""
    allow = cfg.privacy_email_allowlist
    job = {"agent": redact(agent, email_allowlist=allow),
           "session_id": redact(session_id, email_allowlist=allow),
           "event": redact(event, email_allowlist=allow),
           "source": redact(source, email_allowlist=allow),
           "cwd": redact(cwd, email_allowlist=allow),
           "ts": time.time()}
    name = f"{time.time_ns()}-{agent}-{uuid.uuid4().hex[:8]}.json"
    path = cfg.queue_dir / name
    atomic_write(path, json.dumps(job))
    if event == "end" and source:
        snapshot(cfg, agent, session_id, source)
    return path


def drain_queue(cfg: Config, man: Manifest) -> int:
    """Record queued events, removing an entry only after the manifest committed it.

    A kill at any point leaves the uncommitted entry in place. A replay after a
    kill between the commit and the removal finds its timestamp already in the
    event history and skips it, so one queue entry captures exactly once.
    """
    n = 0
    for path in sorted(cfg.queue_dir.glob("*.json")):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
            agent, session_id = job["agent"], job["session_id"]
            if agent not in AGENTS or not session_id:
                raise ValueError("job has no valid agent and session")
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError, ValueError, OSError) as e:
            log(cfg, "bad-job", job=path.name, error=str(e))
            path.unlink(missing_ok=True)
            continue
        event = job.get("event") or ""
        stamp = job.get("ts")
        if not isinstance(stamp, (int, float)) or isinstance(stamp, bool) or not math.isfinite(stamp):
            stamp = path.stat().st_mtime
        man.touch(agent, session_id, cwd=job.get("cwd") or None, source=job.get("source") or None,
                  end=event == "end", now=stamp)
        # The entry is still here because an earlier drain died; its committed
        # event is already in the history at this timestamp.
        if man.last_event_ts(agent, session_id, event) < stamp:
            man.record_event(agent, session_id, event, stamp)
        n += 1
        path.unlink(missing_ok=True)
    return n


def run(cfg: Config, budget_s: float = 900, summarizer=None, consolidator=None, index: bool = True) -> dict:
    """Drain + process until nothing is left. Returns a summary. Exits immediately if another worker runs."""
    lock = try_lock(cfg.locks_dir / "worker.lock")
    if lock is None:
        return {"status": "busy"}
    summary = {"status": "ok", "jobs": 0, "results": []}
    man = open_manifest(cfg)
    try:
        deadline = time.monotonic() + budget_s
        while time.monotonic() < deadline:
            if cfg.paused():
                summary["status"] = "paused"
                break
            jobs = drain_queue(cfg, man)
            results = pipeline.process_ready(cfg, man, deadline - time.monotonic(), summarizer, consolidator)
            summary["jobs"] += jobs
            summary["results"] += results
            held = pipeline.triage_held(cfg, man, consolidator=consolidator, limit=20)
            triaged = held["promoted"] + held["closed"]
            summary["results"] += [("held", "", "triage")] * triaged
            if not jobs and not results and not triaged:
                break
        if index and cfg.auto_index and any(r[2] == "captured" for r in summary["results"]):
            summary["index"] = search.update_index(cfg)
        summary["unsettled"] = man.db.execute(
            "SELECT count(*) FROM sessions WHERE status='pending' AND end_seen=0").fetchone()[0]
    finally:
        man.close()
        lock.__exit__(None, None, None)
    if summary["results"] or summary["jobs"]:
        log(cfg, "worker", jobs=summary["jobs"], results=len(summary["results"]), status=summary["status"])
    # A hook may have enqueued between our last drain and the unlock; its own worker saw "busy" and left.
    if summary["status"] == "ok" and any(cfg.queue_dir.glob("*.json")) and budget_s > 60:
        nested = run(cfg, budget_s / 2, summarizer, consolidator, index)
        summary["results"] += nested.get("results", [])
    return summary


def sleep_then_run(cfg: Config, delay_s: float) -> dict:
    """At most one sleeper waits out the settle window, then runs the worker (covers sessions with no end hook)."""
    lock = try_lock(cfg.locks_dir / "sleeper.lock")
    if lock is None:
        return {"status": "sleeper-exists"}
    try:
        time.sleep(delay_s)
    finally:
        lock.__exit__(None, None, None)
    result = run(cfg)
    if result.get("status") == "ok" and result.get("unsettled"):
        spawn_detached(["--delay", str(int(delay_s))])
    return result


def spawn_detached(extra_args=()) -> None:
    """Start `run.py worker` in the background without a console window; never blocks the hook."""
    runner = Path(__file__).resolve().parent.parent / "run.py"
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        exe = exe.with_name("python.exe")
    cmd = [str(exe), "-I", str(runner), "worker", *extra_args]
    env = dict(os.environ, **{CAPTURE_ENV: "1"})
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, close_fds=True)
    if os.name == "nt":
        # A hidden console, not none: a console-less worker makes every git, qmd and claude child open a window.
        kwargs["creationflags"] = 0x00000200 | 0x08000000  # NEW_PROCESS_GROUP | NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)
