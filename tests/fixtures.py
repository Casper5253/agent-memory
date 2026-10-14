"""Synthetic transcripts for all three agents, plus fake model functions. No network, no real agents."""
import json
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentmem.config import Config
from agentmem.llm import LLMError

SECRET_SAMPLES = [
    "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdefGHIJ",
    "AKIAIOSFODNN7EXAMPLE",
    "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789",
]


def make_vault(tmp: Path, **overrides) -> Config:
    vault = tmp / "vault"
    for d in ("_meta/queue", "_meta/locks", "_meta/logs", "0-Sessions", "1-Projects", "2-Areas", "3-Resources", "4-Archive"):
        (vault / d).mkdir(parents=True, exist_ok=True)
    work = tmp / "work"
    (work / "demo-app").mkdir(parents=True, exist_ok=True)
    (vault / "_meta" / "projects.json").write_text(json.dumps({"projects": [
        {"name": "Demo App", "roots": [str(work / "demo-app")], "status": "active"}]}), encoding="utf-8")
    opts = dict(summarizer="claude-haiku", settle_minutes=20, auto_index=False, embeddings=False)
    opts.update(overrides)
    return Config(vault=vault, **opts)


def ts(minutes_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds").replace("+00:00", "Z")


# ------------------------------------------------------------------------- Claude Code JSONL
def claude_lines(sid, cwd, turns, minutes_ago=30):
    """turns: list of (prompt, answer, [edited files], [commands])."""
    out = []
    t = minutes_ago
    for prompt, answer, files, cmds in turns:
        out.append({"type": "user", "sessionId": sid, "cwd": cwd, "timestamp": ts(t), "message": {"role": "user", "content": prompt}})
        blocks = [{"type": "text", "text": answer}]
        blocks += [{"type": "tool_use", "id": f"t{i}", "name": "Edit", "input": {"file_path": f"{cwd}/{f}", "old_string": "a", "new_string": "b"}}
                   for i, f in enumerate(files)]
        blocks += [{"type": "tool_use", "id": f"c{i}", "name": "Bash", "input": {"command": c}} for i, c in enumerate(cmds)]
        out.append({"type": "assistant", "sessionId": sid, "cwd": cwd, "timestamp": ts(t - 1), "message": {"role": "assistant", "content": blocks}})
        out.append({"type": "user", "sessionId": sid, "cwd": cwd, "timestamp": ts(t - 1),
                    "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t0", "content": f"output {SECRET_SAMPLES[0]}"}]}})
        t -= 2
    return out


