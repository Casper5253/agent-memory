"""Restore masked session ids in vault session notes, from evidence only.

Usage:
    python -I tools/restore_session_ids.py plan --vault V --out O
    python -I tools/restore_session_ids.py apply --vault V --plan O/plan.json

plan writes O/plan.json: one entry per masked note that has a single agreeing answer, and a list
of the notes left masked with the reason. apply edits only the session_id frontmatter line of each
planned note, after checking the note still has the hash it had at plan time, and keeps a backup
copy of each touched note outside the vault before it writes.

Evidence, in order of use. A source gives a unique id, an ambiguous set, or nothing.
    capture-state    the agentmem manifest row for the note path, same agent and part
    transcript-path  a full session id in a "- Transcript:" path in the note body
    transcript-file  a transcript on disk whose id ends with the note filename's 8-char suffix,
                     dated within one day of the note's date (Claude and Codex files, OpenCode session list)
Any ambiguous source, or sources that disagree, leave the note masked and listed.
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))

MASKED_LINE = re.compile(rb'^session_id: "?\[REDACTED\]"?(\r?\n)?$')
TRANSCRIPT_LINE = re.compile(r"(?m)^- Transcript: `([^`]*)`")
ID_IN_PATH = re.compile(r"ses_[0-9A-Za-z]+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
DATE_WINDOW = timedelta(days=1)


class SetupError(Exception):
    """A precondition for a restore run is not met."""


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def frontmatter_lines(data):
    """Lines of the frontmatter block as (index, bytes), or None when the note has no frontmatter."""
    lines = data.splitlines(keepends=True)
    if not lines or lines[0].rstrip(b"\r\n") != b"---":
        return None
    for index in range(1, len(lines)):
        if lines[index].rstrip(b"\r\n") == b"---":
            return [(number, lines[number]) for number in range(1, index)]
    return None


def is_masked(data):
    lines = frontmatter_lines(data) or []
    return sum(1 for _, line in lines if MASKED_LINE.match(line)) == 1


def frontmatter_value(data, key):
    text = data.decode("utf-8", errors="replace")
    match = re.search(rf"(?m)^{key}: \"?([^\"\r\n]*)\"?\r?$", text.split("\n---", 1)[0] if text.startswith("---") else "")
    return match[1] if match else None


def filename_suffix(name):
    stem = name[:-3] if name.endswith(".md") else name
    stem = re.sub(r"-p\d+$", "", stem)
    token = stem.rsplit("-", 1)[-1]
    return token if len(token) == 8 else None


def within_window(day, note_date):
    return abs((day - note_date).days) <= DATE_WINDOW.days


def capture_candidates(manifest, rel, agent, part):
    ids = {row[0] for row in manifest.execute(
        "SELECT session_id FROM captures WHERE note_path = ? AND agent = ? AND part = ?", (rel, agent, part))}
    return ids


def transcript_path_candidates(body):
    found = set()
    for path in TRANSCRIPT_LINE.findall(body):
        found.update(ID_IN_PATH.findall(path))
    return found


def transcript_file_candidates(agent, suffix, note_date, roots, opencode_sessions):
    """(id, source) pairs for transcripts on disk, or OpenCode sessions, whose id ends with the suffix and matches the date."""
    matches = {}
    if agent == "claude" and roots.get("claude") and roots["claude"].is_dir():
        for path in roots["claude"].glob("*/*.jsonl"):
            day = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).date()
            if path.stem.endswith(suffix) and within_window(day, note_date):
                matches.setdefault(path.stem, str(path))
    if agent == "codex" and roots.get("codex") and roots["codex"].is_dir():
        for path in roots["codex"].glob("**/rollout-*.jsonl"):
            found = UUID.findall(path.stem[-36:])
            day = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).date()
            if found and found[-1] == path.stem[-36:] and path.stem.endswith(suffix) and within_window(day, note_date):
                matches.setdefault(found[-1], str(path))
    if agent == "opencode":
        for session in opencode_sessions():
            day = datetime.fromtimestamp(session["created"] / 1000, tz=timezone.utc).date()
            if session["id"].endswith(suffix) and within_window(day, note_date):
                matches.setdefault(session["id"], "opencode session list")
    return matches


def opencode_session_list():
    try:
        result = subprocess.run(["opencode", "session", "list", "--format", "json"],
                                capture_output=True, timeout=120, text=True, encoding="utf-8")
        if result.returncode:
            return []
        return json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []


def resolve(rel, data, manifest, roots, opencode_sessions):
    """Return ("restore", id, sources, detail) or ("masked", reason, candidates)."""
    agent = frontmatter_value(data, "agent")
    part = int(frontmatter_value(data, "part") or 1)
    note_date_text = frontmatter_value(data, "date")
    body = data.decode("utf-8", errors="replace").split("\n---\n", 1)[-1]
    suffix = filename_suffix(Path(rel).name)
    note_date = date.fromisoformat(note_date_text) if note_date_text else None

    sources = {
        "capture-state": ("unique", capture_candidates(manifest, rel, agent, part)),
        "transcript-path": ("unique", transcript_path_candidates(body)),
    }
    if suffix and note_date:
        files = transcript_file_candidates(agent, suffix, note_date, roots, opencode_sessions)
        sources["transcript-file"] = ("unique", set(files)) if len(files) <= 1 else ("ambiguous", sorted(files))
    else:
        files = {}
    ambiguous = {name: ids for name, (kind, ids) in sources.items() if kind == "ambiguous" or len(ids) > 1}
    if ambiguous:
        return ("masked", "ambiguous", {name: sorted(ids) for name, ids in ambiguous.items()})
    answers = {}
    for name, (_, ids) in sources.items():
        for sid in ids:
            answers.setdefault(sid, []).append(name)
    if not answers:
        return ("masked", "no_evidence", {})
    if len(answers) > 1:
        return ("masked", "conflicting_evidence", {sid: names for sid, names in answers.items()})
    (sid, names), = answers.items()
    detail = {"transcript_file": files.get(sid)} if "transcript-file" in names else {}
    return ("restore", sid, names, detail)


def replace_session_line(data, session_id):
    """Replace the single masked session_id line, keeping its line ending and every other byte."""
    lines = data.splitlines(keepends=True)
    fm = frontmatter_lines(data) or []
    hits = [(number, line) for number, line in fm if MASKED_LINE.match(line)]
    if len(hits) != 1:
        raise SetupError("expected exactly one masked session_id line")
    number, line = hits[0]
    eol = b"\r\n" if line.endswith(b"\r\n") else b"\n" if line.endswith(b"\n") else b""
    lines[number] = b'session_id: "' + session_id.encode("ascii") + b'"' + eol
    return b"".join(lines)


def inside(path, root):
    return Path(path).resolve().is_relative_to(Path(root).resolve())


def open_manifest(vault):
    path = vault / "_meta" / "manifest.sqlite"
    if not path.is_file():
        raise SetupError("manifest.sqlite is missing; capture-state evidence is unavailable")
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def build_plan(vault, out, roots=None, opencode_sessions=opencode_session_list):
    if not vault.is_dir():
        raise SetupError("--vault is not a directory")
    if inside(out, vault):
        raise SetupError("--out must be outside the vault")
    if (out / "plan.json").exists():
        raise SetupError("--out already holds plan.json; refusing to overwrite")
    roots = roots if roots is not None else {"claude": Path.home() / ".claude" / "projects",
                                             "codex": Path.home() / ".codex" / "sessions"}
    entries, masked = [], []
    with closing(open_manifest(vault)) as manifest:
        for path in sorted((vault / "0-Sessions").rglob("*.md")):
            data = path.read_bytes()
            if not is_masked(data):
                continue
            rel = path.relative_to(vault).as_posix()
            result = resolve(rel, data, manifest, roots, opencode_sessions)
            if result[0] == "restore":
                _, sid, names, detail = result
                entries.append(dict(note=rel, sha256=sha256_bytes(data), session_id=sid, evidence=names[0],
                                    agreeing=names, detail=detail))
            else:
                masked.append(dict(note=rel, reason=result[1], candidates=result[2]))
    out.mkdir(parents=True, exist_ok=True)
    plan = dict(vault=str(vault), restored=entries, still_masked=masked)
    (out / "plan.json").write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    return plan


def apply_plan(vault, plan_path):
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if Path(plan["vault"]).resolve() != vault.resolve():
        raise SetupError("plan was made for a different vault")
    backups = plan_path.parent / "backups"
    if inside(backups, vault):
        raise SetupError("backups must be outside the vault")
    applied, refused = [], []
    for entry in plan["restored"]:
        note = vault / entry["note"]
        data = note.read_bytes()
        if sha256_bytes(data) != entry["sha256"]:
            refused.append(dict(note=entry["note"], reason="hash_changed_since_plan"))
            continue
        new = replace_session_line(data, entry["session_id"])
        backup = backups / entry["note"]
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(data)
        temp = note.with_name(note.name + ".restore-tmp")
        try:
            temp.write_bytes(new)
            os.replace(temp, note)
        finally:
            if temp.exists():
                temp.unlink()
        if sha256_bytes(note.read_bytes()) != sha256_bytes(new):
            raise SetupError(f"write check failed for {entry['note']}")
        applied.append(dict(note=entry["note"], session_id=entry["session_id"], evidence=entry["evidence"]))
    return dict(applied=applied, refused=refused)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Restore masked session ids from evidence")
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="list restorable and still-masked notes")
    plan.add_argument("--vault", required=True)
    plan.add_argument("--out", required=True)
    apply = commands.add_parser("apply", help="write the planned ids into the vault")
    apply.add_argument("--vault", required=True)
    apply.add_argument("--plan", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            result = build_plan(Path(args.vault), Path(args.out))
            counts = {}
            for entry in result["restored"]:
                counts[entry["evidence"]] = counts.get(entry["evidence"], 0) + 1
            print("restorable by evidence: " + json.dumps(counts, sort_keys=True))
            print(f"restorable {len(result['restored'])} still_masked {len(result['still_masked'])}")
        else:
            result = apply_plan(Path(args.vault), Path(args.plan))
            print(f"applied {len(result['applied'])} refused {len(result['refused'])}")
            for item in result["refused"]:
                print("refused: " + json.dumps(item, ensure_ascii=False))
    except SetupError as error:
        print("setup error: " + str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
