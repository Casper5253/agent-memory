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
Rules: use only information in the digest; never invent. The summarizer's own working directory, environment,
machine, and other runtime context are not evidence about the session. Prefer specific nouns (file names, tools,
versions). Every statement in summary, what_happened, decisions, facts, open_questions, next_steps, and title
must have a citation object with the same section and exact claim text, plus an exact quote copied from the digest.
Do not cite a quote that does not support its claim. Omit any claim without a supporting digest span. Return
"citations": [{"section": "summary", "claim": "...", "quote": "exact digest text"}, ...].
Never include secrets, tokens or personal data."""

CONSOLIDATE_SYSTEM = """You maintain the CURRENT-STATE section of a project note in a knowledge vault.
Input: (1) the existing state section (markdown), (2) a new session's summary JSON with digest citations,
(3) that session's wikilink and date.
Reconcile, do not append blindly:
- Update "Current state" so it describes the project as it is NOW. Remove statements the new session made false.
- The consolidator's own working directory, environment, machine, and other runtime context are not evidence.
- Keep every prior fact and decision unless the cited session evidence contradicts it.
- Every new status bullet must be supported by a cited summary claim and include its source link as
  [[<session wikilink without brackets>#eN]], where eN is the evidence_id in the summary citation. Do not create a new assertion from an uncited statement.
- A new decision that replaces an older active decision: move the old one to "Superseded decisions" as
  "- <old date> — <old decision> → superseded <new date>: <short reason> (<old link>)" and add the new one to "Active decisions".
- Keep every still-valid decision with its original date and link. Never drop history silently.
- "Open questions and next steps": drop items the session resolved, add new ones.
- Attach the session summary's repo_head commit to every repository-state line you write.
  If repo_head is null or missing, mark that line's repository commit unknown; never invent a commit.
Return ONLY a JSON object: {"state_markdown": "<markdown>", "changed": true|false}
state_markdown must contain exactly these four headings, in order, each followed by bullets (or "- none"):
### Current state
### Active decisions
### Superseded decisions
### Open questions and next steps
Decision bullets: "- YYYY-MM-DD — <decision> — <rationale> (<session wikilink exactly as given>)". Max 5000 characters. No secrets."""


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
                                 cwd=neutral_dir, env=env, shell=False)
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


def _grounded_statements(data: dict, digest_text: str) -> tuple:
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
        if quote_text in digest and claim_text in quote_text:
            canonical = {"decisions": "decision"}.get(section.strip(), section.strip())
            citations.append({"section": canonical, "claim": claim_text, "quote": quote_text})
    return {(c["section"], c["claim"]): c for c in citations}, citations


def _sentences(value):
    value = value.strip()
    value = re.sub(r"\b(e\.g|i\.e|Mr|Mrs|Ms|Dr|vs)\.\s+", r"\1<PERIOD> ", value, flags=re.I)
    return [part.replace("<PERIOD>", ".").strip()
            for part in re.split(r"(?<=[.!?])\s+", value) if part.strip()]


def _claim_key(section, claim):
    section = {"decisions": "decision"}.get(section, section)
    return section, re.sub(r"\s+", " ", claim).strip()


def summarize(digest_text: str, model: str = "haiku", timeout: int = 180, email_allowlist=None) -> dict:
    data = _run(SUMMARY_SYSTEM, "SESSION DIGEST:\n\n" + digest_text, model, timeout)
    grounded, citations = _grounded_statements(data, digest_text)

    def keep(section, value):
        return [part for part in _sentences(value) if _claim_key(section, part) in grounded]

    summary = " ".join(keep("summary", str(data.get("summary") or "")))
    if not summary:
        raise LLMError("summary missing digest-cited statements")
    decisions = []
    for d in data.get("decisions") or []:
        if not isinstance(d, dict):
            continue
        decision = str(d.get("decision", "")).strip()
        rationale = str(d.get("rationale", "")).strip()
        if decision and _claim_key("decision", decision) in grounded:
            decisions.append({"decision": redact(decision, email_allowlist=email_allowlist),
                              "rationale": redact(rationale, email_allowlist=email_allowlist)
                              if rationale and (_claim_key("rationale", rationale) in grounded
                                                or _claim_key("decision", rationale) in grounded) else ""})
    grounded_lists = {
        "what_happened": [claim for claim in _strings(data.get("what_happened"), 10) if _claim_key("what_happened", claim) in grounded],
        "facts": [claim for claim in _strings(data.get("facts"), 12) if _claim_key("facts", claim) in grounded],
        "open_questions": [claim for claim in _strings(data.get("open_questions"), 8) if _claim_key("open_questions", claim) in grounded],
        "next_steps": [claim for claim in _strings(data.get("next_steps"), 8) if _claim_key("next_steps", claim) in grounded],
    }
    title = str(data.get("title") or "").strip()
    title = title if _claim_key("title", title) in grounded else ""
    used = {(section, claim) for section, claim in grounded if section == "summary" and claim in summary}
    used.update(("title", title) for _ in [0] if title)
    for key, values in grounded_lists.items():
        used.update((key, value) for value in values)
    for decision in decisions:
        used.add(("decision", decision["decision"]))
        if decision["rationale"]:
            used.add(("rationale", decision["rationale"]))
            used.add(("decision", decision["rationale"]))
    used_claims = {claim for _, claim in used}
    return {
        "title": redact(title, email_allowlist=email_allowlist)[:80],
        "summary": redact(summary, email_allowlist=email_allowlist),
        **{key: _strings(values, len(values), email_allowlist) for key, values in grounded_lists.items()},
        "decisions": decisions[:12],
        "citations": [c for c in citations if (c["section"], c["claim"]) in used or c["claim"] in used_claims],
        "_cost_usd": data.get("_cost_usd", 0.0),
    }


REQUIRED_HEADINGS = ["### Current state", "### Active decisions", "### Superseded decisions", "### Open questions and next steps"]


def consolidate(state_md: str, summary: dict, link: str, date: str, model: str = "haiku", timeout: int = 180,
                email_allowlist=None) -> tuple:
    payload = copy.deepcopy({k: v for k, v in summary.items() if not k.startswith("_")})
    citation_ids = []
    if isinstance(payload.get("citations"), list):
        for index, citation in enumerate(payload["citations"], 1):
            if isinstance(citation, dict):
                citation["evidence_id"] = f"e{index}"
                citation_ids.append(f"e{index}")
    prompt = (f"EXISTING STATE SECTION:\n{state_md or '(empty: this is the first session for the project)'}\n\n"
              f"NEW SESSION ({date}, link {link}):\n{json.dumps(payload, ensure_ascii=False, indent=1)}")

    def checked_markdown(data):
        md = str(data.get("state_markdown") or "").strip()
        pos = [md.find(h) for h in REQUIRED_HEADINGS]
        if any(p < 0 for p in pos) or pos != sorted(pos):
            raise LLMError("consolidation output missing required headings")
        if len(md) > 8000:
            raise LLMError("consolidation output too long")
        old_bullets = {line.strip() for line in (state_md or "").splitlines() if line.strip().startswith("-")}
        target = link[2:-2] if link.startswith("[[") and link.endswith("]]") else link
        citation_claims = {
            f"[[{target}#{citation.get('evidence_id')}]]": re.sub(r"\s+", " ", citation.get("claim", "")).strip()
            for citation in payload.get("citations", []) if isinstance(citation, dict)
            and citation.get("evidence_id") and isinstance(citation.get("claim"), str)
        }
        for line in md.splitlines():
            stripped = line.strip()
            if not (stripped.startswith("-") and stripped.lower() != "- none" and stripped not in old_bullets):
                continue
            refs = re.findall(r"\[\[[^\]]+\]\]", stripped)
            body = re.sub(r"\[\[[^\]]+\]\]", "", stripped)
            normalized_body = re.sub(r"\s+", " ", body).strip()
            if not refs or not any(ref in citation_claims and citation_claims[ref] in normalized_body for ref in refs):
                raise LLMError("new status statement lacks a matching digest claim citation")
        return md

    try:
        data = _run(CONSOLIDATE_SYSTEM, prompt, model, timeout)
        md = checked_markdown(data)
    except LLMError as error:
        if str(error) != "consolidation output too long":
            raise
        shortened = prompt + "\n\nThe previous output exceeded 8000 characters. Shorten it to 8000 characters or fewer. " \
            "Keep prior facts and decisions unless the cited session evidence contradicts them. " \
            "Do not remove any prior item only because the page is long."
        data = _run(CONSOLIDATE_SYSTEM, shortened, model, timeout)
        md = checked_markdown(data)
    while "[[[[" in md or "]]]]" in md:  # models sometimes re-wrap a link that already has brackets
        md = md.replace("[[[[", "[[").replace("]]]]", "]]")
    return redact(md, email_allowlist=email_allowlist), data.get("_cost_usd", 0.0)
