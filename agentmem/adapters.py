"""Agent transcript adapters -> one normalized event stream.

Each adapter reads only the range after `start` (byte offset for JSONL transcripts, message index for
OpenCode) and returns the new end position, so a resumed session is never re-captured.
Tool *outputs* are never kept: they are where secrets and bulk data live, and the digest doesn't need them.
"""
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from .config import CAPTURE_ENV

WRITE_TOOLS = {"edit", "write", "multiedit", "notebookedit", "apply_patch", "patch"}
_INJECTED = re.compile(r"^\s*(?:<environment_context>|<user_instructions>|# AGENTS\.md instructions|<INSTRUCTIONS>|"
                       r"<local-command-|<command-name>|<command-message>|<system-reminder>|Caveat: The messages below)", re.S)
_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_PATCH_FILE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", re.M)


@dataclass
class Event:
    role: str                 # user | assistant
    kind: str                 # text | command | edit | tool
    text: str = ""
    tool: str = ""
    paths: list = field(default_factory=list)
    ts: str = ""
    final: bool = False       # assistant message that ended a turn


@dataclass
class Transcript:
    agent: str
    session_id: str
    source: str               # transcript path or "opencode:<id>"
    cwd: str = ""
    title: str = ""
    started: str = ""
    ended: str = ""
    start: int = 0
    end: int = 0
    events: list = field(default_factory=list)


def _iso(value) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, timezone.utc).isoformat(timespec="seconds")
    return str(value)


def _clean_prompt(text: str) -> str:
    text = _REMINDER.sub("", text or "").strip()
    return "" if not text or _INJECTED.match(text) else text


def _read_lines(path: Path, start: int):
    """Yield (record, end_offset) for complete newline-terminated JSON lines after `start`."""
    with open(path, "rb") as fh:
        fh.seek(start)
        pos = start
        for raw in fh:
            if not raw.endswith(b"\n"):
                break  # partial line still being written: stop, it will be read next time
            pos += len(raw)
            try:
                yield json.loads(raw), pos
            except json.JSONDecodeError:
                yield None, pos


# --------------------------------------------------------------------------- Claude Code
def read_claude(path: str, start: int = 0) -> Transcript:
    p = Path(path)
    t = Transcript(agent="claude", session_id=p.stem, source=str(p), start=start, end=start)
    for rec, pos in _read_lines(p, start):
        t.end = pos
        if not rec or rec.get("isSidechain"):
            continue
        typ = rec.get("type")
        ts = rec.get("timestamp", "")
        if rec.get("sessionId"):
            t.session_id = rec["sessionId"]
        if rec.get("cwd"):
            t.cwd = rec["cwd"]
        if typ == "ai-title" and rec.get("aiTitle"):
            t.title = rec["aiTitle"]
        if typ not in ("user", "assistant") or rec.get("isMeta"):
            continue
        t.started = t.started or ts
        t.ended = ts or t.ended
        content = (rec.get("message") or {}).get("content")
        if typ == "user":
            if isinstance(content, str):
                text = _clean_prompt(content)
                if text:
                    t.events.append(Event("user", "text", text, ts=ts))
            elif isinstance(content, list):
                for b in content:
                    if b.get("type") == "text":
                        text = _clean_prompt(b.get("text", ""))
                        if text:
                            t.events.append(Event("user", "text", text, ts=ts))
            continue
        blocks = content if isinstance(content, list) else []
        for b in blocks:
            bt = b.get("type")
            if bt == "text" and b.get("text", "").strip():
                t.events.append(Event("assistant", "text", b["text"].strip(), ts=ts))
            elif bt == "tool_use":
                name = b.get("name", "")
                inp = b.get("input") or {}
                low = name.lower()
                if low in ("bash", "powershell"):
                    t.events.append(Event("assistant", "command", inp.get("command", ""), tool=name, ts=ts))
                elif low in WRITE_TOOLS:
                    fp = inp.get("file_path") or inp.get("notebook_path") or ""
                    t.events.append(Event("assistant", "edit", tool=name, paths=[fp] if fp else [], ts=ts))
                else:
                    t.events.append(Event("assistant", "tool", tool=name, ts=ts))
    _mark_finals(t)
    return t


# --------------------------------------------------------------------------- Codex
def _codex_text(content) -> str:
    return "\n".join(c.get("text", "") for c in content or [] if isinstance(c, dict)).strip()


