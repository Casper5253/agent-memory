import os
import time
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentmem import notes, pipeline
from agentmem.manifest import Manifest
import fixtures as fx


class PipelineReview(unittest.TestCase):
    def setUp(self):
        self.tmp_ctx = fx.tmpdir()
        self.tmp = Path(self.tmp_ctx.name)
        self.cfg = fx.make_vault(self.tmp)
        self.man = Manifest(self.cfg.manifest_path)
        self.cwd = str(self.tmp / "work" / "demo-app")

    def tearDown(self):
        self.man.close()
        self.tmp_ctx.cleanup()

    def capture(self, sid, turns, consolidator):
        source = self.tmp / "transcripts" / "claude" / f"{sid}.jsonl"
        fx.write_jsonl(source, fx.claude_lines(sid, self.cwd, turns))
        self.man.touch("claude", sid, source=str(source), cwd=self.cwd, end=True)
        return pipeline.finalize(self.cfg, self.man, self.man.get("claude", sid),
                                 fx.fake_summarize, consolidator)

    def test_continuation_is_consolidated_as_its_own_capture(self):
        sid = "continued-identity"
        self.capture(sid, fx.SUBSTANTIAL, fx.fake_consolidate)
        source = self.tmp / "transcripts" / "claude" / f"{sid}.jsonl"
        fx.write_jsonl(source, fx.claude_lines(sid, self.cwd, fx.SWITCH), append=True)
        self.man.touch("claude", sid, source=str(source), cwd=self.cwd, end=True)
        pipeline.finalize(self.cfg, self.man, self.man.get("claude", sid),
                          fx.fake_summarize, fx.fake_consolidate)
        row = self.man.capture("claude", sid, 2)
        state = notes.get_block((self.cfg.projects_dir / "Demo App" / "Demo App.md").read_text(encoding="utf-8"),
                                notes.STATE_BEGIN, notes.STATE_END)
        self.assertEqual(row["consolidated"], 1)
        self.assertIn("Postgres", state)

    def test_pending_continuations_retry_each_capture_in_order(self):
        sid = "pending-continued-identity"
        self.capture(sid, fx.SUBSTANTIAL, fx.failing_llm)
        source = self.tmp / "transcripts" / "claude" / f"{sid}.jsonl"
        fx.write_jsonl(source, fx.claude_lines(sid, self.cwd, fx.SWITCH), append=True)
        self.man.touch("claude", sid, source=str(source), cwd=self.cwd, end=True)
        pipeline.finalize(self.cfg, self.man, self.man.get("claude", sid), fx.fake_summarize, fx.failing_llm)
        self.assertEqual([self.man.capture("claude", sid, part)["consolidated"] for part in (1, 2)], [0, 0])
        calls = []
        def ordered_consolidate(state, summary, link, date, timeout=0):
            calls.append(summary["decisions"][0]["decision"])
            return fx.fake_consolidate(state, summary, link, date, timeout)
        result = pipeline.consolidate_pending(self.cfg, self.man, fx.fake_summarize, ordered_consolidate)
        self.assertEqual(result, {"consolidated": 2, "still_pending": 0})
        self.assertEqual([self.man.capture("claude", sid, part)["consolidated"] for part in (1, 2)], [1, 1])
        state = notes.get_block((self.cfg.projects_dir / "Demo App" / "Demo App.md").read_text(encoding="utf-8"),
                                notes.STATE_BEGIN, notes.STATE_END)
        self.assertEqual(calls, ["Use SQLite for storage", "Switch storage to Postgres instead of SQLite"])
        self.assertIn("Postgres", state)

    def test_discovery_reopens_only_when_source_grows_past_cursor(self):
        sid = "discovered-growth"
        source = self.tmp / "transcripts" / "claude" / f"{sid}.jsonl"
        fx.write_jsonl(source, fx.claude_lines(sid, self.cwd, fx.SUBSTANTIAL))
        self.man.touch("claude", sid, source=str(source), cwd=self.cwd, end=True)
        pipeline.finalize(self.cfg, self.man, self.man.get("claude", sid), fx.fake_summarize, fx.fake_consolidate)
        row = self.man.get("claude", sid)
        cursor = row["captured_until"]
        source_mtime = time.time() - 2000
        discovered = pipeline._discover_orphan_transcripts(self.cfg, self.man, now=time.time())
        self.assertEqual(discovered, 0)
        self.assertEqual(self.man.get("claude", sid)["status"], "captured")
        fx.write_jsonl(source, fx.claude_lines(sid, self.cwd, fx.SWITCH), append=True)
        os.utime(source, (source_mtime, source_mtime))
        discovered = pipeline._discover_orphan_transcripts(self.cfg, self.man, now=time.time())
        self.assertEqual(discovered, 1)
        self.assertEqual(self.man.get("claude", sid)["status"], "pending")
        self.assertEqual(self.man.get("claude", sid)["captured_until"], cursor)
        pipeline.finalize(self.cfg, self.man, self.man.get("claude", sid), fx.fake_summarize, fx.fake_consolidate)
        self.assertEqual(self.man.capture("claude", sid, 2)["consolidated"], 1)
        note_path = self.cfg.vault / self.man.capture("claude", sid, 1)["note_path"]
        self.assertEqual(note_path.read_text(encoding="utf-8").count("## Continued"), 1)
        self.assertEqual(pipeline._discover_orphan_transcripts(self.cfg, self.man, now=time.time()), 0)

    def test_discovery_reopens_skipped_session_after_substantive_growth(self):
        sid = "skipped-growth"
        source = self.tmp / "transcripts" / "claude" / f"{sid}.jsonl"
        fx.write_jsonl(source, fx.claude_lines(sid, self.cwd, fx.TRIVIAL))
        self.man.touch("claude", sid, source=str(source), cwd=self.cwd, end=True)
        pipeline.finalize(self.cfg, self.man, self.man.get("claude", sid), fx.fake_summarize, fx.fake_consolidate)
        row = self.man.get("claude", sid)
        self.assertEqual((row["status"], row["captured_until"]), ("skipped", source.stat().st_size))
        source_mtime = time.time() - 2000
        fx.write_jsonl(source, fx.claude_lines(sid, self.cwd, fx.SUBSTANTIAL), append=True)
        os.utime(source, (source_mtime, source_mtime))
        self.assertEqual(pipeline._discover_orphan_transcripts(self.cfg, self.man, now=time.time()), 1)
        self.assertEqual(self.man.get("claude", sid)["status"], "pending")
        pipeline.finalize(self.cfg, self.man, self.man.get("claude", sid), fx.fake_summarize, fx.fake_consolidate)
        self.assertEqual(self.man.get("claude", sid)["status"], "captured")
        note = self.cfg.vault / self.man.capture("claude", sid, 1)["note_path"]
        self.assertEqual(note.read_text(encoding="utf-8").count("## Continued"), 0)


if __name__ == "__main__":
    unittest.main()
