"""Deterministic extraction: digest, quality gate and model-free fallback summary.

Everything here is redacted. The digest is what a summarizer sees; it is bounded and never contains
tool outputs or raw transcript records.
"""
import hashlib
import os
import re
import subprocess
from dataclasses import dataclass, field

from .adapters import Transcript
from .redact import is_sensitive_path, redact

_DECISION = re.compile(r"(?i)\b(?:decided|decision|we(?:'ll| will) (?:use|go with|keep)|going with|chose|chosen|"
                       r"settled on|switch(?:ed|ing)? to|instead of|recommend(?:ation)?:|verdict)\b")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


@dataclass
class Digest:
    prompts: list = field(default_factory=list)
    answers: list = field(default_factory=list)        # final assistant message per turn
    commands: list = field(default_factory=list)
    files: list = field(default_factory=list)           # modified files, deduped, in order
    decision_lines: list = field(default_factory=list)
    assistant_chars: int = 0
    recent: list = field(default_factory=list)          # last assistant messages (for prompt-less ranges)
    tool_calls: int = 0
    git_commits: list = field(default_factory=list)
    git_files: list = field(default_factory=list)
    text: str = ""                                      # bounded summarizer input
    sha: str = ""

    @property
    def substantial(self) -> bool:
        return self._gate[0]

    @property
    def gate_reason(self) -> str:
        return self._gate[1]

    _gate: tuple = (False, "")


