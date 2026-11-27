"""Lifecycle hook entry for Claude Code and Codex (same JSON contract). Must be fast and never fail the agent.

Stop / PreCompact -> enqueue "turn"; SessionEnd -> enqueue "end"; SessionStart -> small recall pointer.
Every event also kicks a detached worker. Output is always valid hook JSON ({} unless adding context).
"""
import json
import os
import re
import subprocess
import sys
import time

from .config import CAPTURE_ENV
from .fsutil import NO_WINDOW

CONTEXT_CAP = 4000
GIT_TIMEOUT_S = 2
STATE_CAP = 600
PENDING_PREVIEW_CAP = 120
INDEX_PREFIX = 'Note sections (mem note --section): '
INDEX_LABELS = {
    'Purpose and direction': 'Purpose',
    'What works now': 'Works now',
    'Latest progress': 'Latest',
    'Active work and blockers': 'Active',
    'Open questions': 'Questions',
    'Key decisions (decision, reason)': 'Decisions',
    'Next steps': 'Next steps',
    'Pending reconciliation': 'Pending',
    'Recent sessions': 'Sessions',
    'Notes': 'Notes',
}
NOTE_HEADING = re.compile(r'(?m)^(#{2,3}) (.+)$')
NOTE_DATE = re.compile(r'\d{4}-\d{2}-\d{2}')


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _note_sections(text: str):
    """Leaf headings of a project note as (label, item count, newest ISO date), in note order."""
    headings = list(NOTE_HEADING.finditer(text))
    rows = []
    for position, heading in enumerate(headings):
        level = heading.group(1)
        end = len(text)
        for later in headings[position + 1:]:
            if len(later.group(1)) <= len(level):
                end = later.start()
                break
        if any(len(later.group(1)) > len(level) for later in headings[position + 1:] if later.start() < end):
            continue  # a container heading: its child headings are the sections agents can ask for
        body = text[heading.end():end]
        items = [line for line in body.splitlines()
                 if line.startswith('- ') and line[2:].strip().casefold() not in ('none', 'none yet')]
        prose = [line for line in body.splitlines()
                 if line.strip() and not line.startswith('- ') and not line.startswith('<!--')]
        if not items and not prose:
            continue
        dates = NOTE_DATE.findall(body)
        rows.append((INDEX_LABELS.get(heading.group(2).strip(), heading.group(2).strip()),
                     len(items), max(dates) if dates else ''))
    return rows


def _note_index_variants(text: str) -> list:
    """Section index lines from richest to smallest: names with counts and dates, counts, then names."""
    rows = _note_sections(text)
    if not rows:
        return []
    return [INDEX_PREFIX + body for body in (
        '; '.join(f'{name} {count} {date}' if date else f'{name} {count}' for name, count, date in rows),
        '; '.join(f'{name} {count}' for name, count, _ in rows),
        '; '.join(name for name, _, _ in rows))]


def _note_index(text: str, budget: int) -> "str":
    """The richest section index that fits the remaining budget."""
    if not text or budget <= 0:
        return ''
    for line in _note_index_variants(text):
        if len(line) <= budget:
            return line
    return ''


def _git_line(cwd: str) -> str:
    """Live Git facts, read now and never written to the vault. Empty when cwd is not a readable repository."""
    from . import projects
    if not projects.repository_root(cwd):
        return ""

    def run(*args):
        try:
            result = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=GIT_TIMEOUT_S, creationflags=NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    branch, head, status = run("rev-parse", "--abbrev-ref", "HEAD"), run("rev-parse", "--short", "HEAD"), run("status", "--porcelain")
    if branch is None or head is None or status is None:
        return ""
    line = f"Git (live at start): branch {branch}, HEAD {head}, {'dirty' if status else 'clean'}"
    counts = run("rev-list", "--left-right", "--count", "@{u}...HEAD")  # fails without an upstream: omitted
    if counts:
        behind, ahead = counts.split()
        line += f", ahead {ahead} / behind {behind}"
    return line


def _health_line(man, max_attempts: int, pending='') -> str:
    health = man.health(max_attempts)
    parts = []
    if health["pending"]:
        parts.append(f"{_plural(health['pending'], 'session')} not yet captured")
    if health["retrying"]:
        parts.append(f"{_plural(health['retrying'], 'session')} retrying")
    if health["failed"]:
        reason = man.failed(max_attempts)[0]["last_error"] or "unknown error"
        parts.append(f"{health['failed']} failed: {reason[:160]}")
    if health["terminal"]:
        parts.append(f"{_plural(health['terminal'], 'session')} terminal")
    backlog = len(man.unconsolidated())
    reason = re.search(r'status update pending \(([^;\n)]+)', pending)
    if backlog:
        why = reason[1][:100] if reason else 'reconciliation pending'
        parts.append(f"{_plural(backlog, 'captured session')} awaiting consolidation ({why})")
        preview = next((line for line in pending.splitlines() if 'status update pending' not in line and line.strip()), '')
        if preview:
            preview = re.sub(r'^- \[ \] \S+ \[\[[^\]]+\]\]\s*[^\w]*', '', preview)
            preview = re.sub(r'\s*(?:capture-ref:\S+|\[\[[^\]]+\]\])\s*$', '', preview).strip()
            if 0 < len(preview) <= PENDING_PREVIEW_CAP:
                parts.append('pending: ' + preview)  # a complete short item, never a cut fragment
    elif 'status update pending' in pending:
        why = f' ({reason[1][:100]})' if reason else ''
        parts.append(f"status update pending{why}")
    if 'status replay refused' in pending:
        parts.append('status replay refused')
    return f"Capture health: {', '.join(parts)}" if parts else ""


