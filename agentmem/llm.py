"""Lightweight model calls (Claude Haiku via the `claude` CLI), isolated from the user's environment.

Isolation is structural, not a convention:
  --setting-sources ""        no user/project settings, so none of our capture hooks load
  --no-session-persistence    no transcript is written, so there is nothing to capture
  --tools ""                  the model cannot touch the filesystem
  AGENTMEM_CAPTURE=1          any hook that does run exits immediately
Callers must handle LLMError: the pipeline falls back to deterministic output, never a partial write.
"""
import json
import os
import re
import shutil
import subprocess

from .config import CAPTURE_ENV, RUNTIME
from .redact import redact


class LLMError(Exception):
    pass


SUMMARY_SYSTEM = """You write the permanent record of one AI coding-agent session for a personal knowledge vault.
Input: a digest of the session (user prompts, the agent's final answers per turn, modified files, commands).
Return ONLY a JSON object with exactly these keys:
  "title": short noun phrase, max 8 words
  "summary": 2-4 plain-English sentences: what was attempted, what was achieved, current status
  "what_happened": list of short past-tense bullets in order (max 10)
  "decisions": list of {"decision": str, "rationale": str} for choices that were actually made (not proposed). Empty if none.
  "facts": list of verified facts the session established (configuration values, paths, versions, behaviours observed). Max 12.
  "open_questions": list of unresolved questions (max 8)
  "next_steps": list of concrete follow-ups (max 8)
Rules: use only information in the digest; never invent. Prefer specific nouns (file names, tools, versions).
Write facts and decisions so they make sense without the transcript. Never include secrets, tokens or personal data."""

CONSOLIDATE_SYSTEM = """You maintain the CURRENT-STATE section of a project note in a knowledge vault.
Input: (1) the existing state section (markdown), (2) a new session's summary JSON, (3) that session's wikilink and date.
Reconcile, do not append blindly:
- Update "Current state" so it describes the project as it is NOW. Remove statements the new session made false.
- A new decision that replaces an older active decision: move the old one to "Superseded decisions" as
  "- <old date> — <old decision> → superseded <new date>: <short reason> (<old link>)" and add the new one to "Active decisions".
- Keep every still-valid decision with its original date and link. Never drop history silently.
- "Open questions and next steps": drop items the session resolved, add new ones.
Return ONLY a JSON object: {"state_markdown": "<markdown>", "changed": true|false}
state_markdown must contain exactly these four headings, in order, each followed by bullets (or "- none"):
### Current state
### Active decisions
### Superseded decisions
### Open questions and next steps
Decision bullets: "- YYYY-MM-DD — <decision> — <rationale> ([[link]])". Max 5000 characters. No secrets."""


def _run(system: str, prompt: str, model: str, timeout: int) -> dict:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    cwd = RUNTIME / "tmp"
    cwd.mkdir(exist_ok=True)
    env = dict(os.environ, **{CAPTURE_ENV: "1"})
    exe = shutil.which("claude")
    if not exe:
        raise LLMError("claude CLI not found")
    args = [exe, "-p", "--model", model, "--output-format", "json", "--no-session-persistence",
            "--setting-sources", "", "--tools", "", "--disable-slash-commands", "--strict-mcp-config",
            "--system-prompt", system]
    try:
        out = subprocess.run(args, input=prompt.encode("utf-8"), capture_output=True, timeout=timeout,
                             cwd=cwd, env=env, shell=False)
    except FileNotFoundError as e:
        raise LLMError("claude CLI not found") from e
    except subprocess.TimeoutExpired as e:
        raise LLMError(f"timeout after {timeout}s") from e
    if out.returncode != 0:
        raise LLMError(f"claude exit {out.returncode}: {out.stderr.decode('utf-8', 'replace')[:300]}")
    try:
        envelope = json.loads(out.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError as e:
        raise LLMError("non-JSON CLI envelope") from e
    if envelope.get("is_error"):
        raise LLMError(f"model error: {str(envelope.get('result'))[:300]}")
    text = envelope.get("result") or ""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise LLMError("no JSON object in model output")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise LLMError("invalid JSON from model") from e
    data["_cost_usd"] = envelope.get("total_cost_usd", 0.0)
    return data


def _strings(v, limit):
    return [redact(str(x)).strip() for x in (v or []) if str(x).strip()][:limit] if isinstance(v, list) else []


def summarize(digest_text: str, model: str = "haiku", timeout: int = 180) -> dict:
    data = _run(SUMMARY_SYSTEM, "SESSION DIGEST:\n\n" + digest_text, model, timeout)
    if not isinstance(data.get("summary"), str) or not data["summary"].strip():
        raise LLMError("summary missing")
    decisions = []
    for d in data.get("decisions") or []:
        if isinstance(d, dict) and str(d.get("decision", "")).strip():
            decisions.append({"decision": redact(str(d["decision"]).strip()), "rationale": redact(str(d.get("rationale", "")).strip())})
    return {
        "title": redact(str(data.get("title") or "")).strip()[:80],
        "summary": redact(data["summary"].strip()),
        "what_happened": _strings(data.get("what_happened"), 10),
        "decisions": decisions[:12],
        "facts": _strings(data.get("facts"), 12),
        "open_questions": _strings(data.get("open_questions"), 8),
        "next_steps": _strings(data.get("next_steps"), 8),
        "_cost_usd": data.get("_cost_usd", 0.0),
    }


REQUIRED_HEADINGS = ["### Current state", "### Active decisions", "### Superseded decisions", "### Open questions and next steps"]


def consolidate(state_md: str, summary: dict, link: str, date: str, model: str = "haiku", timeout: int = 180) -> tuple:
    payload = {k: v for k, v in summary.items() if not k.startswith("_")}
    prompt = (f"EXISTING STATE SECTION:\n{state_md or '(empty: this is the first session for the project)'}\n\n"
              f"NEW SESSION ({date}, link {link}):\n{json.dumps(payload, ensure_ascii=False, indent=1)}")
    data = _run(CONSOLIDATE_SYSTEM, prompt, model, timeout)
    md = str(data.get("state_markdown") or "").strip()
    pos = [md.find(h) for h in REQUIRED_HEADINGS]
    if any(p < 0 for p in pos) or pos != sorted(pos):
        raise LLMError("consolidation output missing required headings")
    if len(md) > 8000:
        raise LLMError("consolidation output too long")
    return redact(md), data.get("_cost_usd", 0.0)
