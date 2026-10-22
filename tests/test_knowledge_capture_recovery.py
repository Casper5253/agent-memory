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
from fixtures import make_vault, claude_lines, codex_lines, opencode_export, SUBSTANTIAL, SWITCH, write_jsonl, fake_summarize, fake_consolidate


class CaptureBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / '.audit')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = make_vault(self.root)
        self.man = Manifest(self.cfg.manifest_path)
        self.addCleanup(self.man.close)
        self.cwd = str(self.root / 'work' / 'demo-app')
        (self.root / 'work' / '.git').write_text('gitdir: nonexistent', encoding='utf-8')

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



class RecoveryTests(CaptureBase):
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

    def test_identical_session_ids_from_different_agents_are_distinct(self):
        sid, _ = self.source()
        self.finish(sid)
        path = self.root / 'codex-transcript.jsonl'
        write_jsonl(path, codex_lines(sid, self.cwd, SUBSTANTIAL))
        self.man.touch('codex', sid, source=str(path), cwd=self.cwd, end=True)
        pipeline.finalize(self.cfg, self.man, self.man.get('codex', sid), fake_summarize, fake_consolidate)
        rows = self.man.query_captures()
        self.assertEqual({'claude', 'codex'}, {r['agent'] for r in rows})
        self.assertEqual(2, len({r['note_path'] for r in rows}))

    def test_malformed_tail_keeps_valid_prefix_and_cannot_be_consumed(self):
        sid, path = self.source()
        end = path.stat().st_size
        with path.open('ab') as f:
            f.write(b'{"type": invalid}\n')
        self.assertEqual(end, adapters.read('claude', sid, str(path)).end)
        with self.assertRaisesRegex(ValueError, 'source contains malformed'):
            adapters.read('claude', sid, str(path), start=end)
        with self.assertRaisesRegex(ValueError, 'source contains malformed'):
            adapters.read('claude', sid, str(path), end=path.stat().st_size)

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


