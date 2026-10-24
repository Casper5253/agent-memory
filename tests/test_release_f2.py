import json
import re
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentmem import llm, notes, pipeline, remember, status
from test_knowledge_capture_recovery import CaptureBase
import fixtures as fx


class ContinuationEvidenceTests(unittest.TestCase):
    def test_sqlite_then_postgres_has_distinct_resolving_page_refs(self):
        when = datetime(2026, 10, 24, tzinfo=timezone.utc)
        first = remember._summary('Use SQLite storage', 'decision', '')
        second = {**remember._summary('Use Postgres storage', 'decision', ''), '_evidence_offset': 1}
        text = notes.render_session(agent='claude', session_id='synthetic', project='Demo', part=1,
            when=when, started=when.isoformat(), ended='', source='synthetic.jsonl', start=0, end=10,
            summarizer='fake', summary=first, files=[], commands=[], gate_reason='synthetic')
        continued = notes.append_session_continuation(text, when=when, source='synthetic.jsonl', start=10,
            end=20, source_hash='0' * 64, summarizer='fake', summary=second, files=[], commands=[],
            gate_reason='synthetic', capture_part=2, project='Demo')
        self.assertTrue(continued.startswith(text.rstrip()))
        self.assertEqual(status.evidence_refs(first, 'session'), {'[[session#e1]]': 'Use SQLite storage'})
        self.assertEqual(status.evidence_refs(second, 'session'), {'[[session#e2]]': 'Use Postgres storage'})
        output = dict(sections={**status.empty(), 'decisions': [dict(text='Use Postgres storage', ref='[[session#e2]]')]}, displaced=[])
        with patch.object(llm, '_run', return_value=output) as model:
            page, _ = llm.consolidate(notes.EMPTY_STATE, second, '[[session]]', '2026-10-24')
        prompt = json.loads(model.call_args.args[1])
        self.assertEqual(prompt['summary']['citations'][0]['evidence_id'], 'e2')
        self.assertEqual(prompt['allowed_refs'], ['[[session#e2]]'])
        ref = page.sections['decisions'][0].ref
        anchor = re.search(r'#(e\d+)\]\]$', ref)[1]
        section = re.search(r'(?ms)^#{3,4} ' + anchor + r'\n(.*?)(?=^#{2,4} |\Z)', continued, re.I)[1]
        self.assertIn('- Claim: Use Postgres storage', section)
        self.assertNotIn('Use SQLite storage', section)
        self.assertEqual(len(re.findall(r'(?m)^#{3,4} E\d+$', continued)), 2)


class ContinuationPipelineTests(CaptureBase):
    def test_frozen_continuation_offset_survives_retry_and_later_growth(self):
        sid, source = self.source()
        self.finish(sid)
        first = self.man.capture('claude', sid, 1)
        first_summary = json.loads(first['summary_json'])
        fx.write_jsonl(source, fx.claude_lines(sid, self.cwd, fx.SWITCH), append=True)
        self.man.touch('claude', sid, source=str(source), cwd=self.cwd, end=True)
        pipeline.finalize(self.cfg, self.man, self.man.get('claude', sid), fx.fake_summarize, fx.failing_llm)
        second = self.man.capture('claude', sid, 2)
        summary = json.loads(second['summary_json'])
        self.assertEqual(summary['_evidence_offset'], len(first_summary['citations']))
        link = Path(second['note_path']).with_suffix('').as_posix()
        first_refs = status.evidence_refs(first_summary, link)
        second_refs = status.evidence_refs(summary, link)
        self.assertTrue(set(first_refs).isdisjoint(second_refs))
        path = self.cfg.vault / second['note_path']
        before = path.read_bytes()
        result = pipeline.consolidate_pending(self.cfg, self.man, consolidator=fx.fake_consolidate)
        self.assertEqual(result['consolidated'], 1)
        self.assertEqual(path.read_bytes(), before)
        page = (self.cfg.projects_dir / 'Demo App' / 'Demo App.md').read_text(encoding='utf-8')
        self.assertTrue(any(ref in page for ref in second_refs))
        for ref, claim in second_refs.items():
            anchor = re.search(r'#(e\d+)\]\]$', ref)[1]
            section = re.search(r'(?ms)^#{3,4} ' + anchor + r'\n(.*?)(?=^#{2,4} |\Z)', before.decode(), re.I)[1]
            self.assertIn('- Claim: ' + claim, section)
        fx.write_jsonl(source, fx.claude_lines(sid, self.cwd, fx.SUBSTANTIAL), append=True)
        self.man.touch('claude', sid, source=str(source), cwd=self.cwd, end=True)
        self.finish(sid)
        third = self.man.capture('claude', sid, 3)
        third_summary = json.loads(third['summary_json'])
        self.assertEqual(third_summary['_evidence_offset'], summary['_evidence_offset'] + len(summary['citations']))
        self.assertTrue(set(second_refs).isdisjoint(status.evidence_refs(third_summary, link)))
        self.assertTrue(path.read_bytes().startswith(before.rstrip()))

    def test_old_unanchored_continuation_is_held_without_rewriting_evidence(self):
        sid, source = self.source()
        self.finish(sid)
        fx.write_jsonl(source, fx.claude_lines(sid, self.cwd, fx.SWITCH), append=True)
        self.man.touch('claude', sid, source=str(source), cwd=self.cwd, end=True)
        pipeline.finalize(self.cfg, self.man, self.man.get('claude', sid), fx.fake_summarize, fx.failing_llm)
        row = self.man.capture('claude', sid, 2)
        path = self.cfg.vault / row['note_path']
        receipt_path = pipeline._receipt_path(self.cfg, 'claude', sid, 2)
        receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
        receipt['summary'].pop('_evidence_offset')
        receipt['text'] = re.sub(r'(?ms)^### Evidence\n.*?(?=^### Receipt)', '', receipt['text'])
        receipt['content_sha'] = pipeline._hash(receipt['text'])
        receipt['receipt_sha'] = pipeline._receipt_hash(receipt)
        receipt_path.write_text(json.dumps(receipt), encoding='utf-8')
        path.write_text(receipt['text'], encoding='utf-8', newline='\n')
        self.man.db.execute('UPDATE captures SET summary_json=? WHERE agent=? AND session_id=? AND part=2',
                            (json.dumps(receipt['summary']), 'claude', sid))
        before = path.read_bytes(), receipt_path.read_bytes()
        result = pipeline.consolidate_pending(self.cfg, self.man,
            consolidator=lambda *a, **kw: self.fail('old unanchored summary must not reach model'))
        self.assertEqual(result, dict(consolidated=0, still_pending=1))
        self.assertIsNotNone(self.man.capture('claude', sid, 2)['retry_hold'])
        self.assertEqual((path.read_bytes(), receipt_path.read_bytes()), before)


if __name__ == '__main__':
    unittest.main()
