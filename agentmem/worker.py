"""Queue + single-flight worker. Hooks only enqueue; the worker does everything else, then exits.

No daemon: every hook spawns a detached worker; if one is already running it takes the new jobs.
"""
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import pipeline, search
from .config import CAPTURE_ENV, Config
from .fsutil import atomic_write, try_lock
from .logutil import log
from .manifest import Manifest


def enqueue(cfg: Config, agent: str, session_id: str, *, event: str, source: str = "", cwd: str = "") -> Path:
    job = {"agent": agent, "session_id": session_id, "event": event, "source": source, "cwd": cwd, "ts": time.time()}
    name = f"{time.time_ns()}-{agent}-{uuid.uuid4().hex[:8]}.json"
    path = cfg.queue_dir / name
    atomic_write(path, json.dumps(job))
    return path


def drain_queue(cfg: Config, man: Manifest) -> int:
    n = 0
    for path in sorted(cfg.queue_dir.glob("*.json")):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
            if job.get("agent") in ("claude", "codex", "opencode") and job.get("session_id"):
                man.touch(job["agent"], job["session_id"], cwd=job.get("cwd") or None, source=job.get("source") or None,
                          end=job.get("event") == "end", now=job.get("ts"))
                man.record_event(job["agent"], job["session_id"], job.get("event") or "", job.get("ts"))
                n += 1
        except (json.JSONDecodeError, OSError) as e:
            log(cfg, "bad-job", job=path.name, error=str(e))
        path.unlink(missing_ok=True)
    return n


def run(cfg: Config, budget_s: float = 900, summarizer=None, consolidator=None, index: bool = True) -> dict:
    """Drain + process until nothing is left. Returns a summary. Exits immediately if another worker runs."""
    lock = try_lock(cfg.locks_dir / "worker.lock")
    if lock is None:
        return {"status": "busy"}
    summary = {"status": "ok", "jobs": 0, "results": []}
    man = Manifest(cfg.manifest_path)
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
            if not jobs and not results:
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