def write_jsonl(path: Path, records, append=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a" if append else "w", encoding="utf-8", newline="\n") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


# ------------------------------------------------------------------------- Codex rollout JSONL
def codex_lines(sid, cwd, turns, minutes_ago=30):
    out = [{"type": "session_meta", "timestamp": ts(minutes_ago), "payload": {"id": sid, "cwd": cwd}},
           {"type": "response_item", "timestamp": ts(minutes_ago), "payload": {"type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "# AGENTS.md instructions\n<INSTRUCTIONS>injected</INSTRUCTIONS>"}]}}]
    t = minutes_ago
    for prompt, answer, files, cmds in turns:
        out.append({"type": "event_msg", "timestamp": ts(t), "payload": {"type": "item_completed", "item": {
            "type": "UserMessage", "content": [{"type": "text", "text": prompt}]}}})
        for c in cmds:
            out.append({"type": "event_msg", "timestamp": ts(t), "payload": {"type": "item_completed", "item": {
                "type": "CommandExecution", "command": ["powershell.exe", "-Command", c], "cwd": "file:///" + cwd.replace("\\", "/")}}})
        if files:
            out.append({"type": "event_msg", "timestamp": ts(t), "payload": {"type": "item_completed", "item": {
                "type": "FileChange", "changes": {f"{cwd}\\{f}": {"type": "update"} for f in files}}}})
        out.append({"type": "event_msg", "timestamp": ts(t - 1), "payload": {"type": "item_completed", "item": {
            "type": "AgentMessage", "content": [{"type": "Text", "text": answer}], "phase": "final_answer"}}})
        out.append({"type": "event_msg", "timestamp": ts(t - 1), "payload": {"type": "task_complete"}})
        t -= 2
    return out


# ------------------------------------------------------------------------- OpenCode export JSON
def opencode_export(sid, cwd, turns):
    msgs = []
    base = int(time.time() * 1000) - 30 * 60_000
    for i, (prompt, answer, files, cmds) in enumerate(turns):
        msgs.append({"info": {"id": f"u{i}", "role": "user", "time": {"created": base + i * 60_000}},
                     "parts": [{"type": "text", "text": prompt}]})
        parts = [{"type": "reasoning", "text": "thinking"}]
        parts += [{"type": "tool", "tool": "bash", "state": {"input": {"command": c}, "output": SECRET_SAMPLES[1]}} for c in cmds]
        parts += [{"type": "tool", "tool": "edit", "state": {"input": {"filePath": f"{cwd}/{f}"}}} for f in files]
        parts += [{"type": "text", "text": answer}]
        msgs.append({"info": {"id": f"a{i}", "role": "assistant", "time": {"created": base + i * 60_000 + 30_000}}, "parts": parts})
    return {"info": {"id": sid, "directory": cwd, "title": "OpenCode demo"}, "messages": msgs}


SUBSTANTIAL = [
    ("Set up storage for the demo app. Use SQLite for storage, it is local only.",
     "Done. We decided to use SQLite for storage because the app is local only. Created db.py and wired migrations. " * 5,
     ["db.py", "migrations.py"], ["pytest -q", "python -m app"]),
    ("Add tests for the db layer and run them.",
     "Added tests/test_db.py; all 12 tests pass. " * 10, ["tests/test_db.py"], ["pytest -q"]),
]
SWITCH = [
    ("Multi-user is now required. Switch storage to Postgres instead of SQLite.",
     "Switched storage to Postgres instead of SQLite because multi-user needs concurrent writers. Updated db.py. " * 5,
     ["db.py", "docker-compose.yml"], ["pytest -q"]),
    ("Run the full suite against Postgres.", "All tests pass against Postgres. " * 20, ["tests/conftest.py"], ["pytest -q"]),
]
TRIVIAL = [("what time is it?", "It is noon.", [], [])]


# ------------------------------------------------------------------------- fake model functions
def fake_summarize(digest: str, timeout=0) -> dict:
    decisions = []
    if "Postgres instead of SQLite" in digest:
        decisions.append({"decision": "Switch storage to Postgres instead of SQLite", "rationale": "multi-user needs concurrent writers"})
    elif "use SQLite for storage" in digest:
        decisions.append({"decision": "Use SQLite for storage", "rationale": "the app is local only"})
    first = re.search(r"--- USER 1:\n(.+)", digest)
    return {"title": (first.group(1)[:40] if first else "session"), "summary": "Fake summary of the session.",
            "what_happened": ["did work"], "decisions": decisions, "facts": ["tests pass"],
            "open_questions": [], "next_steps": ["deploy"], "_cost_usd": 0.001}


def failing_llm(*a, **k):
    raise LLMError("model unavailable (simulated)")


def fake_consolidate(state: str, summary: dict, link: str, date: str, timeout=0):
    def section(name, nxt):
        m = re.search(rf"### {name}\n(.*?)(?=\n### {nxt}|\Z)", state or "", re.S)
        return [l for l in (m.group(1).splitlines() if m else []) if l.strip().startswith("-") and l.strip() != "- none"]
    active = section("Active decisions", "Superseded decisions")
    superseded = section("Superseded decisions", "Open questions and next steps")
    for d in summary.get("decisions", []):
        m = re.search(r"instead of (\w+)", d["decision"])
        if m:
            for a in list(active):
                if m.group(1).lower() in a.lower():
                    active.remove(a)
                    superseded.append(f"{a} → superseded {date}: {d['decision']}")
        active.append(f"- {date} — {d['decision']} — {d['rationale']} ({link})")
    current = "- " + (summary["decisions"][-1]["decision"] if summary.get("decisions") else "work in progress")
    md = (f"### Current state\n{current}\n\n### Active decisions\n" + ("\n".join(active) or "- none") +
          "\n\n### Superseded decisions\n" + ("\n".join(superseded) or "- none") +
          "\n\n### Open questions and next steps\n- deploy")
    return md, 0.002


def tmpdir():
    return tempfile.TemporaryDirectory(prefix="agentmem-test-")
