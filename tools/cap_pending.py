"""Bound existing project pending blocks. Default dry run; lead applies after backup."""
import argparse
from contextlib import ExitStack
from datetime import date
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agentmem import notes
from agentmem.fsutil import atomic_write, file_lock


def digest(data):
    return hashlib.sha256(data).hexdigest()


def build_plan(vault, day=None):
    vault = Path(vault).resolve()
    day = day or date.today().isoformat()
    plan = dict(vault=str(vault), date=day, cap=notes.PENDING_CAP, pages=[], errors=[])
    for folder in sorted((vault / '1-Projects').iterdir()):
        path = folder / (folder.name + '.md')
        if not path.is_file():
            continue
        if path.resolve() != path.absolute() or not path.resolve().is_relative_to(vault):
            raise ValueError('linked project page is not supported')
        if notes._status_journal(path).exists():
            plan['errors'].append(f'unfinished status journal: {folder.name}')
            continue
        raw = path.read_bytes()
        text = raw.decode('utf-8').replace('\r\n', '\n')
        if notes.PENDING_BEGIN not in text:
            continue
        history_path = notes.project_history_path(path)
        if history_path.resolve() != history_path.absolute():
            raise ValueError('linked history is not supported')
        history_raw = history_path.read_bytes() if history_path.exists() else b''
        history = history_raw.decode('utf-8').replace('\r\n', '\n')
        lines = notes.pending_lines(path, text, history)
        after, records = notes.project_pending(path, text, folder.name, day, lines, history)
        plan['pages'].append(dict(project=folder.name, path=path.relative_to(vault).as_posix(),
            sha256=digest(raw), history_sha256=digest(history_raw), pending_items=len(lines),
            overflow_items=max(0, len(lines) - notes.PENDING_CAP), history_records=len(records),
            before_bytes=len(raw), after_bytes=len(after.encode('utf-8')),
            changed=after != text or bool(records)))
    return plan


def apply_plan(plan):
    if plan['errors']:
        raise ValueError('; '.join(plan['errors']))
    vault = Path(plan['vault']).resolve()
    with ExitStack() as stack:
        stack.enter_context(file_lock(vault / '_meta/locks/worker.lock'))
        for item in sorted(plan['pages'], key=lambda row: row['project']):
            stack.enter_context(file_lock(vault / '_meta/locks' / f"project-{notes.slug(item['project'], 12)}.lock"))
        for item in plan['pages']:
            path = vault / item['path']
            history = notes.project_history_path(path)
            if (digest(path.read_bytes()) != item['sha256'] or
                    digest(history.read_bytes() if history.exists() else b'') != item['history_sha256'] or
                    notes._status_journal(path).exists()):
                raise ValueError(f"page/history changed since plan: {item['project']}")
        for item in plan['pages']:
            if item['changed']:
                notes.cap_project_pending(vault / item['path'], item['project'], plan['date'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault', type=Path, required=True)
    parser.add_argument('--out', type=Path, default=ROOT / '.audit/release/pending/plan.json')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--dry-run', action='store_true')
    modes.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.out.resolve().is_relative_to(args.vault.resolve()):
        parser.error('--out must be outside the vault')
    plan = build_plan(args.vault)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(args.out, json.dumps(plan, indent=2) + '\n')
    if args.apply:
        apply_plan(plan)
    print(json.dumps(dict(mode='apply' if args.apply else 'dry-run', plan=str(args.out), **plan), indent=2))
    return 1 if plan['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
