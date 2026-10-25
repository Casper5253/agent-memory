import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentmem import llm, notes, pipeline, projects, remember, status
from test_knowledge_capture_recovery import CaptureBase
import fixtures as fx


DECISION = ('Record the heading level that set the section; while superseded, deeper headings stay '
            'superseded, and same or shallower headings end the section.')


class NoopTests(CaptureBase):
    def test_filtered_model_response_does_not_consolidate_capture(self):
        sid, _ = self.source()
        summary = remember._summary(DECISION, 'fact', '')
        pipeline.finalize(self.cfg, self.man, self.man.get('claude', sid),
                          lambda *a, **kw: summary.copy(), fx.failing_llm)
        row = self.man.capture('claude', sid, 1)
        link = Path(row['note_path']).with_suffix('').as_posix()
        # A realistic decision using a real summary claim, but an absent file specific.
        output = dict(sections={**status.empty(), 'working_now': [
            dict(text=DECISION + ' in invented.py', ref=f'[[{link}#e1]]')]}, displaced=[])
        with patch.object(llm, '_run', return_value=output):
            result = pipeline.consolidate_pending(self.cfg, self.man)
        self.assertEqual(result, dict(consolidated=0, still_pending=1))
        self.assertEqual(self.man.capture('claude', sid, 1)['consolidated'], 0)
        events = [json.loads(line) for line in self.cfg.log_path.read_text(encoding='utf-8').splitlines()]
        self.assertTrue(any(e.get('action') == 'dropped' for e in events))
        self.assertTrue(any(e.get('action') == 'rejected' and 'claim unsupported by citation' in e.get('item', '') for e in events))
        self.assertFalse(any('invented.py' in json.dumps(e) for e in events))
        self.assertTrue(any(e.get('event') == 'llm-fallback' and 'accepted' in e.get('error', '') for e in events))
        page = (self.cfg.projects_dir / 'Demo App' / 'Demo App.md').read_text(encoding='utf-8')
        self.assertIn('status update pending', page)

    def test_empty_update_stays_pending_but_partial_update_applies(self):
        when = datetime(2026, 10, 24, tzinfo=timezone.utc)
        for entries in ([], [dict(text=DECISION, ref='[[demo#e1]]'), dict(text=DECISION + ' in invented.py', ref='[[demo#e1]]')]):
            with self.subTest(entries=entries):
                summary = remember._summary(DECISION, 'decision' if entries else 'fact', '')
                output = dict(sections={**status.empty(), 'decisions': entries}, displaced=[])
                with patch.object(llm, '_run', return_value=output):
                    self.assertEqual(pipeline.consolidate_into_project(
                        self.cfg, 'Demo App', summary, 'demo', when, 'claude-haiku')[1], bool(entries))

    def test_legacy_noop_marker_is_not_proof_of_accepted_status(self):
        summary = remember._summary(DECISION, 'decision', '')
        path = self.cfg.projects_dir / 'Demo App' / 'Demo App.md'
        identity = 'synthetic-capture'
        notes.update_project_note(path, 'Demo App', '2026-10-24',
            session_line=f'- 2026-10-24 [[demo|demo]] <!-- agentmem:capture:{identity} -->', session_link='demo')
        output = dict(sections={**status.empty(), 'decisions': [dict(text=DECISION, ref='[[demo#e1]]')]}, displaced=[])
        with patch.object(llm, '_run', return_value=output) as model:
            self.assertTrue(pipeline.consolidate_into_project(self.cfg, 'Demo App', summary, 'demo',
                datetime(2026, 10, 24, tzinfo=timezone.utc), 'claude-haiku', capture_id=identity)[1])
        model.assert_called_once()
        self.assertIn(DECISION, path.read_text(encoding='utf-8'))

    def test_model_candidate_and_diagnostic_survive_typed_result(self):
        summary = remember._summary(DECISION, 'decision', '')
        output = dict(sections={**status.empty(), 'decisions': [dict(text=DECISION + ' in invented.py', ref='[[demo#e1]]')]}, displaced=[])
        with patch.object(llm, '_run', return_value=output):
            result, _ = llm.consolidate(notes.EMPTY_STATE, summary, '[[demo]]', '2026-10-24')
        self.assertEqual(status.proposed_update(result, status.empty()), output)


