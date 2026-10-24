import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentmem import llm, notes, status


class LLMTests(unittest.TestCase):
    def test_structured_single_call_no_shorten_or_repo_observations(self):
        summary = {'citations': [{'claim': 'works', 'quote': 'works'}]}
        before = copy.deepcopy(summary)
        data = {'sections': {**status.empty(), 'latest': [{'text': 'Works', 'ref': '[[s#e1]]'}]}, 'displaced': []}
        with patch.object(llm, '_run', return_value=data) as run:
            result, _ = llm.consolidate(notes.EMPTY_STATE, summary, '[[s]]', '2026-10-24')
        self.assertEqual(result.sections['latest'][0].text, 'Works')
        self.assertEqual(run.call_count, 1)
        self.assertEqual(summary, before)
        self.assertNotIn('repo_head', llm.CONSOLIDATE_SYSTEM)

    def test_unmigrated_page_never_calls_model(self):
        with patch.object(llm, '_run') as run:
            with self.assertRaisesRegex(llm.LLMError, 'page not migrated'):
                llm.consolidate('### Current state\n- Legacy', {}, '[[s]]', '2026-10-24')
        run.assert_not_called()

    def test_invalid_json_shape_fails(self):
        with patch.object(llm, '_run', return_value={'state_markdown': 'legacy'}):
            with self.assertRaises(llm.LLMError):
                llm.consolidate(notes.EMPTY_STATE, {}, '[[s]]', '2026-10-24')
