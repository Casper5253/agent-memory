import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentmem import status


class MatchingClaimTests(unittest.TestCase):
    def test_valid_ref_with_unrelated_text_is_dropped(self):
        result = status.reconcile(status.empty(), dict(sections={**status.empty(),
                    'working_now': [{'text': 'Uses MongoDB', 'ref': '[[new#e1]]'}]}, displaced=[]),
                    {'[[new#e1]]': 'Uses SQLite'})
        self.assertEqual(result.sections['working_now'], [])
        self.assertIn(('dropped', 'invalid new item'), result.events)

    def test_displacement_with_unrelated_or_missing_claim_restores_old(self):
        old = status.empty()
        old['active'] = [status.Item('i1', 'Migration blocked', '[[old#e1]]')]
        for claim in ('Unrelated replacement', ''):
            with self.subTest(claim=claim):
                result = status.reconcile(old, dict(sections=status.empty(), displaced=[
                    dict(id='i1', reason='resolved', ref='[[new#e1]]', claim=claim)]),
                    {'[[new#e1]]': 'Migration completed'})
                self.assertEqual(result.sections, old)
                self.assertEqual(result.displaced, [])
                self.assertIn(('restored', 'i1'), result.events)

    def test_matching_normalized_claim_supports_item_and_displacement(self):
        old = status.empty()
        old['active'] = [status.Item('i1', 'Migration blocked', '[[old#e1]]')]
        result = status.reconcile(old, dict(sections={**status.empty(), 'latest': [
                    dict(text='Verified: Migration completed', ref='[[new#e1]]')]}, displaced=[
                    dict(id='i1', reason='resolved', ref='[[new#e1]]', claim='Verified: Migration completed')]),
                    {'[[new#e1]]': 'Migration   completed'})
        self.assertEqual(result.sections['latest'][0].text, 'Verified: Migration completed')
        self.assertEqual(result.displaced[0].item.text, 'Migration blocked')
        self.assertEqual(result.displaced[0].claim, 'Verified: Migration completed')