class ReceiptProjectTests(CaptureBase):
    def test_shared_continuation_retains_original_note_project(self):
        sid, source = self.source()
        self.finish(sid)
        first = self.man.capture('claude', sid, 1)
        fx.write_jsonl(source, fx.claude_lines(sid, self.cwd, fx.SWITCH), append=True)
        self.man.touch('claude', sid, source=str(source), cwd=self.cwd, end=True)
        with patch.object(projects, 'attribute', return_value=projects.Attribution('Another Project', 'cwd', {}, [])):
            self.finish(sid)
        second = self.man.capture('claude', sid, 2)
        receipt = pipeline._load_receipt(self.cfg, 'claude', sid, 2, second['start_offset'])
        self.assertEqual(second['project'], 'Another Project')
        self.assertEqual(second['note_path'], first['note_path'])
        self.assertEqual(receipt['note_project'], 'Demo App')

    def test_receipt_still_rejects_wrong_capture_project_and_bad_hash(self):
        sid, _ = self.source()
        self.crash_after_note(sid)
        path = pipeline._receipt_path(self.cfg, 'claude', sid, 1)
        original = json.loads(path.read_text(encoding='utf-8'))
        for change in ('project', 'hash'):
            with self.subTest(change=change):
                receipt = original.copy()
                if change == 'project':
                    receipt['project'] = 'Wrong Project'
                    receipt['receipt_sha'] = pipeline._receipt_hash(receipt)
                else:
                    receipt['content_sha'] = '0' * 64
                path.write_text(json.dumps(receipt), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'invalid receipt'):
                    pipeline._load_receipt(self.cfg, 'claude', sid, 1, 0)

    def test_new_note_after_legacy_part_uses_its_own_project(self):
        sid, source = self.source()
        self.finish(sid)
        first = self.man.capture('claude', sid, 1)
        path = self.cfg.vault / first['note_path']
        path.write_text(path.read_text(encoding='utf-8').replace('logical_session: true\n', ''), encoding='utf-8')
        fx.write_jsonl(source, fx.claude_lines(sid, self.cwd, fx.SWITCH), append=True)
        self.man.touch('claude', sid, source=str(source), cwd=self.cwd, end=True)
        attribution = projects.Attribution('Another Project', 'cwd', {}, [])
        with patch.object(projects, 'attribute', return_value=attribution):
            self.finish(sid)
        second = self.man.capture('claude', sid, 2)
        receipt = pipeline._load_receipt(self.cfg, 'claude', sid, 2, second['start_offset'])
        self.assertEqual(receipt['note_part'], 2)
        self.assertEqual(receipt['project'], 'Another Project')
        self.assertEqual(receipt['note_project'], 'Another Project')
        self.assertNotEqual(second['note_path'], first['note_path'])

    def test_frozen_receipt_with_stale_previous_project_can_resume(self):
        sid, _ = self.source()
        self.crash_after_note(sid)
        path = pipeline._receipt_path(self.cfg, 'claude', sid, 1)
        receipt = json.loads(path.read_text(encoding='utf-8'))
        # Existing live shape: separate note, matching note/capture identity and hashes,
        # but the writer copied the prior note's project into redundant metadata.
        receipt['note_project'] = 'Previous Project'
        receipt['receipt_sha'] = pipeline._receipt_hash(receipt)
        path.write_text(json.dumps(receipt), encoding='utf-8')
        before = path.read_bytes()
        self.finish(sid, lambda *a, **k: self.fail('frozen receipt must control retry'))
        self.assertEqual(self.man.capture('claude', sid, 1)['project'], 'Demo App')
        self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