def startup_package(cfg, cwd: str) -> str:
    """Reserve health and omission pointer first, then admit complete status items in priority order."""
    from . import notes, projects, status
    from .pipeline import open_manifest
    project = projects.resolve_session(cwd, cfg.projects_map, create=False)
    head = [f"Agent memory: Obsidian vault at {cfg.vault}. Session notes (0-Sessions) are HISTORICAL; project notes (1-Projects) are CURRENT state.",
            'When asked what was previously decided, implemented or configured, search the vault first: '
            '`mem search "<terms>"` (memory skill), never guess.']
    page, pending, note, legacy, recent = status.empty(), '', None, '', ''
    note_text, note_label = '', ''
    man = open_manifest(cfg)
    try:
        if project != projects.UNSORTED:
            note = projects.note_path(cfg.vault, project)
            note_label = note.name
            head.append(f"Project: {project} - {note}")
            if note.exists():
                try:
                    text = note.read_text(encoding='utf-8')
                    note_text = text
                    pending = '\n'.join(notes.pending_lines(note, text))
                    block = notes.get_block(text, notes.STATE_BEGIN, notes.STATE_END)
                    try:
                        page = status.live_page(block)
                    except ValueError:
                        current = re.search(r'(?ms)^### Current state\s*\n(.*?)(?=^### |\Z)', block)
                        if current:
                            legacy = 'Legacy current state (page not migrated):\n' + current[1].strip()[:STATE_CAP]
                        pending += ' status update pending (page not migrated)'
                except (ValueError, OSError):
                    pending += ' status update pending (page not migrated)'
            entries = []
            for row in man.query_captures(project=project, limit=3):
                title = ' '.join(row['title'].split())[:80]
                link = row['note_path'].removesuffix('.md')
                source = f'[[{link}|{title}]]' if len(link) <= 220 and not re.search(r'[\r\n\[\]|]', link) else title
                entries.append(f"{row['date'][:10]} {source}")
            if entries:
                recent = 'Recent sessions: ' + '; '.join(entries)
        head += [line for line in (_git_line(cwd), _health_line(man, cfg.max_attempts, pending)) if line]
    finally:
        man.close()
    count = sum(len(items) for items in page.values())
    pointer = f"{count} more items in {note_label}" if count else ''
    fixed = '\n'.join(head + ([pointer] if pointer else []))
    eligible = {key: [item for item in page[key] if len(item.text) <= 280] for key in status.STARTUP_ORDER}
    selection = {key: items[:1] for key, items in eligible.items()}
    index_variants = _note_index_variants(note_text)
    minimal_index = index_variants[-1] if index_variants else ''
    if minimal_index and len('\n'.join(head + ([pointer] if pointer else []) + [minimal_index])) > CONTEXT_CAP:
        minimal_index = ''  # not even the names fit with the mandatory lines: no index this session

    def fitted_lines(candidate):
        lines = _startup_lines(head, candidate)
        selected = sum(len(items) for items in candidate.values())
        if selected < count:
            lines.append(f"{count - selected} more items in {note_label}")
        if legacy:
            if len('\n'.join(lines + [legacy] + ([minimal_index] if minimal_index else []))) <= CONTEXT_CAP:
                lines.append(legacy)
        if minimal_index:
            lines.append(minimal_index)  # held against status items, then enriched once items are admitted
        return lines

    if len(fixed) > CONTEXT_CAP:
        # Head alone overflows (a very long vault or note path): keep whole lines only, never a slice.
        health = next((line for line in head if line.startswith('Capture health:')), '')
        essential = []
        for line in (health, pointer):
            if line and len('\n'.join(essential + [line])) <= CONTEXT_CAP:
                essential.append(line)
        chosen = []
        for line in head:
            if line in essential:
                continue
            if len('\n'.join(chosen + [line] + essential)) <= CONTEXT_CAP:
                chosen.append(line)
        return '\n'.join(chosen + essential)
    if len('\n'.join(fitted_lines(selection))) > CONTEXT_CAP:
        # Not every section's first item fits: admit first items in startup order while they fit.
        selection = {key: [] for key in eligible}
        for key in status.STARTUP_ORDER:
            trial = {**selection, key: eligible[key][:1]}
            if eligible[key] and len('\n'.join(fitted_lines(trial))) <= CONTEXT_CAP:
                selection = trial

    max_items = max((len(items) for items in eligible.values()), default=0)
    for rank in range(1, max_items):
        for key in status.STARTUP_ORDER:
            if rank < len(eligible[key]) and len(selection[key]) == rank:
                trial = {**selection, key: selection[key] + [eligible[key][rank]]}
                if len('\n'.join(fitted_lines(trial))) <= CONTEXT_CAP:
                    selection = trial
    lines = fitted_lines(selection)
    if minimal_index and minimal_index in lines:
        base = [line for line in lines if line != minimal_index]
        richer = _note_index(note_text, CONTEXT_CAP - len('\n'.join(base)) - (1 if base else 0))
        if richer:  # upgrade to counts, then dates, only after status items are admitted
            lines = base + [richer]
    recent_budget = CONTEXT_CAP - len('\n'.join(lines)) - (1 if recent else 0)
    recent = _fit_recent(recent, recent_budget)
    if recent:
        lines.append(recent)
    return '\n'.join(lines)


