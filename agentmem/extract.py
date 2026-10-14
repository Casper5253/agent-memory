"""Deterministic extraction: digest, quality gate and model-free fallback summary.

Everything here is redacted. The digest is what a summarizer sees; it is bounded and never contains
tool outputs or raw transcript records.
"""
import hashlib
import os
import re
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
    tool_calls: int = 0
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


def build(t: Transcript, min_prompts: int, min_chars: int, cap: int) -> Digest:
    d = Digest()
    seen_files = set()
    for ev in t.events:
        if ev.role == "user" and ev.kind == "text":
            d.prompts.append(redact(ev.text))
        elif ev.kind == "text":
            d.assistant_chars += len(ev.text)
            if ev.final:
                d.answers.append(redact(ev.text))
            for sent in _SENTENCE.split(ev.text):
                if _DECISION.search(sent) and 20 <= len(sent) <= 400:
                    d.decision_lines.append(redact(sent.strip()))
        elif ev.kind == "command":
            d.tool_calls += 1
            cmd = redact(_short(ev.text, 240)) if ev.text and not is_sensitive_path(ev.text) else ""
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
    d._gate = _gate(d, min_prompts, min_chars)
    d.text = _render(t, d, cap)
    d.sha = hashlib.sha256(d.text.encode("utf-8")).hexdigest()[:16]
    return d


def _gate(d: Digest, min_prompts: int, min_chars: int) -> tuple:
    if not d.prompts:
        return False, "no user prompts"
    if d.files and (len(d.prompts) >= min_prompts or len(d.files) >= 3):
        return True, f"{len(d.files)} files modified"
    if len(d.prompts) >= min_prompts and (d.tool_calls >= 5 or d.assistant_chars >= min_chars):
        return True, f"{len(d.prompts)} prompts, {d.tool_calls} tool calls, {d.assistant_chars} chars"
    if len(d.prompts) == 1 and d.assistant_chars >= min_chars * 3 and d.tool_calls >= 10:
        return True, "single long task"
    return False, f"trivial ({len(d.prompts)} prompts, {d.tool_calls} tool calls, {len(d.files)} files)"


def _render(t: Transcript, d: Digest, cap: int) -> str:
    """Prompts and answers interleaved by turn, then files and commands. Middle turns are trimmed first."""
    head = [f"AGENT: {t.agent}", f"WORKING DIRECTORY: {t.cwd}", f"SESSION TITLE: {t.title or '-'}", ""]
    tail = ["", "FILES MODIFIED:"] + [f"- {f}" for f in d.files[:80]]
    tail += ["", "COMMANDS (first 40):"] + [f"- {c}" for c in d.commands[:40]]
    turns = []
    answers = iter(d.answers)
    for i, p in enumerate(d.prompts, 1):
        turns.append(f"--- USER {i}:\n{_short(p, 3000)}")
        a = next(answers, None)
        if a:
            turns.append(f"--- ASSISTANT {i}:\n{_short(a, 3000)}")
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
    first = _short(d.prompts[0], 300) if d.prompts else "(no prompt)"
    title = _short(t.title or (d.prompts[0] if d.prompts else "session"), 60)
    return {
        "title": title,
        "summary": (f"Deterministic capture (no model summary). The session started with: \"{first}\". "
                    f"It had {len(d.prompts)} prompts, {d.tool_calls} tool calls and modified {len(d.files)} files."),
        "what_happened": [_short(p, 240) for p in d.prompts[:12]],
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