def _short(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _norm_path(p: str, cwd: str) -> str:
    """Path relative to the session's cwd when inside it (mixed / and \\ separators are common on Windows)."""
    p = os.path.normpath(p.strip().strip("'\""))
    if cwd:
        base = os.path.normpath(cwd)
        if os.path.normcase(p).startswith(os.path.normcase(base) + os.sep):
            return p[len(base) + 1:].replace("\\", "/")
    return p.replace("\\", "/")


def build(t: Transcript, min_prompts: int, min_chars: int, cap: int, *, email_allowlist=None) -> Digest:
    d = Digest()
    seen_files = set()
    for ev in t.events:
        if ev.role == "user" and ev.kind == "text":
            d.prompts.append(redact(ev.text, email_allowlist=email_allowlist))
        elif ev.kind == "text":
            d.assistant_chars += len(ev.text)
            if ev.final:
                d.answers.append(redact(ev.text, email_allowlist=email_allowlist))
            d.recent = (d.recent + [redact(ev.text, email_allowlist=email_allowlist)])[-15:]
            for sent in _SENTENCE.split(ev.text):
                if _DECISION.search(sent) and 20 <= len(sent) <= 400:
                    d.decision_lines.append(redact(sent.strip(), email_allowlist=email_allowlist))
        elif ev.kind == "command":
            d.tool_calls += 1
            cmd = redact(_short(ev.text, 240), email_allowlist=email_allowlist) if ev.text and not is_sensitive_path(ev.text) else ""
            if cmd and cmd not in d.commands:
                d.commands.append(cmd)
        elif ev.kind == "edit":
            d.tool_calls += 1
            for p in ev.paths:
                rel = _norm_path(p, t.cwd)
                if rel and rel.lower() not in seen_files:
                    seen_files.add(rel.lower())
                    d.files.append(rel)
        else:
            d.tool_calls += 1
    d.git_commits, d.git_files = _git_session_evidence(t)
    for path in d.git_files:
        if path.lower() not in seen_files:
            seen_files.add(path.lower())
            d.files.append(path)
    d._gate = _gate(d, min_prompts, min_chars)
    d.text = _render(t, d, cap)
    d.sha = hashlib.sha256(d.text.encode("utf-8")).hexdigest()[:16]
    return d


def _gate(d: Digest, min_prompts: int, min_chars: int) -> tuple:
    if d.git_commits:
        return True, f"{len(d.git_commits)} Git commits during session"
    if not d.prompts:
        if len(d.files) >= 3 or (d.tool_calls >= 20 and d.assistant_chars >= min_chars):
            return True, f"autonomous continuation ({d.tool_calls} tool calls, {len(d.files)} files)"
        return False, "no user prompts and little work"
    if d.files and (len(d.prompts) >= min_prompts or len(d.files) >= 3):
        return True, f"{len(d.files)} files modified"
    if len(d.prompts) >= min_prompts and (d.tool_calls >= 5 or d.assistant_chars >= min_chars):
        return True, f"{len(d.prompts)} prompts, {d.tool_calls} tool calls, {d.assistant_chars} chars"
    if len(d.prompts) == 1 and d.assistant_chars >= min_chars * 3 and d.tool_calls >= 10:
        return True, "single long task"
    return False, f"trivial ({len(d.prompts)} prompts, {d.tool_calls} tool calls, {len(d.files)} files)"


def _git_session_evidence(t: Transcript) -> tuple[list, list]:
    """Return committed and still-dirty paths proven to have changed during this session window."""
    if not t.cwd or not t.started:
        return [], []
    try:
        commits = subprocess.run(["git", "-C", t.cwd, "log", "--since=" + t.started,
                                  "--until=" + (t.ended or t.started), "--format=@@%H%x09%cI", "--name-only"],
                                 capture_output=True, text=True, timeout=4, check=True).stdout
        status = subprocess.run(["git", "-C", t.cwd, "status", "--porcelain", "--untracked-files=all"],
                                capture_output=True, text=True, timeout=4, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return [], []
    commit_ids, committed_paths = [], []
    current_commit = False
    for line in commits.splitlines():
        if line.startswith("@@"):
            commit_ids.append(line[2:].split("\t", 1)[0])
            current_commit = True
        elif line.strip() and current_commit:
            committed_paths.append(line.strip().replace("\\", "/"))
    window_start = _timestamp(t.started)
    window_end = _timestamp(t.ended) if t.ended else None
    dirty_paths = []
    for line in status.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].split(" -> ")[-1].strip().strip('"')
        candidate = os.path.join(t.cwd, path)
        try:
            modified = os.path.getmtime(candidate)
        except OSError:
            continue
        if modified >= window_start and (window_end is None or modified <= window_end):
            dirty_paths.append(path.replace("\\", "/"))
    files = list(dict.fromkeys(committed_paths + dirty_paths))
    return commit_ids, files


def _timestamp(value):
    try:
        from datetime import datetime
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _render(t: Transcript, d: Digest, cap: int) -> str:
    """Prompts and answers interleaved by turn, then files and commands. Middle turns are trimmed first."""
    head = [f"AGENT: {t.agent}", f"WORKING DIRECTORY: {t.cwd}", f"SESSION TITLE: {t.title or '-'}", ""]
    tail = ["", "FILES MODIFIED:"] + [f"- {f}" for f in d.files[:80]]
    if d.git_commits:
        tail += ["", "GIT COMMITS DURING SESSION:"] + [f"- {commit}" for commit in d.git_commits[:20]]
    tail += ["", "COMMANDS (first 40):"] + [f"- {c}" for c in d.commands[:40]]
    turns = []
    answers = iter(d.answers)
    for i, p in enumerate(d.prompts, 1):
        turns.append(f"--- USER {i}:\n{_short(p, 3000)}")
        a = next(answers, None)
        if a:
            turns.append(f"--- ASSISTANT {i}:\n{_short(a, 3000)}")
    if not d.prompts:
        turns = ["--- (no new user prompts in this range: the agent continued autonomously)"]
        turns += [f"--- ASSISTANT (recent {i}):\n{_short(a, 2500)}" for i, a in enumerate(d.recent, 1)]
    budget = cap - sum(len(x) + 1 for x in head + tail)
    keep_head, middle = turns[:2], turns[2:]
    trimmed = False
    while middle and sum(len(x) + 1 for x in keep_head + middle) > budget:
        middle.pop(0)  # drop the oldest middle turn; the first turn and the latest turns carry the story
        trimmed = True
    body = keep_head + (["--- [earlier turns trimmed] ---"] if trimmed else []) + middle
    text = "\n".join(head + body + tail)
    return text[:cap]


def fallback_summary(t: Transcript, d: Digest) -> dict:
    """Model-free summary. Clearly labelled; never invents decisions beyond quoted sentences."""
    first = _short(d.prompts[0], 300) if d.prompts else "(autonomous continuation, no new prompt)"
    title = _short(t.title or (d.prompts[0] if d.prompts else "session"), 60)
    return {
        "title": title,
        "summary": (f"Deterministic capture (no model summary). The session started with: \"{first}\". "
                    f"It had {len(d.prompts)} prompts, {d.tool_calls} tool calls and modified {len(d.files)} files."),
        "what_happened": [_short(p, 240) for p in d.prompts[:12]] or [_short(a, 240) for a in d.recent[-8:]],
        "decisions": [{"decision": _short(s, 300), "rationale": "(quoted from the agent; not reconciled)"}
                      for s in _dedupe(d.decision_lines)[:8]],
        "facts": [],
        "open_questions": [],
        "next_steps": [],
    }


def _dedupe(items):
    seen, out = set(), []
    for x in items:
        k = x.lower()[:120]
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out
