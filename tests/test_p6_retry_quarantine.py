import re
import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentmem import notes, pipeline
from agentmem.manifest import Manifest
import fixtures as fx


def uncited_summarize(digest: str, timeout=0) -> dict:
    result = fx.fake_summarize(digest, timeout)
    result["citations"] = []
    return result


class RetryQuarantineTests(unittest.TestCase):
    def setUp(self):
        self.tmp_ctx = fx.tmpdir()
        self.tmp = Path(self.tmp_ctx.name)
        self.cfg = fx.make_vault(self.tmp)
        self.man = Manifest(self.cfg.manifest_path)
        self.cwd = str(self.tmp / "work" / "demo-app")

    def tearDown(self):
        self.man.close()
        self.tmp_ctx.cleanup()

    def capture(self, sid, summarizer, consolidator):
        source = self.tmp / "transcripts" / "claude" / f"{sid}.jsonl"
        fx.write_jsonl(source, fx.claude_lines(sid, self.cwd, fx.SUBSTANTIAL))
        self.man.touch("claude", sid, source=str(source), cwd=self.cwd, end=True)
        return pipeline.finalize(self.cfg, self.man, self.man.get("claude", sid), summarizer, consolidator)

    def held(self):
        return self.man.db.execute("SELECT count(*) FROM captures WHERE retry_hold IS NOT NULL").fetchone()[0]

    def test_twenty_uncited_captures_do_not_block_newer_retry(self):
        for n in range(20):
            self.capture(f"uncited-{n:02d}", uncited_summarize, fx.failing_llm)
        self.capture("newer", fx.fake_summarize, fx.failing_llm)
        self.assertEqual(21, len(self.man.unconsolidated()))

        result = pipeline.consolidate_pending(self.cfg, self.man, fx.fake_summarize, fx.fake_consolidate)

        self.assertEqual({"consolidated": 1, "still_pending": 0}, result)
        self.assertEqual(1, self.man.capture("claude", "newer", 1)["consolidated"])
        self.assertEqual(20, len(self.man.unconsolidated()))
        self.assertEqual(20, self.held())
        self.assertTrue(all(self.man.capture("claude", f"uncited-{n:02d}", 1)["retry_hold"] == "uncited summary"
                            for n in range(20)))

    def test_held_capture_keeps_its_note_and_summary(self):
        self.capture("uncited-kept", uncited_summarize, fx.failing_llm)
        before = self.man.capture("claude", "uncited-kept", 1)
        pipeline.consolidate_pending(self.cfg, self.man, fx.fake_summarize, fx.fake_consolidate)

        after = self.man.capture("claude", "uncited-kept", 1)
        self.assertEqual(before["note_path"], after["note_path"])
        self.assertEqual(before["summary_json"], after["summary_json"])
        self.assertTrue((self.cfg.vault / before["note_path"]).exists())
        self.assertEqual(1, len(self.man.unconsolidated()))

    def test_transient_failure_keeps_retrying(self):
        self.capture("cited-transient", fx.fake_summarize, fx.failing_llm)
        result = pipeline.consolidate_pending(self.cfg, self.man, fx.fake_summarize, fx.failing_llm)
        self.assertEqual({"consolidated": 0, "still_pending": 1}, result)
        self.assertEqual(0, self.held())
        self.assertEqual(1, len(self.man.retryable()))

    def updated_stamp(self):
        text = (self.cfg.projects_dir / "Demo App" / "Demo App.md").read_text(encoding="utf-8")
        return re.search(r"(?m)^updated: (.*)$", text)[1].strip()

    def test_failed_retry_does_not_touch_updated(self):
        self.capture("current", fx.fake_summarize, fx.failing_llm)
        stamp = self.updated_stamp()
        historical = datetime(2001, 1, 2, 9, 0)
        pipeline.consolidate_into_project(self.cfg, "Demo App", uncited_summarize("x"), "[[old-session]]",
                                          historical, "claude", fx.failing_llm, retry=True)
        self.assertEqual(stamp, self.updated_stamp())

    def test_successful_retry_never_moves_updated_backwards(self):
        self.capture("current", fx.fake_summarize, fx.failing_llm)
        stamp = self.updated_stamp()
        historical = datetime(2001, 1, 2, 9, 0)
        pipeline.consolidate_into_project(self.cfg, "Demo App", fx.fake_summarize("x"), "[[old-session]]",
                                          historical, "claude", fx.fake_consolidate, retry=True)
        self.assertEqual(stamp, self.updated_stamp())

    def test_touch_updated_only_moves_forward(self):
        note = "---\ntype: project\nupdated: 2026-10-17\n---\nbody"
        self.assertIn("updated: 2026-10-17", notes.touch_updated(note, "2026-10-16"))
        self.assertIn("updated: 2026-10-18", notes.touch_updated(note, "2026-10-18"))

    def test_update_without_touch_keeps_stamp(self):
        self.capture("current", fx.fake_summarize, fx.failing_llm)
        stamp = self.updated_stamp()
        path = self.cfg.projects_dir / "Demo App" / "Demo App.md"
        notes.update_project_note(path, "Demo App", "2001-01-02", pending_add=["- [ ] note"], touch=False)
        self.assertEqual(stamp, self.updated_stamp())

    def test_migration_adds_hold_column_to_existing_manifest(self):
        self.man.db.execute("ALTER TABLE captures DROP COLUMN retry_hold")
        self.man.close()
        self.man = Manifest(self.cfg.manifest_path)
        self.assertEqual(0, self.held())


if __name__ == "__main__":
    unittest.main()
