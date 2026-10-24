"""Plan status-page migration without vault writes. Apply is reserved for approved P6 runs."""
import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import date as calendar_date
import hashlib
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentmem import notes, status
from agentmem.fsutil import atomic_write, file_lock


@dataclass(frozen=True)
class Entry:
    id: str
    raw: str
    section: str


def inventory(source):
    """Top-level bullets own their continuation and nested lines; paragraphs retain line breaks."""
    entries, current, section = [], [], 'working_now'
    def flush():
        if current:
            entries.append(Entry(f'i{len(entries) + 1}', '\n'.join(current).rstrip(), section))
            current.clear()
    lines = source.splitlines()
    for index, line in enumerate(lines):
        if re.match(r'^#{1,6} ', line):
            flush()
            heading = line.lstrip('# ').lower()
            section = ('superseded' if 'superseded' in heading else
                       next((key for key, (name, _) in status.SECTIONS.items() if name.lower() == heading),
                            'decisions' if 'decision' in heading else 'questions' if 'question' in heading else 'working_now'))
        elif re.match(r'^[-*+] ', line) or re.match(r'^\d+[.)] ', line):
            flush()
            current.append(line)
        elif not line.strip():
            following = next((value for value in lines[index + 1:] if value.strip()), '')
            if current and re.match(r'^(?:[-*+] |\d+[.)] )', current[0]) and following[:1].isspace():
                current.append(line)
            else:
                flush()
        else:
            current.append(line)
    flush()
    return entries


