import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentmem import llm, notes, pipeline, status
from test_p3_startup import StartupBase


class PipelineStatusTests(StartupBase):
    def update(self, consolidator):
        return pipeline.consolidate_into_project(self.cfg, 'Alpha', {'citations': [{'claim': 'new', 'quote': 'new'}]},
                                                 'new', datetime(2026, 10, 24, tzinfo=timezone.utc),
                                                 'claude-haiku', consolidator)

    def test_empty_initializes_structured_status(self):
        converter = Mock(return_value=({'sections': {**status.empty(), 'active': [{'text': 'new', 'ref': '[[new#e1]]'}]}, 'displaced': []}, 0.01))
        self.assertEqual(self.update(converter), (0.01, True))
        path = self.cfg.projects_dir / 'Alpha' / 'Alpha.md'
        page = status.parse(notes.get_block(path.read_text(encoding='utf-8'), notes.STATE_BEGIN, notes.STATE_END))
        self.assertEqual(page['active'][0].text, 'new')

    def test_legacy_or_malformed_pending_without_model_or_replacement(self):
        for state in ['### Current state\n- legacy', '<!-- agentmem:status:v2 -->\nmalformed']:
            self.write_state(state)
            converter = Mock()
            self.assertEqual(self.update(converter), (0.0, False))
            converter.assert_not_called()
            text = (self.cfg.projects_dir / 'Alpha' / 'Alpha.md').read_text(encoding='utf-8')
            self.assertEqual(notes.get_block(text, notes.STATE_BEGIN, notes.STATE_END), state)
            self.assertIn('page not migrated', notes.get_block(text, notes.PENDING_BEGIN, notes.PENDING_END))

    def test_old_drop_restores_but_no_new_item_stays_pending(self):
        page = status.empty()
        page['purpose'] = [status.Item('i1', 'old exact claim', '[[old#e1]]')]
        self.write_state(status.render(page))
        converter = Mock(return_value=({'sections': status.empty(), 'displaced': []}, 0))
        with patch.object(pipeline, 'log') as log:
            self.assertEqual(self.update(converter), (0.0, False))
        self.assertTrue(any(call.kwargs.get('action') == 'restored' for call in log.call_args_list))
        text = (self.cfg.projects_dir / 'Alpha' / 'Alpha.md').read_text(encoding='utf-8')
        self.assertIn('old exact claim', text)

    def test_uncited_summary_stays_pending_and_never_calls_model(self):
        converter = Mock()
        summary = {'decisions': [{'decision': 'Switch to Postgres', 'rationale': 'Concurrent writers'}]}
        outcome = pipeline.consolidate_into_project(self.cfg, 'Alpha', summary, 'uncited',
                       datetime(2026, 10, 24, tzinfo=timezone.utc), 'persisted', converter)
        self.assertEqual(outcome, (0, False))
        converter.assert_not_called()
        text = (self.cfg.projects_dir / 'Alpha' / 'Alpha.md').read_text(encoding='utf-8')
        pending = notes.get_block(text, notes.PENDING_BEGIN, notes.PENDING_END)
        self.assertIn('uncited summary, status not updated', pending)
        self.assertIn('Postgres', pending)
