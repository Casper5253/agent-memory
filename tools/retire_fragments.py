"""Retire release fragment projects. Defaults to a read-only plan; --apply is explicit."""
import argparse
from contextlib import ExitStack
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agentmem import notes
from agentmem.fsutil import atomic_write, file_lock

MAPPINGS = {
    **dict.fromkeys(('Agent Memory Gaps', 'Agent Memory Gate', 'Agent Memory Ground', 'Agent Memory P1b'), 'Agent Memory'),
    **dict.fromkeys(('Hxo Fixb', 'Hxo Fixc', 'Hxo Fixd', 'herdr-orchestration', 'Herdr Orchestration Route'), 'Herdr Orchestration'),
    'Pfd M3 Scale': 'Personal Finance Dashboard',
}
WIKI = re.compile(r'\[\[([^\]\r\n]+)\]\]')
DEFAULT_OUT = ROOT / '.audit/release/retire/plan.json'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def child(vault, relative):
    path = vault / relative
    if not path.resolve().is_relative_to(vault) or path.is_symlink():
        raise ValueError(f'unsafe vault path: {relative}')
    return path


def files_under(vault, folder):
    files = []
    if folder.exists():
        if folder.resolve() != folder.absolute():
            raise ValueError(f'linked source directory: {folder}')
        for path in sorted(folder.rglob('*')):
            child(vault, path.relative_to(vault))
            if path.is_file():
                files.append(dict(path=path.relative_to(folder).as_posix(), sha256=sha(path.read_bytes())))
    return files


def database(vault, writable=False):
    path = child(vault, '_meta/manifest.sqlite')
    if not path.is_file():
        raise ValueError('existing manifest.sqlite required')
    db = sqlite3.connect(path.as_uri() + ('?mode=rw' if writable else '?mode=ro'), uri=True, isolation_level=None)
    db.row_factory = sqlite3.Row
    if not writable:
        db.execute('PRAGMA query_only=ON')
    return db


def manifest_rows(db, fragment):
    result = {}
    for table, keys in (('captures', ('agent', 'session_id', 'part')), ('sessions', ('agent', 'session_id'))):
        rows = [dict(row) for row in db.execute(f'SELECT * FROM {table} WHERE project=? ORDER BY ' + ','.join(keys), (fragment,))]
        result[table] = dict(count=len(rows), sha256=sha(json.dumps(rows, sort_keys=True, default=str).encode()),
                             identities=[{key: row[key] for key in keys} for row in rows])
    return result


def block_count(text, begin, end):
    try:
        return sum(line.startswith('- ') for line in notes.get_block(text, begin, end).splitlines())
    except ValueError:
        return 0


def link_target(inner):
    target = re.split(r'[#|]', inner, maxsplit=1)[0]
    return target.replace('\\', '/').removesuffix('.md').strip()


def fenced_ranges(text):
    """Code examples and verbatim history payloads contain no active wikilinks."""
    ranges, start, fence, offset = [], None, '', 0
    for line in text.splitlines(keepends=True):
        match = re.match(r' {0,3}(`{3,}|~{3,})(.*)', line)
        if match:
            run, rest = match.groups()
            if start is None:
                start, fence = offset, run
            elif run[0] == fence[0] and len(run) >= len(fence) and not rest.strip():
                ranges.append((start, offset + len(line)))
                start = None
        offset += len(line)
    if start is not None:
        ranges.append((start, len(text)))
    return ranges