def _convert(entries, date):
    """Keep each occurrence in its own section; code applies the caps, git-live and text checks.
    Superseded occurrences are never placed, so they reach history only."""
    sections = status.empty()
    for entry in entries:
        if entry.section in sections:
            sections[entry.section].append({'keep': entry.id})
    return dict(sections=sections, displaced=[])


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def _inside(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('plan path escapes vault')
    return path


def _project(path):
    return path.parent.name


def build_plan(vault, path, converter, date):
    source_bytes = path.read_bytes()
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    history = notes.project_history_path(path)
    history_hash = _hash(history)
    source = source_bytes.decode('utf-8').replace('\r\n', '\n')
    block = notes.get_block(source, notes.STATE_BEGIN, notes.STATE_END)
    if block == notes.EMPTY_STATE:
        entries = []
    else:
        entries = inventory(block)
    update = converter(entries, date)
    if (not isinstance(update, dict) or not isinstance(update.get('sections'), dict)
            or set(update['sections']) != set(status.SECTIONS)
            or any(not isinstance(items, list) for items in update['sections'].values())):
        raise ValueError('invalid migration StatusUpdate')
    by_id = {e.id: e for e in entries}
    old, requested, kept = status.empty(), status.empty(), set()
    for section, choices in update['sections'].items():
        for choice in choices:
            identity = choice.get('keep') if isinstance(choice, dict) else None
            if not isinstance(identity, str) or identity not in by_id or identity in kept:
                continue
            entry = by_id[identity]
            text = re.sub(r'^(?:[-*+] |\d+[.)] )', '', entry.raw)
            match = re.fullmatch(r'(.+) (' + status.REF.pattern + r')', text)
            text, ref = (match[1], match[2]) if match else (text, f'migration:{date}')
            if not status.valid_text(text, new=True):
                continue
            old[section].append(status.Item(identity, text, ref))
            requested[section].append({'keep': identity})
            kept.add(identity)
    result = status.reconcile(old, dict(sections=requested, displaced=[]), set())
    placed_ids = {i.id for items in result.sections.values() for i in items}
    displaced_reasons = {d.item.id: d.reason for d in result.displaced}
    records = []
    for entry in entries:
        if entry.id in placed_ids:
            continue
        reason = displaced_reasons.get(entry.id, 'migration')
        records.append(dict(key=f'migration:{source_hash}:{entry.id}',
                            text=f'- {date} [{entry.section}; {reason}; migration:{date}]\n{entry.raw}'))
    history_link = history.relative_to(vault).with_suffix('').as_posix()
    after = notes.set_block(source, notes.STATE_BEGIN, notes.STATE_END, status.render(result.sections, history_link))
    joined = after + '\n' + '\n'.join(r['text'] for r in records)
    unaccounted = [e.id for e in entries if e.raw not in joined]
    if unaccounted:
        raise ValueError('unaccounted migration occurrences: ' + ', '.join(unaccounted))
    return dict(version=1, path=path.relative_to(vault).as_posix(), project=_project(path), date=date,
                source_hash=source_hash, history_hash=history_hash, after=after, records=records,
                old_entries=len(entries), placed=len(placed_ids), unaccounted=0,
                displaced=dict(Counter(displaced_reasons.get(e.id, 'migration') for e in entries if e.id not in placed_ids)),
                restored=result.events)


def plan_vault(vault, out, *, converter=None, date=None):
    vault, out = Path(vault).resolve(), Path(out).resolve()
    if out.is_relative_to(vault):
        raise ValueError('dry-run output must be outside vault')
    out.mkdir(parents=True, exist_ok=True)
    date = date or calendar_date.today().isoformat()
    report = []
    for path in sorted((vault / '1-Projects').rglob('*.md')):
        relative = path.relative_to(vault).as_posix()
        source = path.read_text(encoding='utf-8')
        classification = ('handoff' if re.search(r'(?mi)^type:\s*handoff\s*$', source) or 'handoff' in path.stem.lower()
                          else 'status' if notes.STATE_BEGIN in source else 'legacy')
        row = dict(path=relative, classification=classification)
        if classification == 'status':
            try:
                block = notes.get_block(source, notes.STATE_BEGIN, notes.STATE_END)
                if block.startswith(status.FORMAT):
                    status.parse(block)
                    row['skipped'] = 'already migrated'
                else:
                    plan = build_plan(vault, path, converter or _convert, date)
                    name = hashlib.sha256(relative.encode('utf-8')).hexdigest() + '.json'
                    atomic_write(out / name, json.dumps(plan, ensure_ascii=False, indent=2) + '\n')
                    atomic_write(out / (name + '.page.md'), plan['after'])
                    atomic_write(out / (name + '.history.md'), '\n\n'.join(r['text'] for r in plan['records']) + '\n')
                    row.update({key: plan[key] for key in ('old_entries', 'placed', 'unaccounted', 'displaced', 'restored')})
                    row['plan'] = name
            except ValueError as error:
                row['skipped'] = str(error)
        report.append(row)
    atomic_write(out / 'report.json', json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return report


def apply_plan(vault, plan_path, backup):
    if backup is None:
        raise ValueError('apply requires backup path')
    vault, backup = Path(vault).resolve(), Path(backup).resolve()
    if backup == vault or backup.is_relative_to(vault):
        raise ValueError('backup must be separate from vault')
    plan = json.loads(Path(plan_path).read_text(encoding='utf-8'))
    path = _inside(vault, plan['path'])
    history = notes.project_history_path(path)
    lock = vault / '_meta' / 'locks' / f"project-{notes.slug(plan['project'], 12)}.lock"
    with file_lock(lock):
        if _hash(path) != plan['source_hash']:
            raise ValueError('source changed since dry run')
        if _hash(history) != plan['history_hash']:
            raise ValueError('history changed since dry run')
        if (_hash(_inside(backup, plan['path'])) != plan['source_hash']
                or _hash(_inside(backup, history.relative_to(vault))) != plan['history_hash']):
            raise ValueError('backup does not hash-match source and history')
        status.parse(notes.get_block(plan['after'], notes.STATE_BEGIN, notes.STATE_END))
        journal = dict(before=path.read_text(encoding='utf-8'), after=plan['after'], records=plan['records'],
                       project=plan['project'], date=plan['date'])
        atomic_write(notes._status_journal(path), json.dumps(journal, ensure_ascii=False))
        notes.recover_status_update(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--apply', action='store_true', help='P6 approval required; apply existing reviewed plans')
    parser.add_argument('--backup', type=Path)
    args = parser.parse_args(argv)
    if args.apply:
        if not args.backup:
            parser.error('--apply requires --backup')
        rows = json.loads((args.out / 'report.json').read_text(encoding='utf-8'))
        for row in rows:
            if row.get('plan'):
                apply_plan(args.vault, args.out / row['plan'], args.backup)
    else:
        rows = plan_vault(args.vault, args.out)
    print(json.dumps(rows, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
