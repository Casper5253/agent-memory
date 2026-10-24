"""Structured project status, model boundary validation, and deterministic retention."""
from dataclasses import dataclass, field
import re


SECTIONS = {
    'purpose': ('Purpose and direction', 3),
    'working_now': ('What works now', 8),
    'latest': ('Latest progress', 5),
    'active': ('Active work and blockers', 6),
    'questions': ('Open questions', 6),
    'decisions': ('Key decisions (decision, reason)', 8),
    'next': ('Next steps', 5),
}
STARTUP_ORDER = ('purpose', 'active', 'next', 'working_now', 'decisions', 'questions', 'latest')
FORMAT = '<!-- agentmem:status:v2 -->'
REF = re.compile(r'(?:\[\[[^\[\]\n#|]+#e[1-9][0-9]*\]\]|migration:\d{4}-\d{2}-\d{2})')
GIT_PATTERNS = tuple(re.compile(pattern, re.I) for pattern in (
    r'\bon branch\s+\S+', r'\bbranch\s*:\s*\S+', r'\bHEAD\s+(?:is|at|=|:)\s*\S+',
    r'\b(?:HEAD|commit|sha)\s+(?:at\s+|is\s+)?`?[0-9a-f]{7,40}\b',
    r'\bat\s+`?(?=[0-9a-f]{0,39}[a-f])[0-9a-f]{7,40}\b', r'\buncommitted\b',
    r'\b(?:ahead(?:\s+of)?|behind)\s+(?:origin/|upstream\b|remote\b|main\b|master\b|by\s+\d+\s+commits?\b)',
    r'\bworking (?:tree|directory)\s+(?:is\s+)?(?:clean|dirty)\b',
))


@dataclass(frozen=True)
class Item:
    id: str
    text: str
    ref: str


@dataclass(frozen=True)
class Displaced:
    item: Item
    section: str
    reason: str
    evidence: str = ''
    claim: str = ''


@dataclass
class Result:
    sections: dict
    displaced: list = field(default_factory=list)
    events: list = field(default_factory=list)


def empty():
    return {key: [] for key in SECTIONS}


def valid_text(text, *, new=False):
    return (isinstance(text, str) and bool(text.strip()) and text == text.strip()
            and not any(c in text for c in ('\n', '\r')) and '<!--' not in text
            and (not new or len(text) <= 280))


def render(sections, history='History'):
    if set(sections) != set(SECTIONS) or not re.fullmatch(r'[^\[\]\n|]+', history):
        raise ValueError('invalid status sections or history link')
    lines = [FORMAT]
    for key, (heading, _) in SECTIONS.items():
        lines += ['', f'### {heading}']
        for item in sections[key]:
            if not valid_text(item.text) or not REF.fullmatch(item.ref):
                raise ValueError('invalid stored status item')
            lines.append(f'- {item.text} {item.ref}')
    lines += ['', f'History: [[{history}]]']
    return '\n'.join(lines)


def parse(text):
    lines = text.splitlines()
    if not lines or lines.pop(0) != FORMAT:
        raise ValueError('page not migrated')
    result, count = empty(), 0
    for key, (heading, _) in SECTIONS.items():
        if lines[:2] != ['', f'### {heading}']:
            raise ValueError('page not migrated')
        del lines[:2]
        while lines and lines[0].startswith('- '):
            line = lines.pop(0)
            match = re.fullmatch(r'- (.+) (' + REF.pattern + r')', line)
            if not match or not valid_text(match[1]):
                raise ValueError('page not migrated')
            count += 1
            result[key].append(Item(f'i{count}', match[1], match[2]))
    if len(lines) != 2 or lines[0] or not re.fullmatch(r'History: \[\[[^\[\]\n|]+\]\]', lines[1]):
        raise ValueError('page not migrated')
    return result


def live_page(text):
    from .notes import EMPTY_STATE
    return empty() if text == EMPTY_STATE else parse(text)


def model_items(sections):
    return {key: [dict(id=i.id, text=i.text, ref=i.ref) for i in items] for key, items in sections.items()}


def evidence_refs(summary, link):
    target = link[2:-2] if link.startswith('[[') and link.endswith(']]') else link
    return {f'[[{target}#e{n}]]': citation['claim'] for n, citation in enumerate(summary.get('citations', []), 1)
            if isinstance(citation, dict) and isinstance(citation.get('claim'), str) and citation['claim'].strip()}


def _matches_claim(text, ref, allowed):
    claim = allowed.get(ref) if isinstance(allowed, dict) else None
    return (isinstance(claim, str) and bool(claim.strip())
            and re.sub(r'\s+', ' ', claim).strip() in re.sub(r'\s+', ' ', text).strip())


def reconcile(old, update, allowed):
    if (not isinstance(update, dict) or not isinstance(update.get('sections'), dict)
            or set(update['sections']) != set(SECTIONS)
            or any(not isinstance(items, list) for items in update['sections'].values())
            or not isinstance(update.get('displaced'), list)):
        raise ValueError('invalid StatusUpdate JSON')
    originals = {i.id: (section, i) for section, items in old.items() for i in items}
    result, handled = Result(empty()), set()
    counter = len(originals)
    for section, items in update['sections'].items():
        for entry in items:
            if isinstance(entry, dict) and set(entry) == {'keep'} and isinstance(entry['keep'], str):
                identity = entry['keep']
                if identity in originals and identity not in handled:
                    result.sections[section].append(originals[identity][1])
                    handled.add(identity)
                continue
            if (not isinstance(entry, dict) or set(entry) != {'text', 'ref'}
                    or not valid_text(entry.get('text'), new=True) or not isinstance(entry.get('ref'), str)
                    or not REF.fullmatch(entry['ref']) or not _matches_claim(entry['text'], entry['ref'], allowed)):
                result.events.append(('dropped', 'invalid new item'))
                continue
            counter += 1
            result.sections[section].append(Item(f'i{counter}', entry['text'], entry['ref']))
    for entry in update['displaced']:
        if not isinstance(entry, dict):
            continue
        identity, reason, ref = entry.get('id'), entry.get('reason'), entry.get('ref')
        claim = entry.get('claim')
        if (isinstance(identity, str) and identity in originals and identity not in handled
                and reason in ('superseded', 'resolved') and isinstance(ref, str)
                and valid_text(claim, new=True) and _matches_claim(claim, ref, allowed)):
            section, item = originals[identity]
            result.displaced.append(Displaced(item, section, reason, ref, claim))
            handled.add(identity)
    for identity, (section, item) in originals.items():
        if identity not in handled:
            result.sections[section].append(item)
            result.events.append(('restored', identity))
    for section, items in result.sections.items():
        kept = []
        for item in items:
            if any(pattern.search(item.text) for pattern in GIT_PATTERNS):
                result.displaced.append(Displaced(item, section, 'git-live'))
            else:
                kept.append(item)
        excess = max(0, len(kept) - SECTIONS[section][1])
        oldest = sorted(range(len(kept)), key=lambda n: (_ref_date(kept[n].ref), n))[:excess]
        result.displaced += [Displaced(kept[n], section, 'limit') for n in oldest]
        result.sections[section] = [item for n, item in enumerate(kept) if n not in oldest]
    return result


def _ref_date(ref):
    match = re.search(r'\d{4}-\d{2}-\d{2}', ref)
    return match[0] if match else ''
