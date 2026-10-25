import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentmem import llm, notes, pipeline, remember, status
from test_knowledge_capture_recovery import CaptureBase


EXPECTED_D1 = '''<!-- agentmem:status:v2 -->

### Purpose and direction

### What works now

### Latest progress
- Use SQLite for local storage. [[demo#e1]]

### Active work and blockers

### Open questions

### Key decisions (decision, reason)
- Use SQLite for local storage. [[demo#e1]]

### Next steps

History: [[1-Projects/Demo App/History]]'''

EXPECTED_D2 = '''<!-- agentmem:status:v2 -->

### Purpose and direction

### What works now

### Latest progress

### Active work and blockers

### Open questions

### Key decisions (decision, reason)
- Use Postgres for concurrent storage. [[demo#e1]]
- Use SQLite for local storage. [[old#e1]]

### Next steps

History: [[1-Projects/Demo App/History]]'''


class DecisionGuaranteeTests(CaptureBase):
    def run_update(self, summary, output):
        with patch.object(llm, '_run', return_value=output):
            result = pipeline.consolidate_into_project(self.cfg, 'Demo App', summary, 'demo',
                datetime(2026, 10, 25, tzinfo=timezone.utc), 'claude-haiku')
        path = self.cfg.projects_dir / 'Demo App' / 'Demo App.md'
        text = path.read_text(encoding='utf-8')
        events = [json.loads(line) for line in self.cfg.log_path.read_text(encoding='utf-8').splitlines()] if self.cfg.log_path.exists() else []
        return result, notes.get_block(text, notes.STATE_BEGIN, notes.STATE_END), events, text

    def test_latest_only_model_cannot_omit_cited_decision(self):
        summary = remember._summary('Use SQLite for local storage.', 'decision', '')
        output = dict(sections={**status.empty(), 'latest': [dict(text=summary['decisions'][0]['decision'], ref='[[demo#e1]]')]}, displaced=[])
        result, page, events, _ = self.run_update(summary, output)
        self.assertTrue(result[1])
        self.assertEqual(page, EXPECTED_D1)
        self.assertTrue(any(e.get('action') == 'added-omitted-decision' for e in events))

    def test_valid_decision_applies_despite_invalid_question_and_restores_old(self):
        old = status.empty()
        old['decisions'] = [status.Item('i1', 'Use SQLite for local storage.', '[[old#e1]]')]
        notes.update_project_note(self.cfg.projects_dir / 'Demo App' / 'Demo App.md', 'Demo App', '2026-10-24', state=status.render(old))
        summary = remember._summary('Use Postgres for concurrent storage.', 'decision', '')
        output = dict(sections={**status.empty(),
            'decisions': [dict(text='Use Postgres for concurrent storage.', ref='[[demo#e1]]')],
            'questions': [dict(text='Does absent.py support concurrent storage?', ref='[[demo#e1]]')]}, displaced=[])
        result, page, events, text = self.run_update(summary, output)
        self.assertTrue(result[1])
        self.assertEqual(page, EXPECTED_D2)
        self.assertTrue(any(e.get('action') == 'rejected' for e in events))
        self.assertTrue(any(e.get('action') == 'restored' for e in events))
        self.assertNotIn('status update pending', text)

    def test_guaranteed_decision_can_supersede_old_decision(self):
        old = status.empty()
        old['decisions'] = [status.Item('i1', 'Use SQLite for local storage.', '[[old#e1]]')]
        notes.update_project_note(self.cfg.projects_dir / 'Demo App' / 'Demo App.md', 'Demo App', '2026-10-24', state=status.render(old))
        summary = remember._summary('Use Postgres for concurrent storage.', 'decision', '')
        output = dict(sections=status.empty(), displaced=[dict(id='i1', reason='superseded',
                      ref='[[demo#e1]]', claim='Use Postgres for concurrent storage.')])
        result, page, _, _ = self.run_update(summary, output)
        self.assertTrue(result[1])
        self.assertEqual(page, EXPECTED_D2.replace('- Use SQLite for local storage. [[old#e1]]\n', ''))
        history = (self.cfg.projects_dir / 'Demo App' / 'History.md').read_text(encoding='utf-8')
        self.assertIn('[decisions; superseded]', history)
        self.assertIn('Use SQLite for local storage.', history)

    def test_guarantee_deduplicates_normalized_claims_and_requires_decision_citation(self):
        summary = remember._summary('Use SQLite for local storage.', 'decision', '')
        allowed = status.evidence_support(summary, 'demo')
        old = status.empty()
        old['decisions'] = [status.Item('i1', 'USE SQLITE FOR LOCAL STORAGE!', '[[old#e1]]')]
        original = dict(sections=status.empty(), displaced=[])
        for page, proposal in ((old, original), (status.empty(), dict(sections={**status.empty(), 'decisions':
            [dict(text='Use SQLite for local storage.', ref='[[demo#e1]]')]}, displaced=[]))):
            complete, events = status.include_summary_decisions(page, proposal, summary, allowed)
            self.assertEqual(complete, proposal)
            self.assertEqual(events, [])
        uncited = {**summary, 'citations': [dict(section='facts', claim='Use SQLite for local storage.', quote='Use SQLite for local storage.')]}
        complete, events = status.include_summary_decisions(status.empty(), original, uncited, status.evidence_support(uncited, 'demo'))
        self.assertEqual(complete, original)
        self.assertEqual(events, [])
        self.assertEqual(original, dict(sections=status.empty(), displaced=[]))
        same_ref = status.empty()
        same_ref['decisions'] = [status.Item('i1', 'Existing supported wording', '[[demo#e1]]')]
        self.assertEqual(status.include_summary_decisions(same_ref, original, summary, allowed), (original, []))

    def test_guarantee_does_not_bypass_chronology_or_section_caps(self):
        old = status.empty()
        old['decisions'] = [status.Item(f'i{n}', f'Newer storage decision {n}', f'[[old#e{n}]]') for n in range(1, 9)]
        summary = remember._summary('Use SQLite for local storage.', 'decision', '')
        allowed = status.evidence_support(summary, 'demo')
        update = dict(sections=status.empty(), displaced=[dict(id='i1', reason='superseded',
            ref='[[demo#e1]]', claim='Use SQLite for local storage.')])
        proposal, events = status.include_summary_decisions(old, update, summary, allowed)
        result = status.reconcile(old, proposal, allowed,
            source_orders={i.ref: (20, 1) for i in old['decisions']}, new_order=(10, 1))
        self.assertEqual(result.sections, old)
        self.assertEqual(len(result.sections['decisions']), 8)
        self.assertEqual([(d.item.text, d.reason) for d in result.displaced], [('Use SQLite for local storage.', 'limit')])
        self.assertEqual(events, [('added-omitted-decision', '[[demo#e1]]')])