class DirectionATests(CaptureBase):
    def setUp(self):
        super().setUp()
        self.other = self.root / 'work' / 'other-tool'
        self.other.mkdir()
        self.data = projects.load_map(self.cfg.projects_map)
        self.data['projects'].append({'name': 'Other Tool', 'aliases': ['orbital'], 'roots': [str(self.other)]})
        self.save_map()

    def save_map(self):
        self.cfg.projects_map.write_text(json.dumps(self.data), encoding='utf-8')

    def test_content_routing_deduplicates_files_and_records_related(self):
        attribution = projects.attribute(self.cwd, self.cfg.projects_map,
                  files=[str(self.other / 'one.py')] * 30 + [str(self.other / 'two.py'), self.cwd + '/x.py'],
                  text='Work in ' + str(self.other / 'one.py'))
        self.assertEqual('Other Tool', attribution.project)
        self.assertEqual('content', attribution.method)
        self.assertEqual(2, attribution.signals['Other Tool']['files'])
        self.assertEqual(9, attribution.signals['Other Tool']['score'])
        self.assertEqual(['Demo App'], attribution.related_projects)

    def test_near_tie_and_no_content_keep_cwd(self):
        for files in ([], [self.cwd + '/a.py', str(self.other / 'b.py')]):
            attribution = projects.attribute(self.cwd, self.cfg.projects_map, files=files)
            self.assertEqual(('Demo App', 'cwd-fallback'), (attribution.project, attribution.method))

    def test_explicit_pin_wins(self):
        self.data['pins'] = [{'root': self.cwd, 'project': 'Demo App'}]
        self.save_map()
        attribution = projects.attribute(self.cwd, self.cfg.projects_map,
                  files=[str(self.other / 'one.py')], text='orbital ' + str(self.other / 'two.py'))
        self.assertEqual(('Demo App', 'pinned'), (attribution.project, attribution.method))

    def test_unknown_pin_warns_and_does_not_change_the_map(self):
        self.data['pins'] = [{'root': self.cwd, 'project': 'Missing Synthetic Project'}]
        self.save_map()
        before = self.cfg.projects_map.read_bytes()
        with self.assertLogs('agentmem.projects', level='WARNING') as logs:
            attribution = projects.attribute(self.cwd, self.cfg.projects_map, create=False)
        self.assertEqual(('Demo App', 'cwd-fallback'), (attribution.project, attribution.method))
        self.assertIn('unknown project', logs.output[0])
        self.assertEqual(before, self.cfg.projects_map.read_bytes())

    def test_capture_records_related_link_and_attribution_counts(self):
        sid, path = self.source()
        records = claude_lines(sid, self.cwd, SUBSTANTIAL)
        records[0]['message']['content'] += ' Review Other Tool.'
        for record in records:
            blocks = (record.get('message') or {}).get('content')
            for block in blocks if isinstance(blocks, list) else []:
                if block.get('name') == 'Edit':
                    block['input']['file_path'] = str(self.other / Path(block['input']['file_path']).name)
        write_jsonl(path, records)
        # One edit in the cwd project makes it a related project, while the other wins.
        records[-2]['message']['content'].append({'type': 'tool_use', 'name': 'Edit', 'input': {'file_path': self.cwd + '/one.py'}})
        write_jsonl(path, records)
        self.finish(sid)
        row = self.man.capture('claude', sid, 1)
        self.assertEqual('Other Tool', row['project'])
        text = (self.cfg.vault / row['note_path']).read_text(encoding='utf-8')
        self.assertIn('attribution_method: content', text)
        self.assertIn('related_projects: ["Demo App"]', text)
        related = projects.note_path(self.cfg.vault, 'Demo App').read_text(encoding='utf-8')
        self.assertIn('(related)', notes.get_block(related, notes.SESS_BEGIN, notes.SESS_END))

    def test_remembered_text_uses_same_content_rule(self):
        from agentmem.remember import remember_item
        result = remember_item(self.cfg, 'orbital uses port 8443 in ' + str(self.other / 'config.py'),
                               cwd=self.cwd, consolidator=fake_consolidate)
        self.assertEqual('Other Tool', result['project'])
        text = Path(result['evidence']).read_text(encoding='utf-8')
        self.assertIn('attribution_method: content', text)
        self.assertIn('attribution_signals:', text)
        self.assertIn('related_projects:', text)
        self.assertRegex(text, r'session_id: "remember-[0-9a-f]+"')

    def test_parent_repo_routes_worktree_without_creating_project(self):
        import subprocess
        subprocess.run(['git', 'init', '-q', self.cwd], check=True, capture_output=True)
        gitdir = Path(self.cwd) / '.git' / 'worktrees' / 'synthetic'
        gitdir.mkdir(parents=True)
        (gitdir / 'commondir').write_text('../..', encoding='utf-8')
        tree = self.root / 'work' / 'synthetic-tree'
        tree.mkdir()
        (tree / '.git').write_text('gitdir: ' + str(gitdir), encoding='utf-8')
        before = self.cfg.projects_map.read_bytes()
        self.assertEqual('Demo App', projects.resolve_session(str(tree), self.cfg.projects_map))
        self.assertEqual(before, self.cfg.projects_map.read_bytes())

    def test_head_stamped_and_null_outside_repository(self):
        import subprocess
        sid, _ = self.source()
        self.finish(sid)
        text = (self.cfg.vault / self.man.capture('claude', sid, 1)['note_path']).read_text(encoding='utf-8')
        self.assertIn('repo_head: null', text)
        subprocess.run(['git', 'init', '-q', self.cwd], check=True, capture_output=True)
        subprocess.run(['git', '-C', self.cwd, '-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.invalid',
                        'commit', '--allow-empty', '-q', '-m', 'synthetic'], check=True, capture_output=True)
        head = subprocess.run(['git', '-C', self.cwd, 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True).stdout.strip()
        sid, _ = self.source('synthetic-head')
        self.finish(sid)
        text = (self.cfg.vault / self.man.capture('claude', sid, 1)['note_path']).read_text(encoding='utf-8')
        self.assertIn('repo_head: "' + head + '"', text)
        self.assertIn('attribution_method:', text)

    def test_consolidation_prompt_requires_recorded_commit(self):
        self.assertIn('repo_head', llm.CONSOLIDATE_SYSTEM)
        self.assertIn('repository-state', llm.CONSOLIDATE_SYSTEM)

    def test_search_labels_located_pending_state_and_sessions(self):
        path = self.cfg.vault / '1-Projects/Demo App/Demo App.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        text = notes.new_project_note('Demo App', '2026-10-01')
        text = notes.set_block(text, notes.STATE_BEGIN, notes.STATE_END, 'state-only marker')
        text = notes.set_block(text, notes.PENDING_BEGIN, notes.PENDING_END, 'unreconciledneedle marker')
        text = notes.set_block(text, notes.SESS_BEGIN, notes.SESS_END, 'recent-only marker')
        path.write_text(text, encoding='utf-8')
        hits = [{'file': str(path), 'snippet': snippet} for snippet in
                ['state-only marker', 'unreconciledneedle marker', 'recent-only marker', 'unlocatable excerpt']]
        import subprocess
        with patch.object(search, '_qmd', return_value=subprocess.CompletedProcess([], 0, json.dumps(hits).encode(), b'')):
            got = search.search(self.cfg, 'marker')
        self.assertEqual(['CURRENT', 'PENDING', 'RECENT_SESSION', 'CURRENT'], [h['label'] for h in got])
        self.assertEqual({'label', 'path', 'title', 'score', 'line', 'docid', 'snippet'}, set(got[0]))
        fallback = search.fallback_search(self.cfg, 'unreconciledneedle', 8)
        self.assertEqual('PENDING', fallback[0]['label'])


if __name__ == '__main__':
    unittest.main()