def read_codex(path: str, start: int = 0) -> Transcript:
    p = Path(path)
    sid = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", p.name)
    t = Transcript(agent="codex", session_id=sid.group(1) if sid else p.stem, source=str(p), start=start, end=start)
    if start > 0:  # session_meta (cwd, id) lives on line 1
        for rec, _ in _read_lines(p, 0):
            if rec and rec.get("type") == "session_meta":
                meta = rec.get("payload") or {}
                t.session_id = meta.get("id") or meta.get("session_id") or t.session_id
                t.cwd = meta.get("cwd", "")
            break
    for rec, pos in _read_lines(p, start):
        t.end = pos
        if not rec:
            continue
        typ, pl, ts = rec.get("type"), rec.get("payload") or {}, rec.get("timestamp", "")
        if typ == "session_meta":
            t.session_id = pl.get("id") or pl.get("session_id") or t.session_id
            t.cwd = pl.get("cwd", t.cwd)
            continue
        if typ == "turn_context" and pl.get("cwd"):
            t.cwd = t.cwd or pl["cwd"]
        if typ != "event_msg":
            continue
        et = pl.get("type")
        if et == "task_complete" and t.events:
            for ev in reversed(t.events):
                if ev.role == "assistant" and ev.kind == "text":
                    ev.final = True
                    break
            continue
        if et != "item_completed":
            continue
        it = pl.get("item") or {}
        kind = it.get("type")
        t.started = t.started or ts
        t.ended = ts or t.ended
        if kind == "UserMessage":
            text = _clean_prompt(_codex_text(it.get("content")))
            if text:
                t.events.append(Event("user", "text", text, ts=ts))
        elif kind == "AgentMessage":
            text = _codex_text(it.get("content"))
            if text:
                t.events.append(Event("assistant", "text", text, ts=ts, final=it.get("phase") == "final_answer"))
        elif kind == "CommandExecution":
            cmd = it.get("command")
            if isinstance(cmd, list):
                cmd = cmd[-1] if len(cmd) >= 2 and str(cmd[-2]).lower() in ("-command", "-c", "/c") else " ".join(map(str, cmd))
            t.events.append(Event("assistant", "command", str(cmd or ""), tool="shell", ts=ts))
            if not t.cwd and it.get("cwd", "").startswith("file:"):
                t.cwd = unquote(urlparse(it["cwd"]).path).lstrip("/")
        elif kind == "FileChange":
            t.events.append(Event("assistant", "edit", tool="apply_patch", paths=list((it.get("changes") or {}).keys()), ts=ts))
        elif kind:
            t.events.append(Event("assistant", "tool", tool=kind, ts=ts))
    _mark_finals(t)
    return t


# --------------------------------------------------------------------------- OpenCode
def export_opencode(session_id: str, timeout: int = 120) -> dict:
    """`opencode export --pure`: no plugins load, so our own capture plugin can't recurse."""
    env = dict(os.environ, **{CAPTURE_ENV: "1"})
    exe = shutil.which("opencode")
    if not exe:
        raise FileNotFoundError("opencode CLI not found")
    out = subprocess.run([exe, "export", session_id, "--pure"], capture_output=True, timeout=timeout,
                         env=env, check=True)
    text = out.stdout.decode("utf-8", errors="replace")
    return json.loads(text[text.index("{"):])


def read_opencode(session_id: str, start: int = 0, data: "dict | None" = None) -> Transcript:
    data = data if data is not None else export_opencode(session_id)
    info = data.get("info") or {}
    msgs = data.get("messages") or []
    t = Transcript(agent="opencode", session_id=info.get("id", session_id), source=f"opencode:{session_id}",
                   cwd=info.get("directory", ""), title=info.get("title", ""), start=start, end=len(msgs))
    for m in msgs[start:]:
        mi = m.get("info") or {}
        role = mi.get("role")
        ts = _iso((mi.get("time") or {}).get("created"))
        t.started = t.started or ts
        t.ended = ts or t.ended
        for part in m.get("parts") or []:
            pt = part.get("type")
            if pt == "text" and part.get("text", "").strip() and not part.get("synthetic"):
                text = part["text"].strip()
                if role == "user":
                    text = _clean_prompt(text)
                    if text:
                        t.events.append(Event("user", "text", text, ts=ts))
                else:
                    t.events.append(Event("assistant", "text", text, ts=ts))
            elif pt == "tool" and role == "assistant":
                tool = part.get("tool", "")
                inp = (part.get("state") or {}).get("input") or {}
                if tool == "bash":
                    t.events.append(Event("assistant", "command", inp.get("command", ""), tool=tool, ts=ts))
                elif tool in WRITE_TOOLS:
                    fp = inp.get("filePath") or ""
                    t.events.append(Event("assistant", "edit", tool=tool, paths=[fp] if fp else [], ts=ts))
                else:
                    t.events.append(Event("assistant", "tool", tool=tool, ts=ts))
    _mark_finals(t)
    return t


def _mark_finals(t: Transcript) -> None:
    """The last assistant text before each user prompt (and at the end) is that turn's answer."""
    last = None
    for ev in t.events:
        if ev.role == "user" and last is not None:
            last.final = True
        if ev.role == "assistant" and ev.kind == "text":
            last = ev
        elif ev.role == "user":
            last = None
    if last is not None:
        last.final = True


def read(agent: str, session_id: str, source: str, start: int = 0) -> Transcript:
    if agent == "claude":
        return read_claude(source, start)
    if agent == "codex":
        return read_codex(source, start)
    if agent == "opencode":
        return read_opencode(session_id, start)
    raise ValueError(f"unknown agent {agent!r}")


def find_codex_transcript(session_id: str) -> "str | None":
    root = Path.home() / ".codex" / "sessions"
    hits = sorted(root.glob(f"**/rollout-*{session_id}.jsonl"))
    return str(hits[-1]) if hits else None
