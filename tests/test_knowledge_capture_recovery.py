"""Synthetic crash recovery and direction A contract tests."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentmem import adapters, llm, notes, pipeline, projects, search
from agentmem.manifest import Manifest
from agentmem.redact import redact
from fixtures import make_vault, claude_lines, opencode_export, SUBSTANTIAL, SWITCH, write_jsonl, fake_summarize, fake_consolidate


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / '.audit')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = make_vault(self.root)
        self.man = Manifest(self.cfg.manifest_path)
        self.addCleanup(self.man.close)
        self.cwd = str(self.root / 'work' / 'demo-app')

    def source(self, sid='synthetic-a-12345678', agent='claude', turns=SUBSTANTIAL):
        path = self.root / (sid + '.jsonl')
        write_jsonl(path, claude_lines(sid, self.cwd, turns))
        self.man.touch(agent, sid, source=str(path), cwd=self.cwd, end=True)
        return sid, path

    def finish(self, sid, summarize=fake_summarize):
        return pipeline.finalize(self.cfg, self.man, self.man.get('claude', sid), summarize, fake_consolidate)

    def crash_after_note(self, sid):
        with patch.object(self.man, 'record_capture', side_effect=RuntimeError('crash after note')):
            with self.assertRaisesRegex(RuntimeError, 'crash after note'):
                self.finish(sid)

    def test_identity_survives_but_transcript_cookies_are_masked(self):
        sid, _ = self.source()
        self.finish(sid)
        text = (self.cfg.vault / self.man.capture('claude', sid, 1)['note_path']).read_text(encoding='utf-8')
        self.assertIn('session_id: "' + sid + '"', text)
        self.assertEqual('session_id: [REDACTED]', redact('session_id: synthetic-cookie-value'))
        self.assertEqual('Cookie: [REDACTED]', redact('Cookie: synthetic-cookie-value'))

    def test_suffix_collision_is_distinct(self):
        for sid in ('synthetic-a-12345678', 'synthetic-b-12345678'):
            self.source(sid)
            self.finish(sid)
        rows = self.man.query_captures()
        self.assertEqual(2, len({r['note_path'] for r in rows}))
        for r in rows:
            self.assertIn('session_id: "' + r['session_id'] + '"', (self.cfg.vault / r['note_path']).read_text(encoding='utf-8'))

    def test_retry_uses_first_evidence_without_model_or_source(self):
        sid, path = self.source()
        self.crash_after_note(sid)
        before = {p: p.read_bytes() for p in self.cfg.sessions_dir.rglob('*.md')}
        path.unlink()
        def forbidden(*a, **k):
            self.fail('recovery must not summarize again')
        self.finish(sid, forbidden)
        self.assertEqual(before, {p: p.read_bytes() for p in self.cfg.sessions_dir.rglob('*.md')})
        row = self.man.capture('claude', sid, 1)
        self.assertEqual('Fake summary of the session.', json.loads(row['summary_json'])['summary'])
        receipts = list((self.cfg.meta / 'capture-receipts').glob('*.json'))
        self.assertEqual(1, len(receipts))
        self.assertEqual(sid, json.loads(receipts[0].read_text(encoding='utf-8'))['session_id'])

    def test_existing_corrupt_note_is_not_adopted(self):
        sid, _ = self.source()
        self.crash_after_note(sid)
        path = next(self.cfg.sessions_dir.rglob('*.md'))
        path.write_text('broken frontmatter and body', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'capture_identity_conflict'):
            self.finish(sid)
        self.assertIsNone(self.man.capture('claude', sid, 1))
        self.assertEqual('broken frontmatter and body', path.read_text(encoding='utf-8'))

    def test_receipt_precedes_note_and_controls_retry(self):
        sid, _ = self.source()
        original = pipeline.create_once
        def crash(path, text):
            if path.suffix == '.md':
                raise RuntimeError('before note')
            return original(path, text)
        with patch.object(pipeline, 'create_once', side_effect=crash):
            with self.assertRaisesRegex(RuntimeError, 'before note'):
                self.finish(sid)
        self.assertEqual(1, len(list((self.cfg.meta / 'capture-receipts').glob('*.json'))))
        self.finish(sid, lambda *a, **k: self.fail('receipt must control retry'))
        self.assertEqual(1, len(list(self.cfg.sessions_dir.rglob('*.md'))))

    def test_bounded_reads_and_truncation(self):
        sid, path = self.source()
        end = adapters.read('claude', sid, str(path)).end
        write_jsonl(path, claude_lines(sid, self.cwd, SWITCH), append=True)
        old = adapters.read('claude', sid, str(path), end=end)
        self.assertEqual(end, old.end)
        self.assertFalse(any('Postgres' in e.text for e in old.events))
        path.write_bytes(path.read_bytes()[:end-1])
        with self.assertRaisesRegex(ValueError, 'source.*(truncat|range)'):
            adapters.read('claude', sid, str(path), end=end)

    def test_partial_line_does_not_advance(self):
        sid, path = self.source()
        end = path.stat().st_size
        with path.open('ab') as f:
            f.write(b'{"type":"user"')
        self.assertEqual(end, adapters.read('claude', sid, str(path)).end)
        with self.assertRaises(ValueError):
            adapters.read('claude', sid, str(path), end=path.stat().st_size)

    def test_opencode_exact_end_and_earlier_mutation(self):
        sid = 'synthetic-open'
        data = opencode_export(sid, self.cwd, SUBSTANTIAL)
        old_end = len(data['messages'])
        data['messages'] += opencode_export(sid, self.cwd, SWITCH)['messages']
        t = adapters.read_opencode(sid, data=data, end=old_end)
        self.assertEqual(old_end, t.end)
        self.assertFalse(any('Postgres' in e.text for e in t.events))

    def test_pending_reconciliation_reuses_persisted_summary(self):
        sid, path = self.source()
        self.finish(sid)
        self.man.set_consolidated('claude', sid, 1, 0)
        self.man.db.execute("UPDATE captures SET summarizer='deterministic'")
        path.unlink()
        result = pipeline.consolidate_pending(self.cfg, self.man,
                    summarizer=lambda *a, **k: self.fail('must reuse immutable summary'), consolidator=fake_consolidate)
        self.assertEqual({'consolidated': 1, 'still_pending': 0}, result)

    def test_resumed_part_without_content_signals_keeps_cwd_project(self):
        sid, path = self.source()
        self.finish(sid)
        neutral = [('Continue the investigation.', 'Checked the implementation. ' * 100, [], []),
                   ('Confirm the result.', 'Confirmed the result. ' * 100, [], [])]
        write_jsonl(path, claude_lines(sid, self.cwd, neutral), append=True)
        self.man.touch('claude', sid, source=str(path), cwd=self.cwd, end=True)
        self.finish(sid)
        self.assertEqual('Demo App', self.man.capture('claude', sid, 2)['project'])

    def test_opencode_mutated_prior_part_fails_without_second_note(self):
        sid = 'synthetic-open-mutation'
        data = opencode_export(sid, self.cwd, SUBSTANTIAL)
        self.man.touch('opencode', sid, cwd=self.cwd, source=sid, end=True)
        with patch.object(adapters, 'export_opencode', return_value=data):
            pipeline.finalize(self.cfg, self.man, self.man.get('opencode', sid), fake_summarize, fake_consolidate)
        data['messages'][0]['parts'][0]['text'] = 'Mutated earlier user text.'
        data['messages'] += opencode_export(sid, self.cwd, SWITCH)['messages']
        self.man.touch('opencode', sid, end=True)
        with patch.object(adapters, 'export_opencode', return_value=data):
            with self.assertRaisesRegex(ValueError, 'source_change'):
                pipeline.finalize(self.cfg, self.man, self.man.get('opencode', sid), fake_summarize, fake_consolidate)
        self.assertEqual(1, len(self.man.query_captures()))
        self.assertEqual(1, len(list(self.cfg.sessions_dir.rglob('*.md'))))

    def test_create_once_without_hardlinks_publishes_complete_bytes(self):
        from agentmem.fsutil import create_once
        path = self.root / 'new-note.md'
        with patch('agentmem.fsutil.os.link', side_effect=OSError('unsupported')):
            self.assertTrue(create_once(path, 'complete evidence'))
            self.assertFalse(create_once(path, 'different evidence'))
        self.assertEqual(b'complete evidence', path.read_bytes())

    def test_legacy_recovery_keeps_original_bytes_and_summary(self):
        sid, path = self.source()
        self.crash_after_note(sid)
        note = next(self.cfg.sessions_dir.rglob('*.md'))
        before = note.read_bytes()
        legacy_path = note.with_name('legacy-12345678.md')
        note.rename(legacy_path)
        for receipt in (self.cfg.meta / 'capture-receipts').glob('*.json'):
            receipt.unlink()
        path.unlink()
        self.finish(sid, lambda *a, **k: self.fail('legacy evidence must control recovery'))
        row = self.man.capture('claude', sid, 1)
        self.assertEqual(before, (self.cfg.vault / row['note_path']).read_bytes())
        self.assertEqual('Fake summary of the session.', json.loads(row['summary_json'])['summary'])


if __name__ == '__main__':
    unittest.main()
