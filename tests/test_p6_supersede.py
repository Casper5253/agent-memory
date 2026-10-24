import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentmem import llm, notes, remember, status


DECISION = 'Next phase (P7): integrate the status page and the LLMWiki knowledge store into one memory product.'
REASON = 'User wants one product.'
NEXT = 'P7 design first: define how the status page and knowledge store fit together.'
DECISION_PAGE = '''<!-- agentmem:status:v2 -->

### Purpose and direction

### What works now

### Latest progress

### Active work and blockers

### Open questions

### Key decisions (decision, reason)
- Next phase (P7): integrate the status page and the LLMWiki knowledge store into one memory product. [[decision#e1]]

### Next steps

History: [[History]]'''
DECISION_AND_NEXT_PAGE = '''<!-- agentmem:status:v2 -->

### Purpose and direction

### What works now

### Latest progress

### Active work and blockers

### Open questions

### Key decisions (decision, reason)
- Next phase (P7): integrate the status page and the LLMWiki knowledge store into one memory product. [[decision#e1]]

### Next steps
- P7 design first: define how the status page and knowledge store fit together. [[next#e1]]

History: [[History]]'''


class SupersedeScopeTests(unittest.TestCase):
    def test_remember_decision_cannot_render_its_reason_alone(self):
        summary = remember._summary(DECISION, 'decision', REASON)
        output = dict(sections={**status.empty(), 'decisions': [dict(text=REASON, ref='[[decision#e2]]')]}, displaced=[])
        with patch.object(llm, '_run', return_value=output):
            result, _ = llm.consolidate(notes.EMPTY_STATE, summary, '[[decision]]', '2026-10-24')
        self.assertEqual(status.render(result.sections), DECISION_PAGE)

    def test_remember_next_step_cannot_supersede_product_decision(self):
        summary = remember._summary(NEXT, 'next', '')
        output = dict(sections={**status.empty(), 'next': [dict(text=NEXT, ref='[[next#e1]]')]},
                      displaced=[dict(id='i1', reason='superseded', ref='[[next#e1]]', claim=NEXT)])
        with patch.object(llm, '_run', return_value=output):
            result, _ = llm.consolidate(DECISION_PAGE, summary, '[[next]]', '2026-10-24')
        self.assertEqual(status.render(result.sections), DECISION_AND_NEXT_PAGE)
        self.assertEqual(result.displaced, [])

    def test_supersede_section_compatibility_matrix(self):
        work = {'next', 'active', 'questions'}
        for source in status.SECTIONS:
            for destination in status.SECTIONS:
                with self.subTest(source=source, destination=destination):
                    old = status.empty()
                    old[source] = [status.Item('i1', 'Original claim', '[[old#e1]]')]
                    update = dict(sections={**status.empty(), destination: [dict(text='Replacement claim', ref='[[new#e1]]')]},
                                  displaced=[dict(id='i1', reason='superseded', ref='[[new#e1]]', claim='Replacement claim')])
                    result = status.reconcile(old, update, {'[[new#e1]]': 'Replacement claim'})
                    compatible = source == destination or source in work and destination in work
                    self.assertEqual(any(i.id == 'i1' for items in result.sections.values() for i in items), not compatible)
                    self.assertEqual(len(result.displaced), int(compatible))

    def test_supersede_requires_new_retained_replacement(self):
        old = status.empty()
        old['decisions'] = [status.Item('i1', 'Original decision', '[[old#e1]]')]
        for proposed in ([], [dict(keep='i1')], [dict(text='Unrelated claim', ref='[[new#e1]]')]):
            with self.subTest(proposed=proposed):
                update = dict(sections={**status.empty(), 'decisions': proposed},
                              displaced=[dict(id='i1', reason='superseded', ref='[[new#e1]]', claim='Replacement decision')])
                result = status.reconcile(old, update, {'[[new#e1]]': 'Replacement decision'})
                self.assertEqual(result.sections, old)
                self.assertEqual(result.displaced, [])

    def test_next_evidence_mislabeled_as_decision_cannot_retire_decision(self):
        old = status.parse(DECISION_PAGE)
        summary = remember._summary(NEXT, 'next', '')
        update = dict(sections={**status.empty(), 'decisions': [dict(text=NEXT, ref='[[next#e1]]')]},
                      displaced=[dict(id='i1', reason='superseded', ref='[[next#e1]]', claim=NEXT)])
        result = status.reconcile(old, update, status.evidence_support(summary, '[[next]]'))
        self.assertEqual(status.render(result.sections), DECISION_PAGE)
        self.assertEqual(result.displaced, [])

    def test_reason_cannot_replace_decision_even_when_citing_decision_anchor(self):
        summary = remember._summary(DECISION, 'decision', REASON)
        update = dict(sections={**status.empty(), 'decisions': [dict(text=REASON, ref='[[decision#e1]]')]}, displaced=[])
        result = status.reconcile(status.empty(), update, status.evidence_support(summary, '[[decision]]'))
        self.assertEqual(status.render(result.sections), DECISION_PAGE)

    def test_decision_and_reason_candidates_render_the_decision_once(self):
        summary = remember._summary(DECISION, 'decision', REASON)
        update = dict(sections={**status.empty(), 'decisions': [dict(text=DECISION, ref='[[decision#e1]]'),
                                                              dict(text=REASON, ref='[[decision#e2]]')]}, displaced=[])
        result = status.reconcile(status.empty(), update, status.evidence_support(summary, '[[decision]]'))
        self.assertEqual(status.render(result.sections), DECISION_PAGE)

    def test_shared_reason_does_not_guess_which_decision_it_means(self):
        summary = dict(decisions=[dict(decision='Use SQLite', rationale='Local data'),
                                  dict(decision='Use JSON', rationale='Local data')],
                       citations=[dict(section='decision', claim='Use SQLite', quote='Use SQLite'),
                                  dict(section='decision', claim='Use JSON', quote='Use JSON'),
                                  dict(section='rationale', claim='Local data', quote='Local data')])
        update = dict(sections={**status.empty(), 'decisions': [dict(text='Local data', ref='[[new#e3]]')]}, displaced=[])
        result = status.reconcile(status.empty(), update, status.evidence_support(summary, '[[new]]'))
        self.assertEqual(result.sections, status.empty())

    def test_filtered_git_item_cannot_be_a_replacement(self):
        old = status.empty()
        old['working_now'] = [status.Item('i1', 'Original feature works', '[[old#e1]]')]
        claim = 'working tree is clean'
        update = dict(sections={**status.empty(), 'working_now': [dict(text=claim, ref='[[new#e1]]')]},
                      displaced=[dict(id='i1', reason='superseded', ref='[[new#e1]]', claim=claim)])
        result = status.reconcile(old, update, {'[[new#e1]]': claim})
        self.assertEqual(result.sections, old)
        self.assertEqual([d.reason for d in result.displaced], ['git-live'])

    def test_resolved_item_does_not_require_a_replacement(self):
        old = status.empty()
        old['next'] = [status.Item('i1', 'Implement coverage', '[[old#e1]]')]
        update = dict(sections=status.empty(), displaced=[dict(id='i1', reason='resolved', ref='[[new#e1]]', claim='Coverage completed')])
        result = status.reconcile(old, update, {'[[new#e1]]': 'Coverage completed'})
        self.assertEqual(result.sections, status.empty())
        self.assertEqual(result.displaced[0].reason, 'resolved')


if __name__ == '__main__':
    unittest.main()
