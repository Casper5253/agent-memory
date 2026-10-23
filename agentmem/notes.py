"""Session notes (immutable) and project notes (managed regions only)."""
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from .fsutil import atomic_write
from .redact import redact

STATE_BEGIN, STATE_END = "<!-- agentmem:state:begin -->", "<!-- agentmem:state:end -->"
SESS_BEGIN, SESS_END = "<!-- agentmem:sessions:begin -->", "<!-- agentmem:sessions:end -->"
PENDING_BEGIN, PENDING_END = "<!-- agentmem:pending:begin -->", "<!-- agentmem:pending:end -->"
EMPTY_STATE = "### Current state\n- none yet\n\n### Active decisions\n- none\n\n### Superseded decisions\n- none\n\n### Open questions and next steps\n- none"


def slug(text: str, words: int = 6) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return "-".join(s.split("-")[:words]) or "session"


def _yaml_str(v: str) -> str:
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def session_note_path(sessions_dir: Path, when: datetime, agent: str, title: str, session_id: str, part: int) -> Path:
    id8 = hashlib.sha256(json.dumps([agent, session_id, part], separators=(",", ":")).encode("utf-8")).hexdigest()
    suffix = f"-p{part}" if part > 1 else ""
    name = f"{when:%Y-%m-%d-%H%M}-{agent}-{slug(title)}-{id8}{suffix}.md"
    return sessions_dir / f"{when:%Y}" / f"{when:%m}" / name


def project_link(project: str) -> str:
    return f"[[1-Projects/{project}/{project}|{project}]]"


def session_metadata(text, note_type="session"):
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise ValueError("capture_identity_conflict: malformed session frontmatter")
    header = text[4:].split("\n---\n", 1)[0]
    fields = {}
    for line in header.splitlines():
        if not line or line[0].isspace() or ": " not in line:
            continue
        key, value = line.split(": ", 1)
        if key in fields:
            raise ValueError("capture_identity_conflict: duplicate frontmatter field")
        try:
            fields[key] = str(json.loads(value)) if value.startswith('"') else value
        except ValueError as error:
            raise ValueError("capture_identity_conflict: invalid frontmatter string") from error
    if fields.get("type") != note_type or fields.get("immutable") != "true":
        raise ValueError("capture_identity_conflict: not an immutable session note")
    return fields


def source_range(text):
    match = re.search(r"(?m)^- Transcript: `[^\n]*` \(range (\d+)[–-](\d+)\)$", text)
    if not match or int(match[2]) < int(match[1]):
        raise ValueError("capture_identity_conflict: missing legacy source range")
    return int(match[1]), int(match[2])


def summary_from_session(text):
    def section(title):
        match = re.search(r"(?ms)^## " + re.escape(title) + r"\n\n(.*?)(?=\n## |\Z)", text)
        if not match:
            raise ValueError("capture_identity_conflict: missing legacy summary section")
        return match[1].strip()
    def bullets(title):
        return [line[2:] for line in section(title).splitlines() if line.startswith("- ") and line != "- none recorded"]
    title = re.search(r"(?m)^# (.+)$", text)
    decisions = []
    for line in bullets("Decisions and rationale"):
        match = re.fullmatch(r"\*\*(.*?)\*\*(?: — (.*))?", line)
        if not match:
            raise ValueError("capture_identity_conflict: invalid legacy decision")
        decisions.append({"decision": match[1], "rationale": match[2] or ""})
    return dict(title=title[1] if title else "Session", summary=section("Executive summary"),
                what_happened=bullets("What happened"), decisions=decisions, facts=bullets("Confirmed facts"),
                open_questions=bullets("Open questions and next steps"), next_steps=[])


