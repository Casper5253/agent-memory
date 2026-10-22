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
                   repo_head=None, attribution=None) -> str:
    tags = ["session", f"agent/{agent}", f"project/{slug(project, 8)}"]
    fm = [
        "---", "type: session", f"agent: {agent}", f"session_id: {_yaml_str(session_id)}",
        f"project: {_yaml_str(project)}", f"date: {when:%Y-%m-%d}", f"started: {_yaml_str(started)}",
        f"ended: {_yaml_str(ended)}", f"part: {part}", f"summarizer: {summarizer}",
        f"files_modified: {len(files)}", "immutable: true",
        "repo_head: " + (_yaml_str(repo_head) if repo_head else "null"),
        *([f"attribution_method: {attribution.method}",
           "attribution_signals: " + json.dumps(attribution.signals, ensure_ascii=False, sort_keys=True),
           "related_projects: " + json.dumps(attribution.related_projects, ensure_ascii=False)] if attribution else []),
        "tags:", *[f"  - {t}" for t in tags], "---", "",
    ]

    def bullets(items, empty="- none recorded"):
        return "\n".join(f"- {redact(x)}" for x in items) if items else empty

    decisions = "\n".join(f"- **{redact(d['decision'])}**" + (f" — {redact(d['rationale'])}" if d.get("rationale") else "")
                          for d in summary.get("decisions", [])) or "- none recorded"
    plink = (f"[[{project_note}|{project}]]" if project_note else project_link(project)) if project != "Unsorted" else "Unsorted (no project note)"
    body = [
        f"# {redact(summary.get('title') or 'Session')}", "",
        f"> Historical record of a {agent} session on {when:%Y-%m-%d}. It may be outdated: the current state lives in {plink}.",
        *( [f"> Continues [[{prev_link}]]."] if prev_link else [] ), "",
        "## Executive summary", "", redact(summary.get("summary", "")), "",
        "## What happened", "", bullets(summary.get("what_happened", [])), "",
        "## Decisions and rationale", "", decisions, "",
        "## Confirmed facts", "", bullets(summary.get("facts", [])), "",
        "## Artifacts and modified files", "", bullets([f"`{f}`" for f in files[:60]]), "",
        "## Commands run", "", bullets([f"`{c}`" for c in commands[:25]], "- none"), "",
        "## Open questions and next steps", "",
        bullets(summary.get("open_questions", []) + [f"Next: {s}" for s in summary.get("next_steps", [])]), "",
        "## Source", "",
        f"- Transcript: `{source}` (range {start}–{end})",
        f"- Captured by agentmem ({summarizer}); quality gate: {gate_reason}", "",
    ]
    return "\n".join(fm + body)


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


def update_project_note(path: Path, project: str, today: str, *, state=None, pending_add=None, pending_clear=None,
                        session_link=None, session_line=None) -> None:
    """Caller holds the project lock. Re-reads the note, edits only managed blocks, writes atomically."""
    text = path.read_text(encoding="utf-8") if path.exists() else new_project_note(project, today)
    text = ensure_blocks(text)
    if state is not None:
        text = set_block(text, STATE_BEGIN, STATE_END, state)
    if pending_add or pending_clear:
        lines = [l for l in get_block(text, PENDING_BEGIN, PENDING_END).splitlines() if l.strip()]
        if pending_clear:
            lines = [l for l in lines if pending_clear not in l]
        lines += [l for l in pending_add or [] if l not in lines]  # idempotent on retry
        text = set_block(text, PENDING_BEGIN, PENDING_END, "\n".join(lines))
    if session_line:
        lines = [l for l in get_block(text, SESS_BEGIN, SESS_END).splitlines() if l.strip() and session_link not in l]
        text = set_block(text, SESS_BEGIN, SESS_END, "\n".join(([session_line] + lines)[:15]))
    atomic_write(path, touch_updated(text, today))