def _fit_recent(recent, budget):
    prefix = 'Recent sessions: '
    if not recent.startswith(prefix) or budget < len(prefix):
        return ''
    entries = recent[len(prefix):].split('; ')
    kept = []
    for entry in entries:
        candidate = prefix + '; '.join(kept + [entry])
        if len(candidate) > budget:
            break
        kept.append(entry)
    if len(kept) == len(entries):
        return prefix + '; '.join(kept)
    if not kept:
        return ''  # a lone marker says nothing; the section index names the sessions instead
    candidate = prefix + '; '.join(kept + ['…'])
    return candidate if len(candidate) <= budget else prefix + '; '.join(kept)


def _startup_lines(head, selection):
    """Section headings in startup order, with compact source refs so each item takes less of the cap."""
    from . import status
    lines = list(head)
    for key in status.STARTUP_ORDER:
        if selection[key]:
            lines.append(f"### {status.SECTIONS[key][0]}")
            lines += [f"- {item.text} {_compact_ref(item.ref)}" for item in selection[key]]
    return lines


def _compact_ref(ref):
    """Session date and evidence anchor, e.g. (2026-10-17#e2). The project page keeps the full link."""
    from . import status
    date, anchor = status._ref_date(ref), re.search(r'#e\d+', ref)
    return f"({date}{anchor[0]})" if date and anchor else ref


def is_devin_env() -> bool:
    """True inside a Devin CLI/Desktop session (its hooks inherit the agent's environment)."""
    return bool(os.environ.get("CHISEL_SESSION_DB") or os.environ.get("DEVIN_TRACEPARENT")
                or os.environ.get("AI_AGENT", "").startswith("devin"))


def session_start(cfg, agent: str, sid: str, cwd: str) -> dict:
    """Record the start as a hook event (no capture is queued) and return the startup context, if any."""
    from .pipeline import open_manifest
    if sid:
        man = open_manifest(cfg)
        try:
            # Devin fires both the picked-up .claude hook and its own config hook; one context per start.
            if time.time() - man.last_event_ts(agent, sid, "start") < 10:
                return {}
            man.record_event(agent, sid, "start")
        finally:
            man.close()
    ctx = startup_package(cfg, cwd)
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}} if ctx else {}


def main(agent: str) -> int:
    out = {}
    try:
        if os.environ.get(CAPTURE_ENV):
            return 0  # inside our own pipeline: never capture the capturer
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        from . import config, worker
        cfg = config.load()
        if cfg.paused():
            return 0
        event = payload.get("hook_event_name") or payload.get("hookEventName") or ""
        sid = payload.get("session_id") or payload.get("sessionId") or ""
        cwd = payload.get("cwd") or os.environ.get("DEVIN_PROJECT_DIR") or os.getcwd()
        src = payload.get("transcript_path") or ""
        if sid and not src and (agent == "devin" or is_devin_env()):
            # Devin picks up the .claude hook too. Its payloads carry no transcript_path, so remap
            # argv's agent only when a Devin transcript actually exists for this session id.
            from .adapters import find_devin_transcript
            found = find_devin_transcript(sid)
            if found:
                agent, src = "devin", found
        if event == "SessionStart":
            out = session_start(cfg, agent, sid, cwd)
        elif sid and event in ("Stop", "PreCompact", "PostCompaction", "SessionEnd", "SubagentStop"):
            if event == "SubagentStop":
                return 0
            worker.enqueue(cfg, agent, sid, event="end" if event == "SessionEnd" else "turn", source=src, cwd=cwd)
        worker.spawn_detached()
    except Exception as e:  # noqa: BLE001 - a memory hook must never break an agent turn
        try:
            from . import config
            from .logutil import log
            log(config.load(), "hook-error", agent=agent, error=f"{type(e).__name__}: {e}")
        except Exception:  # noqa: BLE001
            pass
    finally:
        sys.stdout.write(json.dumps(out))
        sys.stdout.flush()
    return 0