def render_session(*, agent, session_id, project, part, when, started, ended, source, start, end, summarizer,
                   summary, files, commands, gate_reason, prev_link=None, project_note=None,
                   repo_head=None, attribution=None, source_hash="", email_allowlist=None) -> str:
    tags = ["session", f"agent/{agent}", f"project/{slug(project, 8)}"]
    fm = [
        "---", "type: session", f"agent: {agent}", f"session_id: {_yaml_str(session_id)}",
        f"project: {_yaml_str(project)}", f"date: {when:%Y-%m-%d}", f"started: {_yaml_str(started)}",
        f"ended: {_yaml_str(ended)}", f"part: {part}", f"summarizer: {summarizer}",
        f"files_modified: {len(files)}", "immutable: true", "logical_session: true",
        "repo_head: " + (_yaml_str(repo_head) if repo_head else "null"),
        *([f"attribution_method: {attribution.method}",
           "attribution_signals: " + json.dumps(attribution.signals, ensure_ascii=False, sort_keys=True),
           "related_projects: " + json.dumps(attribution.related_projects, ensure_ascii=False)] if attribution else []),
        "tags:", *[f"  - {t}" for t in tags], "---", "",
    ]

    def bullets(items, empty="- none recorded"):
        return "\n".join(f"- {redact(x, email_allowlist=email_allowlist)}" for x in items) if items else empty

    decisions = "\n".join(f"- **{redact(d['decision'], email_allowlist=email_allowlist)}**" +
                          (f" — {redact(d['rationale'], email_allowlist=email_allowlist)}" if d.get("rationale") else "")
                          for d in summary.get("decisions", [])) or "- none recorded"
    plink = (f"[[{project_note}|{project}]]" if project_note else project_link(project)) if project != "Unsorted" else "Unsorted (no project note)"
    body = [
        f"# {redact(summary.get('title') or 'Session', email_allowlist=email_allowlist)}", "",
        f"> Historical record of a {agent} session on {when:%Y-%m-%d}. It may be outdated: the current state lives in {plink}.",
        *( [f"> Continues [[{prev_link}]]."] if prev_link else [] ), "",
    ]
    sections = [
        ("Executive summary", redact(summary.get("summary", ""), email_allowlist=email_allowlist)),
        ("What happened", bullets(summary.get("what_happened", []), "")),
        ("Decisions and rationale", decisions if summary.get("decisions") else ""),
        ("Confirmed facts", bullets(summary.get("facts", []), "")),
        ("Artifacts and modified files", bullets([f"`{f}`" for f in files[:60]], "")),
        ("Commands run", bullets([f"`{c}`" for c in commands[:25]], "")),
        ("Open questions and next steps", bullets(summary.get("open_questions", []) +
                                                     [f"Next: {s}" for s in summary.get("next_steps", [])], "")),
    ]
    for heading, content in sections:
        if content.strip():
            body += [f"## {heading}", "", content, ""]
    body += ["## Source", "", f"- Transcript: `{source}` (range {start}–{end})"]
    if source_hash:
        body.append(f"- SHA-256: `{source_hash}`")
    body += [f"- Captured by agentmem ({summarizer}); quality gate: {gate_reason}", ""]
    citations = summary.get("citations", [])
    if citations:
        body += ["## Evidence", ""]
        for index, citation in enumerate(citations, 1):
            if not isinstance(citation, dict):
                continue
            claim = redact(str(citation.get("claim") or ""), email_allowlist=email_allowlist)
            quote = redact(str(citation.get("quote") or ""), email_allowlist=email_allowlist)
            if claim and quote:
                body += [f"### E{index}", "", f"- Claim: {claim}", f"- Digest span: {quote}", ""]
    return "\n".join(fm + body)


def append_session_continuation(text, *, when, source, start, end, source_hash, summarizer, summary, files,
                                commands, gate_reason, email_allowlist=None) -> str:
    fields = session_metadata(text)
    if fields.get("logical_session") != "true":
        raise ValueError("capture_identity_conflict: prior note is not a logical session note")
    date = when.strftime("%Y-%m-%d %H:%M %Z").strip()
    chunks = [f"\n## Continued {date}\n"]

    def add(heading, value):
        if value and value.strip():
            chunks.append(f"\n### {heading}\n\n{value.strip()}\n")

    add("Executive summary", redact(summary.get("summary", ""), email_allowlist=email_allowlist))
    add("What happened", "\n".join(f"- {redact(v, email_allowlist=email_allowlist)}"
                                     for v in summary.get("what_happened", []) if str(v).strip()))
    decisions = "\n".join("- **" + redact(str(d.get("decision", "")), email_allowlist=email_allowlist) + "**" +
                          ((" — " + redact(str(d.get("rationale", "")), email_allowlist=email_allowlist))
                           if d.get("rationale") else "") for d in summary.get("decisions", [])
                          if str(d.get("decision", "")).strip())
    add("Decisions and rationale", decisions)
    add("Confirmed facts", "\n".join(f"- {redact(v, email_allowlist=email_allowlist)}"
                                      for v in summary.get("facts", []) if str(v).strip()))
    add("Artifacts and modified files", "\n".join(f"- `{redact(v, email_allowlist=email_allowlist)}`"
                                                   for v in files[:60] if str(v).strip()))
    add("Commands run", "\n".join(f"- `{redact(v, email_allowlist=email_allowlist)}`"
                                    for v in commands[:25] if str(v).strip()))
    add("Open questions and next steps", "\n".join(f"- {redact(v, email_allowlist=email_allowlist)}"
                   for v in summary.get("open_questions", []) + [f"Next: {v}" for v in summary.get("next_steps", [])]
                   if str(v).strip()))
    receipt = f"- Transcript range: `{start}–{end}`\n- SHA-256: `{source_hash}`\n- Source: `{source}`\n- Captured by agentmem ({summarizer}); quality gate: {gate_reason}"
    add("Receipt", receipt)
    return text.rstrip() + "\n" + "".join(chunks)


# ---------------------------------------------------------------------------------- project notes
def new_project_note(project: str, today: str) -> str:
    return "\n".join([
        "---", "type: project", "status: active", f"created: {today}", f"updated: {today}",
        "tags:", "  - type/project", f"  - project/{slug(project, 8)}", "---", "",
        f"# {project}", "", "## Current state (maintained by agentmem)", "",
        "> Reconciled from session evidence. Edit freely outside the markers; inside, agentmem rewrites.", "",
        STATE_BEGIN, EMPTY_STATE, STATE_END, "",
        "## Pending reconciliation", "", PENDING_BEGIN, PENDING_END, "",
        "## Recent sessions", "", SESS_BEGIN, SESS_END, "",
        "## Notes", "", "",
    ])


