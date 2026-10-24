import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentmem import status


class StatusTests(unittest.TestCase):
    def old(self, text='Uses SQLite', ref='[[2026-01-01-old#e1]]'):
        page = status.empty()
        page['working_now'] = [status.Item('i1', text, ref)]
        return page

    def update(self, **sections):
        return {'sections': {**status.empty(), **sections}, 'displaced': []}

    def test_round_trip(self):
        page = self.old()
        self.assertEqual(status.parse(status.render(page)), page)

    def test_dropped_old_item_restored(self):
        result = status.reconcile(self.old(), self.update(), {'[[new#e1]]'})
        self.assertEqual(result.sections, self.old())
        self.assertEqual(result.events, [('restored', 'i1')])

    def test_rephrase_requires_new_evidence_and_preserves_original(self):
        result = status.reconcile(self.old(), self.update(working_now=[{'text': 'Uses JSON', 'ref': '[[2026-01-01-old#e1]]'}]), {'[[new#e1]]'})
        self.assertEqual(result.sections, self.old())
        self.assertIn(('dropped', 'invalid new item'), result.events)

    def test_uncited_displacement_restored(self):
        update = self.update()
        update['displaced'] = [{'id': 'i1', 'reason': 'resolved'}]
        self.assertEqual(status.reconcile(self.old(), update, set()).sections, self.old())

    def test_cited_displacement(self):
        update = self.update()
        update['displaced'] = [{'id': 'i1', 'reason': 'superseded', 'ref': '[[new#e1]]', 'claim': 'Uses JSON'}]
        result = status.reconcile(self.old(), update, {'[[new#e1]]': 'Uses JSON'})
        self.assertEqual(result.displaced[0].item.text, 'Uses SQLite')
        self.assertFalse(result.sections['working_now'])

    def test_limit_oldest(self):
        page = status.empty()
        page['purpose'] = [status.Item(f'i{n}', str(n), f'[[2026-01-0{n}-s#e1]]') for n in range(1, 5)]
        result = status.reconcile(page, self.update(purpose=[{'keep': f'i{n}'} for n in range(1, 5)]), set())
        self.assertEqual([d.item.id for d in result.displaced], ['i1'])
        self.assertEqual(result.displaced[0].reason, 'limit')

    def test_git_variants_and_historical_commit(self):
        for text in ['currently on branch feature/x at 1a2b3c4', 'working tree is clean', 'branch: x', 'HEAD is main', 'migration complete; uncommitted changes', 'ahead of origin/main', 'behind main']:
            with self.subTest(text=text):
                result = status.reconcile(self.old(text), self.update(working_now=[{'keep': 'i1'}]), set())
                self.assertEqual(result.displaced[0].reason, 'git-live')
                self.assertEqual(result.displaced[0].item.text, text)
        self.assertFalse(status.reconcile(self.old('merged in 1a2b3c4'), self.update(), set()).displaced)

    def test_invalid_new_items_dropped(self):
        for text, ref in [('x', ''), ('x', '[[old#e1]]'), ('x' * 281, '[[new#e1]]'), ('x\ny', '[[new#e1]]'), ('<!-- agentmem:state:begin -->', '[[new#e1]]')]:
            result = status.reconcile(status.empty(), self.update(active=[{'text': text, 'ref': ref}]), {'[[new#e1]]': text})
            self.assertFalse(result.sections['active'])

    def test_old_long_item_kept(self):
        self.assertEqual(status.reconcile(self.old('x' * 400), self.update(), set()).sections, self.old('x' * 400))

    def test_malformed_and_legacy_rejected(self):
        for text in ['### Current state\n- old', status.render(self.old()) + '\nunexpected']:
            with self.assertRaises(ValueError):
                status.parse(text)