def build_plan(vault, mappings=None, day=None):
    vault = Path(vault).resolve()
    mappings = MAPPINGS if mappings is None else mappings
    plan = dict(version=1, vault=str(vault), date=day or date.today().isoformat(), fragments=[], links=[], errors=[])
    all_md = sorted(path for path in vault.rglob('*.md') if '.git' not in path.relative_to(vault).parts)
    moves = {}
    preserved = set()
    db = database(vault)
    try:
        db.execute('BEGIN')
        for fragment, parent in mappings.items():
            if not all(value and '/' not in value and '\\' not in value and value not in ('.', '..') for value in (fragment, parent)):
                raise ValueError('invalid project name')
            old = f'1-Projects/{fragment}'
            new = f'4-Archive/merged-fragments/{fragment}'
            source, dest = child(vault, old), child(vault, new)
            if source.exists() and dest.exists():
                plan['errors'].append(f'archive destination already exists while source remains: {fragment}')
            folder = source if source.exists() else dest
            state = 'active' if source.exists() else 'archived' if dest.exists() else 'absent'
            inventory = files_under(vault, folder)
            page = folder / f'{fragment}.md'
            history = folder / 'History.md'
            parent_folder = child(vault, f'1-Projects/{parent}')
            if state != 'absent' and not (parent_folder / f'{parent}.md').is_file():
                plan['errors'].append(f'parent project page missing: {parent}')
            page_text = page.read_bytes().decode('utf-8') if page.is_file() else ''
            inputs = [dict(path=f'{old}/{file.name}', sha256=sha(file.read_bytes()))
                      for file in (page, history) if file.is_file() and file.read_bytes().strip()]
            marker = '<!-- agentmem:fragment-retirement:' + sha(json.dumps([fragment, parent, inventory], sort_keys=True).encode()) + ' -->'
            parent_history = parent_folder / 'History.md'
            parent_text = parent_history.read_bytes().decode('utf-8') if parent_history.exists() else ''
            item = dict(fragment=fragment, parent=parent, source=old, archive=new, state=state,
                files=inventory, files_moved=len(inventory) if state == 'active' else 0,
                copy_sources=inputs, state_items=block_count(page_text, notes.STATE_BEGIN, notes.STATE_END),
                pending_items=block_count(page_text, notes.PENDING_BEGIN, notes.PENDING_END),
                parent_history=parent_history.relative_to(vault).as_posix(),
                parent_history_sha256=sha(parent_history.read_bytes()) if parent_history.exists() else None,
                marker=marker, history_already_copied=marker in parent_text, manifest=manifest_rows(db, fragment))
            item['unconsolidated_captures'] = db.execute(
                'SELECT COUNT(*) FROM captures WHERE project=? AND consolidated=0', (fragment,)).fetchone()[0]
            plan['fragments'].append(item)
            if state == 'absent' and any(row['count'] for row in item['manifest'].values()):
                plan['errors'].append(f'manifest rows remain but fragment source/archive is missing: {fragment}')
            for entry in inventory:
                original, archived = f"{old}/{entry['path']}", f"{new}/{entry['path']}"
                moves[original.removesuffix('.md').casefold()] = (fragment, archived.removesuffix('.md'))
                preserved.add((folder / entry['path']).relative_to(vault).as_posix())
        db.execute('COMMIT')
    finally:
        db.close()
    final_names = {}
    for path in all_md:
        relative = path.relative_to(vault).as_posix()
        final = moves.get(relative.removesuffix('.md').casefold(), (None, relative.removesuffix('.md')))[1]
        final_names.setdefault(Path(final).name.casefold(), set()).add(final.casefold())
    for path in all_md:
        relative = path.relative_to(vault).as_posix()
        child(vault, relative)
        text = path.read_bytes().decode('utf-8')
        fences = fenced_ranges(text)
        for match in WIKI.finditer(text):
            if any(start <= match.start() < end for start, end in fences):
                continue
            target = link_target(match[1])
            key = target.casefold()
            candidates = [(old, value) for old, value in moves.items()
                          if old == key or old.endswith('/' + key)]
            if not candidates:
                continue
            exact = [pair for pair in candidates if pair[0] == key]
            candidates = exact or candidates
            for old, (fragment, new) in candidates:
                unique_match = len(candidates) == 1
                filename_unique = len(final_names.get(Path(old).name.casefold(), ())) == 1
                immutable = relative.startswith('0-Sessions/') or relative in preserved
                rewrite = unique_match and not immutable
                explicit_path = '/' in target
                risk = not unique_match or (immutable and not filename_unique)
                plan['links'].append(dict(fragment=fragment, source=relative, start=match.start(),
                    target=target, replacement=new, rewrite=rewrite, at_risk=risk,
                    ambiguous=not unique_match, immutable=immutable, filename_fallback=immutable and explicit_path and filename_unique,
                    source_sha256=sha(path.read_bytes())))
    return plan


def summary(plan):
    result = []
    for item in plan['fragments']:
        links = [link for link in plan['links'] if link['fragment'] == item['fragment']]
        result.append(dict(fragment=item['fragment'], parent=item['parent'], state=item['state'],
            state_items_copied=item['state_items'], pending_items_copied=item['pending_items'],
            verbatim_sources=len(item['copy_sources']), files_moved=item['files_moved'],
            manifest_rows_remapped={key: value['count'] for key, value in item['manifest'].items()},
            unconsolidated_captures=item['unconsolidated_captures'],
            links_found=len(links), links_rewritten=sum(link['rewrite'] for link in links),
            links_at_risk=sum(link['at_risk'] for link in links),
            immutable_filename_fallbacks=sum(link['filename_fallback'] for link in links)))
    return dict(fragments=result, errors=plan['errors'])


