"""Lightweight model calls (Claude Haiku via the `claude` CLI), isolated from the user's environment.

Isolation is structural, not a convention:
  --setting-sources ""        no user/project settings, so none of our capture hooks load
  --no-session-persistence    no transcript is written, so there is nothing to capture
  --tools ""                  the model cannot touch the filesystem
  AGENTMEM_CAPTURE=1          any hook that does run exits immediately
Callers must handle LLMError: the pipeline falls back to deterministic output, never a partial write.
"""
import copy
import json
import os
import re
import shutil
import subprocess
import tempfile

from .config import CAPTURE_ENV
from .grounding import grounded
from .redact import redact
from .fsutil import NO_WINDOW


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
Rules: use only information in the digest; never invent. The summarizer's own working directory, environment,
machine, and other runtime context are not evidence about the session. Prefer specific nouns (file names, tools,
versions). Every statement in summary, what_happened, decisions, facts, open_questions, next_steps, and title
must have a citation object with the same section and exact claim text, plus an exact quote copied from the digest.
Do not cite a quote that does not support its claim. Omit any claim without a supporting digest span. Return
"citations": [{"section": "summary", "claim": "...", "quote": "exact digest text"}, ...].
The summary states project facts only: what exists and works, decisions and their reasons, real blockers, open questions, and next steps for the project. Never record agent process: being idle or waiting, awaiting a review or reply, reporting to someone, a run in progress, a command that was run or observed, or routing between teammates. Keep "Run the full suite before release" as a next step; drop "Stay idle until dev-claude assigns a new task" and "Ran unittest discovery for test_x.py".
Never include secrets, tokens or personal data."""

CONSOLIDATE_SYSTEM = """Maintain a project status page from cited session evidence. Return only JSON.
The input has sections of old items with explicit ids, plus the new session summary and allowed refs.
Return {"sections": {key: [entry, ...] for every supplied section key}, "displaced": [entry, ...]}.
Keep an old item unchanged with {"keep": "iN"}. Move it between sections if needed.
New items are {"text": "one claim, at most 280 characters, no newlines or HTML markers", "ref": "allowed session ref"}.
To replace text, add a new cited item and displace the exact original with
{"id": "iN", "reason": "superseded" or "resolved", "ref": "allowed new-session ref", "claim": "resolving or replacing statement"}.
Superseded requires a new cited replacement in a compatible section, using the same ref as the displacement.
Decisions replace only decisions. Next steps, active work and questions may replace each other.
When a new next step or open item about the same work replaces an old next step, active item or question,
add the new cited item and displace the old id as "superseded".
What works now, latest progress and purpose items are replaced only within their own section.
A next step about the same work does not supersede the decision behind that work. Keep that decision.
Resolved removes only questions, next steps or active work, using a new cited result with the same ref.
Questions may be answered by decisions, latest progress or working_now; tasks by latest progress or working_now.
Never resolve decisions, purpose, working_now or latest progress.
In decisions, write the decision text, optionally followed by "; reason". Never write its rationale alone.
Use the decision citation for a decision item, not a rationale or next-step citation.
Every new item text and displacement claim must contain the exact claim of the evidence id it cites.
Keep every old item unless the new evidence resolves or contradicts it. Never silently omit an item.
If evidence is inconclusive, keep both claims and add a cited open question.
Do not write current repository observations such as branch, HEAD, working tree, dirty or ahead/behind state.
Separate durable achievements and decisions from current repository observations. Code reads Git live.
The page states project facts only: what exists and works, decisions and their reasons, real blockers, open product questions, and next steps for the project. Never record agent process: being idle or waiting, awaiting a review or reply, reporting to someone, a run in progress, a command that was run or observed, or routing between teammates. Keep "Run the full suite before release" as a project next step. Drop "Stay idle until dev-claude assigns a new task", "Ran unittest discovery for test_x.py", "Get the review verdict from Haiku" and "Observed the replay still advancing".
History, rendering and bounds belong to code. Do not write markdown or history links. No secrets."""


def _run(system: str, prompt: str, model: str, timeout: int) -> dict:
    env = dict(os.environ, **{CAPTURE_ENV: "1"})
    exe = shutil.which("claude")
    if not exe:
        raise LLMError("claude CLI not found")
    args = [exe, "-p", "--model", model, "--output-format", "json", "--no-session-persistence",
            "--setting-sources", "", "--tools", "", "--disable-slash-commands", "--strict-mcp-config",
            "--system-prompt", system]
    try:
        with tempfile.TemporaryDirectory(prefix="agentmem-llm-") as neutral_dir:
            out = subprocess.run(args, input=prompt.encode("utf-8"), capture_output=True, timeout=timeout,
                                 cwd=neutral_dir, env=env, shell=False, creationflags=NO_WINDOW)
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


def _strings(v, limit, email_allowlist=None):
    return [redact(str(x), email_allowlist=email_allowlist).strip() for x in (v or []) if str(x).strip()][:limit] if isinstance(v, list) else []


def _grounded_statements(data: dict, digest_text: str) -> list:
    normalize = lambda value: re.sub(r"\s+", " ", value).strip()
    digest = normalize(digest_text)
    citations = []
    for citation in data.get("citations", []) if isinstance(data.get("citations"), list) else []:
        if not isinstance(citation, dict):
            continue
        section, claim, quote = (citation.get(k) for k in ("section", "claim", "quote"))
        if not all(isinstance(value, str) and value.strip() for value in (section, claim, quote)):
            continue
        claim_text, quote_text = normalize(claim), normalize(quote)
        if quote_text in digest and grounded(claim_text, quote_text, digest):
            canonical = {"decisions": "decision"}.get(section.strip(), section.strip())
            citations.append({"section": canonical, "claim": claim_text, "quote": quote_text})
    return citations


def _sentences(value):
    value = value.strip()
    value = re.sub(r"\b(e\.g|i\.e|Mr|Mrs|Ms|Dr|vs)\.\s+", r"\1<PERIOD> ", value, flags=re.I)
    return [part.replace("<PERIOD>", ".").strip()
            for part in re.split(r"(?<=[.!?])\s+", value) if part.strip()]


def _summary_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        sentences = [item.strip() for item in value if item.strip()]
        return " ".join(s if s[-1] in ".!?" else s + "." for s in sentences)
    raise LLMError("summary is not text")


def _text_key(value):
    return re.sub(r"\s+", " ", value).strip().lower().rstrip(" .!?;:,")


def _cover(section, text, citations, digest_text):
    """The grounded citation of this section that supports text, or None.

    A claim may cover several sentences, so either text contains the claim or the claim contains text."""
    key = _text_key(text)
    if not key:
        return None
    for citation in citations:
        claim = _text_key(citation["claim"])
        if citation["section"] == section and (key == claim or key in claim or claim in key) \
                and grounded(text, citation["quote"], digest_text):
            return citation
    return None


def summarize(digest_text: str, model: str = "haiku", timeout: int = 180, email_allowlist=None) -> dict:
    data = _run(SUMMARY_SYSTEM, "SESSION DIGEST:\n\n" + digest_text, model, timeout)
    citations = _grounded_statements(data, digest_text)
    used = []

    def supported(section, text):
        citation = _cover(section, text, citations, digest_text)
        if citation is not None and citation not in used:
            used.append(citation)
        return citation is not None

    summary = " ".join(part for part in _sentences(_summary_text(data.get("summary"))) if supported("summary", part))
    if not summary:
        raise LLMError("summary missing digest-cited statements")
    decisions = []
    for d in data.get("decisions") or []:
        if not isinstance(d, dict):
            continue
        decision = str(d.get("decision", "")).strip()
        rationale = str(d.get("rationale", "")).strip()
        if decision and supported("decision", decision):
            decisions.append({"decision": redact(decision, email_allowlist=email_allowlist),
                              "rationale": redact(rationale, email_allowlist=email_allowlist)
                              if rationale and (supported("rationale", rationale) or supported("decision", rationale))
                              else ""})
    grounded_lists = {
        key: [claim for claim in _strings(data.get(key), limit) if supported(key, claim)]
        for key, limit in (("what_happened", 10), ("facts", 12), ("open_questions", 8), ("next_steps", 8))
    }
    title = str(data.get("title") or "").strip()
    title = title if supported("title", title) else ""
    return {
        "title": redact(title, email_allowlist=email_allowlist)[:80],
        "summary": redact(summary, email_allowlist=email_allowlist),
        **{key: _strings(values, len(values), email_allowlist) for key, values in grounded_lists.items()},
        "decisions": decisions[:12],
        "citations": [citation for citation in citations if citation in used],
        "_cost_usd": data.get("_cost_usd", 0.0),
    }


def consolidate(state_md: str, summary: dict, link: str, date: str, model: str = "haiku", timeout: int = 180,
                email_allowlist=None) -> tuple:
    from . import status
    try:
        old = status.live_page(state_md)
    except ValueError as error:
        raise LLMError("page not migrated") from error
    payload = copy.deepcopy({k: v for k, v in summary.items() if not k.startswith("_")})
    for index, citation in enumerate(payload.get("citations", []), 1 + summary.get('_evidence_offset', 0)):
        if isinstance(citation, dict):
            citation["evidence_id"] = f"e{index}"
    allowed = status.evidence_support(summary, link)
    prompt = json.dumps(dict(sections=status.model_items(old), summary=payload,
                             allowed_refs=sorted(allowed), date=date), ensure_ascii=False)
    data = _run(CONSOLIDATE_SYSTEM, redact(prompt, email_allowlist=email_allowlist), model, timeout)
    try:
        safe = json.loads(redact(json.dumps(data, ensure_ascii=False), email_allowlist=email_allowlist))
        return status.reconcile(old, safe, allowed), data.get("_cost_usd", 0.0)
    except (ValueError, TypeError) as error:
        raise LLMError("invalid StatusUpdate JSON") from error
