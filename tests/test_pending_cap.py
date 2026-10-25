import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentmem import hook, llm, notes, pipeline, remember, search, status
from test_knowledge_capture_recovery import CaptureBase
from fixtures import fake_consolidate
from tools import cap_pending


class PendingCapTests(CaptureBase):
    def setUp(self):
        (Path(__file__).resolve().parents[1] / '.audit').mkdir(exist_ok=True)
        super().setUp()

    def page(self):
        return self.cfg.projects_dir / 'Demo App' / 'Demo App.md'

    def seed(self, lines):
        path = self.page()
        path.parent.mkdir(parents=True, exist_ok=True)
        text = notes.set_block(notes.new_project_note('Demo App', '2026-10-25'),
                               notes.PENDING_BEGIN, notes.PENDING_END, '\n'.join(lines))
        path.write_text(text, encoding='utf-8')
        return path

    def lines(self, count=200):
        return [f'- [ ] 2026-10-25 [[session-{i}]] Pending item {i}.' for i in range(count)]

    def test_200_lines_cap_preserves_verbatim_overflow_and_total(self):
        lines = self.lines()
        path = self.seed(lines)
        notes.update_project_note(path, 'Demo App', '2026-10-25', touch=False)
        text = path.read_text(encoding='utf-8')
        pending = notes.get_block(text, notes.PENDING_BEGIN, notes.PENDING_END)
        self.assertEqual(10, sum(line.startswith('- [ ]') for line in pending.splitlines()))
        self.assertIn('190 more pending items', pending)
        history = notes.project_history_path(path).read_text(encoding='utf-8')
        for line in lines[:190]:
            self.assertIn('\n' + line + '\n', history)
        self.assertIn('pending-overflow', history)
        self.assertEqual(lines, notes.pending_lines(path, text))
        before = history
        notes.update_project_note(path, 'Demo App', '2026-10-25', touch=False)
        self.assertEqual(before, notes.project_history_path(path).read_text(encoding='utf-8'))

    def test_overflow_marker_survives_failure_then_success_clears_active_only(self):
        line = '- [ ] 2026-10-25 [[old]] capture-ref:abc status update pending (base=' + 'a' * 64 + '; order=12:2)'
        path = self.seed([line] + self.lines())
        notes.update_project_note(path, 'Demo App', '2026-10-25', touch=False)
        complete = '\n'.join(notes.pending_lines(path))
        self.assertEqual(('a' * 64, (12, 2)), pipeline._pending_marker(complete, 'old', 'abc'))
        summary = remember._summary('Use SQLite for local storage.', 'decision', '')
        model = Mock(side_effect=llm.LLMError('synthetic model unavailable'))
        when = datetime(2026, 10, 25, tzinfo=timezone.utc)
        self.assertFalse(pipeline.consolidate_into_project(self.cfg, 'Demo App', summary, 'old', when,
            'remember', model, capture_id='abc', retry=True)[1])
        complete = '\n'.join(notes.pending_lines(path))
        self.assertEqual(('a' * 64, (12, 2)), pipeline._pending_marker(complete, 'old', 'abc'))
        model.side_effect = None
        model.return_value = (dict(sections={**status.empty(), 'decisions': [dict(text='Use SQLite for local storage.', ref='[[old#e1]]')]}, displaced=[]), 0)
        self.assertTrue(pipeline.consolidate_into_project(self.cfg, 'Demo App', summary, 'old', when,
            'remember', model, capture_id='abc', retry=True)[1])
        self.assertFalse(any('capture-ref:abc' in item for item in notes.pending_lines(path)))
        self.assertIn(line, notes.project_history_path(path).read_text(encoding='utf-8'))

    def test_real_retry_and_held_capture_keep_manifest_semantics(self):
        sid, _ = self.source()
        self.finish(sid)
        row = self.man.capture('claude', sid, 1)
        self.man.set_consolidated('claude', sid, 1, 0)
        capture = pipeline._capture_identity('claude', sid, 1)
        link = row['note_path'].removesuffix('.md')
        path = self.seed([f'- [ ] [[{link}]] capture-ref:{capture} status update pending (order=1:1)'] + self.lines())
        notes.update_project_note(path, 'Demo App', '2026-10-25', touch=False)
        self.man.hold_retry('claude', sid, 1, 'synthetic held')
        model = Mock(side_effect=AssertionError('held capture must not call model'))
        self.assertEqual({'consolidated': 0, 'still_pending': 0}, pipeline.consolidate_pending(self.cfg, self.man, consolidator=model))
        self.assertTrue(any(capture in line for line in notes.pending_lines(path)))
        self.man.db.execute('UPDATE captures SET retry_hold=NULL WHERE session_id=?', (sid,))
        result = pipeline.consolidate_pending(self.cfg, self.man, consolidator=fake_consolidate)
        self.assertEqual({'consolidated': 1, 'still_pending': 0}, result)
        self.assertFalse(any(capture in line for line in notes.pending_lines(path)))

    def test_overflow_journal_recovers_without_duplicate_or_lost_lines(self):
        lines = self.lines()
        path = self.seed(lines)
        original_write = notes.atomic_write
        def fail_page(target, text):
            if target == path:
                raise OSError('synthetic page failure')
            return original_write(target, text)
        with patch.object(notes, 'atomic_write', side_effect=fail_page):
            with self.assertRaisesRegex(OSError, 'synthetic page failure'):
                notes.update_project_note(path, 'Demo App', '2026-10-25', touch=False)
        self.assertTrue(notes._status_journal(path).exists())
        history_before = notes.project_history_path(path).read_bytes()
        notes.recover_status_update(path)
        self.assertEqual(history_before, notes.project_history_path(path).read_bytes())
        self.assertEqual(lines, notes.pending_lines(path))
        self.assertFalse(notes._status_journal(path).exists())

    def test_one_off_plan_apply_and_repeat_preserve_stamp_and_state(self):
        path = self.seed(self.lines())
        before = path.read_bytes()
        plan = cap_pending.build_plan(self.cfg.vault, '2026-10-25')
        self.assertEqual(before, path.read_bytes())
        self.assertFalse(notes.project_history_path(path).exists())
        self.assertEqual(200, plan['pages'][0]['pending_items'])
        cap_pending.apply_plan(plan)
        after = path.read_bytes()
        self.assertEqual(len(after), plan['pages'][0]['after_bytes'])
        self.assertIn('updated: 2026-10-25', after.decode('utf-8'))
        self.assertEqual(notes.get_block(before.decode('utf-8').replace('\r\n', '\n'), notes.STATE_BEGIN, notes.STATE_END),
                         notes.get_block(after.decode('utf-8'), notes.STATE_BEGIN, notes.STATE_END))
        plan = cap_pending.build_plan(self.cfg.vault, '2026-10-25')
        self.assertFalse(plan['pages'][0]['changed'])
        cap_pending.apply_plan(plan)
        self.assertEqual(after, path.read_bytes())

    def test_health_reads_overflow_and_search_keeps_pending_out_of_current(self):
        line = '- [ ] [[parked]] status update pending (synthetic overflow reason; order=1:1)'
        path = self.seed([line] + self.lines())
        notes.update_project_note(path, 'Demo App', '2026-10-25', touch=False)
        self.assertNotIn('synthetic overflow reason', notes.get_block(path.read_text(encoding='utf-8'), notes.PENDING_BEGIN, notes.PENDING_END))
        startup = hook.startup_package(self.cfg, self.cwd)
        self.assertIn('synthetic overflow reason', startup)
        history = notes.project_history_path(path)
        self.assertEqual('HISTORICAL', search._located_label(str(history), 'CURRENT', 'synthetic overflow reason'))
        self.assertEqual('PENDING', search._located_label(str(path), 'CURRENT', 'Pending item 199.'))

    def test_allowed_email_in_overflow_survives_journal_and_lookup(self):
        line = '- [ ] [[parked]] Contact owner@example.org for the review.'
        path = self.seed([line] + self.lines())
        notes.update_project_note(path, 'Demo App', '2026-10-25', touch=False,
                                  email_allowlist=['owner@example.org'])
        self.assertIn(line, notes.pending_lines(path))

    def test_one_off_moves_existing_text_verbatim_without_reprocessing_page(self):
        line = '- [ ] [[parked]] Contact owner@example.org for the review.'
        path = self.seed([line] + self.lines())
        text = path.read_text(encoding='utf-8') + '\nOwner: owner@example.org\n'
        path.write_text(text, encoding='utf-8', newline='\n')
        plan = cap_pending.build_plan(self.cfg.vault, '2026-10-25')
        cap_pending.apply_plan(plan)
        self.assertIn(line, notes.pending_lines(path))
        self.assertIn('Owner: owner@example.org', path.read_text(encoding='utf-8'))
        self.assertEqual(plan['pages'][0]['after_bytes'], path.stat().st_size)


if __name__ == '__main__':
    import unittest
    unittest.main()