def history_section(vault, item, day):
    folder = child(vault, item['source'] if item['state'] == 'active' else item['archive'])
    text = f"\n\n{item['marker']}\n## Merged from fragment {item['fragment']} ({day})\n"
    for source in item['copy_sources']:
        content = (folder / Path(source['path']).name).read_bytes().decode('utf-8')
        fence = '`' * max(3, max((len(run) + 1 for run in re.findall(r'`+', content)), default=3))
        text += f"\nSource: {source['path']}\n\n{fence}markdown\n" + content
        text += ('' if content.endswith('\n') else '\n') + fence + '\n'
    text += '\nArchived files:\n' + ''.join(f"- [[{item['archive']}/{file['path']}]]\n" for file in item['files'])
    return text


def apply_plan(plan):
    if plan['errors']:
        raise ValueError('; '.join(plan['errors']))
    vault = Path(plan['vault']).resolve()
    if not any(item['state'] != 'absent' for item in plan['fragments']):
        return
    with ExitStack() as stack:
        stack.enter_context(file_lock(child(vault, '_meta/locks/worker.lock')))
        projects = {item[key] for item in plan['fragments'] for key in ('parent', 'fragment')}
        # Names can share a lock file ("Herdr Orchestration", "herdr-orchestration"); taking one twice deadlocks.
        for name in sorted({notes.slug(project, 12) for project in projects}):
            stack.enter_context(file_lock(child(vault, f'_meta/locks/project-{name}.lock')))
        db = database(vault, writable=True)
        written, moved = {}, []
        try:
            db.execute('BEGIN IMMEDIATE')
            changes = {}
            for item in plan['fragments']:
                if manifest_rows(db, item['fragment']) != item['manifest']:
                    raise ValueError(f"manifest changed: {item['fragment']}")
                if item['state'] == 'absent':
                    continue
                folder = child(vault, item['source'] if item['state'] == 'active' else item['archive'])
                if files_under(vault, folder) != item['files']:
                    raise ValueError(f"source files changed: {item['fragment']}")
                target = child(vault, item['parent_history'])
                old = target.read_bytes() if target.exists() else None
                if (sha(old) if old is not None else None) != item['parent_history_sha256']:
                    raise ValueError(f"parent history changed: {item['parent']}")
                changes.setdefault(target, old.decode('utf-8') if old is not None else f"# {item['parent']} history\n")
            rewrites = {}
            for link in plan['links']:
                if link['rewrite']:
                    path = child(vault, link['source'])
                    if sha(path.read_bytes()) != link['source_sha256']:
                        raise ValueError(f"link source changed: {link['source']}")
                    rewrites.setdefault(path, {})[link['start']] = link
            for path, entries in rewrites.items():
                text = changes.get(path, path.read_bytes().decode('utf-8'))
                def replace(match):
                    link = entries.get(match.start())
                    if link is None:
                        return match[0]
                    suffix = match[1][len(re.split(r'[#|]', match[1], maxsplit=1)[0]):]
                    extension = '.md' if re.split(r'[#|]', match[1], maxsplit=1)[0].endswith('.md') else ''
                    return '[[' + link['replacement'] + extension + suffix + ']]'
                changes[path] = WIKI.sub(replace, text)
            for item in plan['fragments']:
                if item['state'] != 'absent' and not item['history_already_copied']:
                    target = child(vault, item['parent_history'])
                    changes[target] += history_section(vault, item, plan['date'])
            for path, text in changes.items():
                old = path.read_bytes() if path.exists() else None
                if old is not None and text.encode('utf-8') == old:
                    continue
                written[path] = old
                atomic_write(path, text)
            for item in plan['fragments']:
                if item['state'] == 'active':
                    source, dest = child(vault, item['source']), child(vault, item['archive'])
                    if dest.exists():
                        raise ValueError(f'archive collision: {dest}')
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    source.rename(dest)
                    moved.append((source, dest))
                    if files_under(vault, dest) != item['files']:
                        raise ValueError('archived bytes differ')
                for table in ('captures', 'sessions'):
                    db.execute(f'UPDATE {table} SET project=? WHERE project=?', (item['parent'], item['fragment']))
            db.execute('COMMIT')
        except BaseException:
            if db.in_transaction:
                db.execute('ROLLBACK')
            for source, dest in reversed(moved):
                dest.rename(source)
            for path, old in reversed(list(written.items())):
                if old is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write(path, old.decode('utf-8'))
            raise
        finally:
            db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault', type=Path, required=True)
    parser.add_argument('--out', type=Path, default=DEFAULT_OUT)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', help='default; write only the plan outside the vault')
    mode.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.out.resolve().is_relative_to(args.vault.resolve()):
        parser.error('--out must be outside the vault')
    plan = build_plan(args.vault)
    plan['summary'] = summary(plan)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(args.out, json.dumps(plan, indent=2, ensure_ascii=False) + '\n')
    if args.apply:
        apply_plan(plan)
    print(json.dumps(dict(mode='apply' if args.apply else 'dry-run', plan=str(args.out), **plan['summary']), indent=2))
    return 1 if plan['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
