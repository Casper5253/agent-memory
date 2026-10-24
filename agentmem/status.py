"""Structured project status, model boundary validation, and deterministic retention."""
from dataclasses import dataclass, field
import copy
import re

from .grounding import grounded


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
SUPERSEDE_SECTIONS = {
    key: frozenset(('next', 'active', 'questions')) if key in ('next', 'active', 'questions') else frozenset((key,))
    for key in SECTIONS
}
RESOLVE_SECTIONS = {
    'questions': frozenset(('decisions', 'latest', 'working_now')),
    'next': frozenset(('latest', 'working_now')),
    'active': frozenset(('latest', 'working_now')),
}
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
class Evidence:
    text: str
    section: str
    decision: str = ''
    decision_ref: str = ''


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
    proposal: dict = field(default=None, repr=False, compare=False)


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
    offset = summary.get('_evidence_offset', 0)
    if type(offset) is not int or offset < 0:
        raise ValueError('invalid evidence offset')
    return {f'[[{target}#e{n}]]': citation['claim'] for n, citation in enumerate(summary.get('citations', []), offset + 1)
            if isinstance(citation, dict) and isinstance(citation.get('claim'), str) and citation['claim'].strip()}


def evidence_support(summary, link):
    target = link[2:-2] if link.startswith('[[') and link.endswith(']]') else link
    offset = summary.get('_evidence_offset', 0)
    if type(offset) is not int or offset < 0:
        raise ValueError('invalid evidence offset')
    citations = [(f'[[{target}#e{n}]]', citation) for n, citation in enumerate(summary.get('citations', []), offset + 1)
                 if isinstance(citation, dict) and isinstance(citation.get('claim'), str) and citation['claim'].strip()]
    decisions, reasons = {}, {}
    for decision in summary.get('decisions') or []:
        if not isinstance(decision, dict) or not isinstance(decision.get('decision'), str):
            continue
        text = decision['decision'].strip()
        ref = next((ref for ref, citation in citations if citation.get('section') in ('decision', 'decisions')
                    and _claim_key(citation['claim']) == _claim_key(text)), '')
        if not text or not ref:
            continue
        decisions[_claim_key(text)] = (text, ref)
        reason = decision.get('rationale')
        if isinstance(reason, str) and reason.strip():
            reasons.setdefault(_claim_key(reason), set()).add((text, ref))
    result = {}
    for ref, citation in citations:
        section = citation.get('section', '')
        key = _claim_key(citation['claim'])
        pair = decisions.get(key, ('', '')) if section in ('decision', 'decisions') else ('', '')
        if not pair[0] and section in ('decision', 'decisions', 'rationale') and key in reasons:
            section = 'rationale'
            matches = reasons[key]
            pair = next(iter(matches)) if len(matches) == 1 else ('', '')
        quote = citation.get('quote')
        text = f"{citation['claim']} {quote if isinstance(quote, str) else ''}".strip()
        result[ref] = Evidence(text, section, *pair)
    return result


def _claim_key(text):
    return re.sub(r'\s+', ' ', text).strip().casefold().rstrip('.!?')


def _decision_entry(entry, allowed):
    if not isinstance(entry, dict) or set(entry) != {'text', 'ref'}:
        return entry
    if not valid_text(entry['text'], new=True):
        return entry
    evidence = allowed.get(entry['ref']) if isinstance(allowed, dict) and isinstance(entry['ref'], str) else None
    if not isinstance(evidence, Evidence):
        return entry
    if evidence.section and evidence.section not in ('decision', 'decisions', 'rationale'):
        return None
    if evidence.decision and (evidence.section == 'rationale'
                              or _claim_key(evidence.decision) not in _claim_key(entry['text'])):
        return dict(text=evidence.decision, ref=evidence.decision_ref)
    return None if evidence.section == 'rationale' else entry


