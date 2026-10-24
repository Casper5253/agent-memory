import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentmem import notes, pipeline, remember, status
import fixtures as fx


class OverlappingSessionsTests(unittest.TestCase):
    def test_source_order_guards_resolution_and_moving_keeps(self):
        old = status.empty()
        old['next'] = [status.Item('i1', 'Implement coverage', '[[B#e1]]')]
        update = dict(sections={**status.empty(), 'latest': [dict(text='Coverage completed', ref='[[A#e1]]')]},
                      displaced=[dict(id='i1', reason='resolved', ref='[[A#e1]]', claim='Coverage completed')])
        for incoming, removed in (((10, 1), False), ((20, 2), True), ((30, 1), True)):
            with self.subTest(incoming=incoming):
                result = status.reconcile(old, update, {'[[A#e1]]': 'Coverage completed'},
                                          source_orders={'[[B#e1]]': (20, 1)}, new_order=incoming)
                self.assertEqual(bool(result.sections['next']), not removed)
                self.assertEqual(bool(result.displaced), removed)
        move = dict(sections={**status.empty(), 'latest': [dict(keep='i1')]}, displaced=[])
        result = status.reconcile(old, move, {}, source_orders={'[[B#e1]]': (20, 1)}, new_order=(10, 1))
        self.assertEqual(result.sections, old)

    def test_retention_does_not_evict_newer_evidence(self):
        old = status.empty()
        old['purpose'] = [status.Item(f'i{n}', f'Newer direction {n}', f'[[B#e{n}]]') for n in range(1, 4)]
        update = dict(sections={**status.empty(), 'purpose': [dict(text='Older direction', ref='[[A#e1]]')]}, displaced=[])
        result = status.reconcile(old, update, {'[[A#e1]]': 'Older direction'},
                                  source_orders={i.ref: (20, 1) for i in old['purpose']}, new_order=(10, 1))
        self.assertEqual(result.sections, old)
        self.assertEqual([(d.item.text, d.reason) for d in result.displaced], [('Older direction', 'limit')])

    def test_legacy_page_uses_conservative_watermark_and_prunes_metadata(self):
        old = status.empty()
        old['decisions'] = [status.Item('i1', 'Use Postgres', '[[B#e1]]')]
        text = notes.touch_state_order(notes.new_project_note('Demo App', '2026-10-24'), (20, 1))
        self.assertEqual(notes.evidence_orders(text, old), {'[[B#e1]]': (20, 1)})
        text = notes.touch_evidence_orders(text, {'[[B#e1]]': (15, 2), '[[removed#e1]]': (5, 1)})
        text = notes.touch_evidence_orders(text, notes.evidence_orders(text, old))
        self.assertEqual(notes.evidence_orders(text, old), {'[[B#e1]]': (15, 2)})
        self.assertEqual(len(notes.EVIDENCE_ORDER.findall(text)), 1)

    def test_raw_json_can_reconcile_parked_older_capture(self):
        with fx.tmpdir() as tmp:
            cfg = fx.make_vault(Path(tmp))
            path = cfg.projects_dir / 'Demo App' / 'Demo App.md'
            start = datetime(2026, 10, 24, 14, 3, tzinfo=timezone.utc)
            old = status.empty()
            old['decisions'] = [status.Item('i1', 'Use Postgres', '[[B#e1]]')]
            notes.update_project_note(path, 'Demo App', '2026-10-24', state=status.render(old),
                                      state_order_value=(int((start + timedelta(minutes=1)).timestamp() * 1_000_000), 1),
                                      pending_add=['- [ ] [[A]] status update pending (order=1:1)'])
            summary = remember._summary('Use SQLite', 'decision', '')
            def model(*args, **kwargs):
                return dict(sections={**status.empty(), 'decisions': [dict(text='Use SQLite', ref='[[A#e1]]')]},
                            displaced=[dict(id='i1', reason='superseded', ref='[[A#e1]]', claim='Use SQLite')]), 0
            self.assertTrue(pipeline.consolidate_into_project(cfg, 'Demo App', summary, 'A', start, 'remember', model, retry=True)[1])
            text = path.read_text(encoding='utf-8')
            self.assertNotIn('status update pending', text)
            self.assertIn('Use Postgres', text)
            self.assertIn('Use SQLite', text)

    def test_older_session_finishes_after_newer_update(self):
        with fx.tmpdir() as tmp:
            cfg = fx.make_vault(Path(tmp))
            path = cfg.projects_dir / 'Demo App' / 'Demo App.md'
            start = datetime(2026, 10, 24, 14, 3, tzinfo=timezone.utc)
            newer = start + timedelta(minutes=1)

            def commit(text, link, when, replace=False):
                summary = remember._summary(text, 'decision', '')
                def model(state, summary, wrapped_link, date, timeout):
                    old = status.live_page(state)
                    update = dict(sections={**status.empty(), 'decisions': [dict(text=text, ref=f'[[{link}#e1]]')]},
                                  displaced=[dict(id=i.id, reason='superseded', ref=f'[[{link}#e1]]', claim=text)
                                             for i in old['decisions']] if replace else [])
                    # Production llm.consolidate returns Result, not raw JSON.
                    return status.reconcile(old, update, status.evidence_support(summary, link)), 0
                return pipeline.consolidate_into_project(cfg, 'Demo App', summary, link, when, 'remember', model)

            self.assertTrue(commit('Use Postgres for storage', 'B', newer)[1])
            self.assertTrue(commit('Use SQLite for storage', 'A', start, replace=True)[1])
            text = path.read_text(encoding='utf-8')
            state = status.parse(notes.get_block(text, notes.STATE_BEGIN, notes.STATE_END))
            self.assertEqual({i.text for i in state['decisions']}, {'Use Postgres for storage', 'Use SQLite for storage'})
            self.assertEqual(notes.state_order(text), (int(newer.timestamp() * 1_000_000), 1))
            self.assertNotIn('status update pending', text)
            # Retry is idempotent, and an intermediate source may retire A but not B.
            self.assertTrue(commit('Use SQLite for storage', 'A', start, replace=True)[1])
            self.assertTrue(commit('Use JSON for storage', 'C', start + timedelta(seconds=30), replace=True)[1])
            text = path.read_text(encoding='utf-8')
            state = status.parse(notes.get_block(text, notes.STATE_BEGIN, notes.STATE_END))
            self.assertEqual({i.text for i in state['decisions']}, {'Use Postgres for storage', 'Use JSON for storage'})
            history = path.with_name('History.md').read_text(encoding='utf-8')
            self.assertIn('Use SQLite for storage', history)
            self.assertNotIn('Use Postgres for storage', history)


if __name__ == '__main__':
    unittest.main()