def ensure_blocks(text: str) -> str:
    """Add any missing managed blocks to an existing (human-written) project note, at the end."""
    add = []
    if STATE_BEGIN not in text:
        add += ["", "## Current state (maintained by agentmem)", "", STATE_BEGIN, EMPTY_STATE, STATE_END]
    if PENDING_BEGIN not in text:
        add += ["", "## Pending reconciliation", "", PENDING_BEGIN, PENDING_END]
    if SESS_BEGIN not in text:
        add += ["", "## Recent sessions", "", SESS_BEGIN, SESS_END]
    return text.rstrip("\n") + "\n" + "\n".join(add) + "\n" if add else text


def get_block(text: str, begin: str, end: str) -> str:
    i, j = text.find(begin), text.find(end)
    if i < 0 or j < i or text.count(begin) != 1 or text.count(end) != 1:
        raise ValueError(f"managed block {begin} missing or malformed")
    return text[i + len(begin):j].strip("\n")


def set_block(text: str, begin: str, end: str, content: str) -> str:
    get_block(text, begin, end)  # validates
    i, j = text.find(begin), text.find(end)
    return text[: i + len(begin)] + "\n" + content.strip("\n") + ("\n" if content.strip() else "") + text[j:]


def touch_updated(text: str, today: str) -> str:
    return re.sub(r"(?m)^updated: .*$", f"updated: {today}", text, count=1) if text.startswith("---") else text


def project_history_path(path: Path) -> Path:
    return path.with_name("History.md")


def state_order(text: str) -> tuple:
    header = text.split("\n---\n", 1)[0] if text.startswith("---\n") else ""
    match = re.search(r"(?m)^agentmem_state_order: (\d+):(\d+)$", header)
    return (int(match[1]), int(match[2])) if match else (0, 0)


def touch_state_order(text: str, order: tuple) -> str:
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        return text
    header, body = text[4:].split("\n---\n", 1)
    value = f"{order[0]}:{order[1]}"
    if re.search(r"(?m)^agentmem_state_order: \d+:\d+$", header):
        header = re.sub(r"(?m)^agentmem_state_order: \d+:\d+$", f"agentmem_state_order: {value}", header, count=1)
    else:
        header += f"\nagentmem_state_order: {value}"
    return "---\n" + header + "\n---\n" + body


def append_project_history(path: Path, project: str, date: str, session_link: str, items, reason: str,
                           email_allowlist=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else f"# {project} history\n"
    suffix = f"[[{session_link}]]" if session_link else "session evidence unavailable"
    entries = [f"- {date} {reason}: {redact(item.strip(), email_allowlist=email_allowlist)} ({suffix})" for item in items
               if item and item.strip() and item.strip().lower() not in ("- none", "- none yet")]
    additions = [entry for entry in entries if entry not in text]
    if additions:
        atomic_write(path, text.rstrip("\n") + "\n\n" + "\n".join(additions) + "\n")


def update_project_note(path: Path, project: str, today: str, *, state=None, pending_add=None, pending_clear=None,
                        session_link=None, session_line=None, state_order_value=None, email_allowlist=None) -> None:
    """Caller holds the project lock. Re-reads the note, edits only managed blocks, writes atomically."""
    text = path.read_text(encoding="utf-8") if path.exists() else new_project_note(project, today)
    text = ensure_blocks(text)
    if state is not None:
        old_state = get_block(text, STATE_BEGIN, STATE_END)
        old_bullets = [line.strip()[2:] for line in old_state.splitlines()
                       if line.strip().startswith("- ") and line.strip().lower() not in ("- none", "- none yet")]
        new_bullets = {line.strip()[2:] for line in state.splitlines()
                       if line.strip().startswith("- ") and line.strip().lower() not in ("- none", "- none yet")}
        displaced = [item for item in old_bullets if item not in new_bullets]
        if displaced:
            append_project_history(project_history_path(path), project, today, session_link or "", displaced,
                                   "Displaced status item", email_allowlist=email_allowlist)
        text = set_block(text, STATE_BEGIN, STATE_END, state)
        if state_order_value is not None:
            text = touch_state_order(text, state_order_value)
    if pending_add or pending_clear:
        lines = [l for l in get_block(text, PENDING_BEGIN, PENDING_END).splitlines() if l.strip()]
        if pending_clear:
            lines = [l for l in lines if pending_clear not in l]
        lines += [l for l in pending_add or [] if l not in lines]  # idempotent on retry
        text = set_block(text, PENDING_BEGIN, PENDING_END, "\n".join(lines))
    if session_line:
        lines = [l for l in get_block(text, SESS_BEGIN, SESS_END).splitlines() if l.strip() and session_link not in l]
        text = set_block(text, SESS_BEGIN, SESS_END, "\n".join(([session_line] + lines)[:15]))
    text = redact(text, email_allowlist=email_allowlist)
    atomic_write(path, touch_updated(text, today))