def _matches_claim(text, ref, allowed):
    evidence = allowed.get(ref) if isinstance(allowed, dict) else None
    if isinstance(evidence, Evidence):
        evidence = evidence.text
    return isinstance(evidence, str) and bool(evidence.strip()) and grounded(text, evidence, evidence)


def _entry_problem(entry, allowed):
    if not isinstance(entry, dict) or set(entry) != {'text', 'ref'}:
        return 'invalid item shape or incompatible decision evidence'
    if not valid_text(entry.get('text'), new=True):
        return 'invalid text: must be nonempty, single-line, at most 280 characters, without HTML markers'
    ref = entry.get('ref')
    if not isinstance(ref, str) or not REF.fullmatch(ref):
        return 'invalid citation syntax'
    if ref not in allowed:
        return 'citation not in supplied evidence'
    if not _matches_claim(entry['text'], ref, allowed):
        return 'claim unsupported by citation: absent specifics or insufficient shared content'
    return ''


def reconcile(old, update, allowed, *, source_orders=None, new_order=(0, 0)):
    if (not isinstance(update, dict) or not isinstance(update.get('sections'), dict)
            or set(update['sections']) != set(SECTIONS)
            or any(not isinstance(items, list) for items in update['sections'].values())
            or not isinstance(update.get('displaced'), list)):
        raise ValueError('invalid StatusUpdate JSON')
    originals = {i.id: (section, i) for section, items in old.items() for i in items}
    source_orders = source_orders or {}
    result, handled = Result(empty(), proposal=copy.deepcopy(update)), set()
    counter = len(originals)
    for section, items in update['sections'].items():
        for index, entry in enumerate(items):
            if isinstance(entry, dict) and set(entry) == {'keep'} and isinstance(entry['keep'], str):
                identity = entry['keep']
                if identity in originals and identity not in handled:
                    original_section, item = originals[identity]
                    destination = original_section if source_orders.get(item.ref, (0, 0)) > new_order else section
                    result.sections[destination].append(item)
                    handled.add(identity)
                continue
            if section == 'decisions':
                entry = _decision_entry(entry, allowed)
            problem = _entry_problem(entry, allowed)
            if problem:
                result.events.append(('dropped', 'invalid new item'))
                result.events.append(('rejected', f'{section}[{index}]: {problem}'))
                continue
            if section == 'decisions' and any(i.text == entry['text'] and i.ref == entry['ref'] for i in result.sections[section]):
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
            if source_orders.get(item.ref, (0, 0)) > new_order:
                result.events.append(('refused', f'newer source evidence: {identity}'))
                continue
            compatible_sections = SUPERSEDE_SECTIONS[section] if reason == 'superseded' else RESOLVE_SECTIONS.get(section, ())
            if not any(
                    replacement.ref == ref and replacement.id not in originals
                    and not any(pattern.search(replacement.text) for pattern in GIT_PATTERNS)
                    for compatible in compatible_sections for replacement in result.sections[compatible]):
                result.events.append(('refused', f'incompatible {reason}: {identity}'))
                continue
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
        oldest = sorted(range(len(kept)), key=lambda n: (
            source_orders.get(kept[n].ref, new_order), _ref_date(kept[n].ref), n))[:excess]
        result.displaced += [Displaced(kept[n], section, 'limit') for n in oldest]
        result.sections[section] = [item for n, item in enumerate(kept) if n not in oldest]
    return result


def proposed_update(result, old):
    """Revalidate typed model results against the current page and source orders."""
    if result.proposal is not None:
        return result.proposal
    originals = {item.id: item for items in old.values() for item in items}
    return dict(sections={section: [dict(keep=item.id) if originals.get(item.id) == item
                                   else dict(text=item.text, ref=item.ref) for item in items]
                          for section, items in result.sections.items()},
                displaced=[dict(id=d.item.id, reason=d.reason, ref=d.evidence, claim=d.claim)
                           for d in result.displaced if d.reason in ('superseded', 'resolved')])


def _ref_date(ref):
    match = re.search(r'\d{4}-\d{2}-\d{2}', ref)
    return match[0] if match else ''
