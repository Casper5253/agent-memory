"""Append-only JSONL operational log in _meta/logs. Never contains prompts, transcript text or secrets."""
import json
import os
import time

from .fsutil import file_lock


def log(cfg, event: str, **fields) -> None:
    path = cfg.log_path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "pid": os.getpid(), "event": event, **fields}
        line = json.dumps(rec, ensure_ascii=False, default=str) + "\n"
        with file_lock(path.with_suffix(".lock"), timeout=5):
            if path.exists() and path.stat().st_size > 5_000_000:
                os.replace(path, path.with_suffix(".1.jsonl"))
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception:  # noqa: BLE001 - logging must never break capture or an agent hook
        pass
