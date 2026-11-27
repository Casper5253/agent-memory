import copy
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentmem import adapters, pipeline  # noqa: E402
from agentmem.manifest import Manifest  # noqa: E402
import fixtures as fx  # noqa: E402


class LostPolicy(unittest.TestCase):
    def setUp(self):
        self._tmp = fx.tmpdir()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.db_path = self.tmp / "opencode.db"
        self.cfg = fx.make_vault(self.tmp, settle_minutes=1, opencode_db=self.db_path)
        self.man = Manifest(self.cfg.manifest_path)
        self.addCleanup(self.man.close)
        self.cwd = str(self.tmp / "work" / "demo-app")

    def make_opencode_db(self, updated_ms):
        fx.write_opencode_activity(self.db_path, "ses-open", updated_ms)

    def touch_old(self, agent, sid, cwd=None, source=None):
        self.man.touch(agent, sid, cwd=cwd or self.cwd, source=source, end=True,
                       now=time.time() - 3600)

    def test_live_opencode_source_change_is_retried_then_quiet_capture_happens_once(self):
        sid = "ses-open"
        quiet_ms = int((time.time() - 3600) * 1000)
        self.make_opencode_db(quiet_ms)
        self.touch_old("opencode", sid, source=sid)

        first = fx.opencode_export(sid, self.cwd, fx.SUBSTANTIAL)
        changed = copy.deepcopy(first)
        changed["messages"][0]["parts"][0]["text"] = "Changed while the first capture was being prepared."
        exports = [first, changed, changed, changed, changed, changed]
        calls = []

        def export(session_id, timeout=120):
            calls.append(session_id)
            return exports[min(len(calls) - 1, len(exports) - 1)]

        with mock.patch.object(adapters, "export_opencode", side_effect=export):
            first_results = pipeline.process_ready(self.cfg, self.man, summarizer=fx.fake_summarize,
                                                   consolidator=fx.fake_consolidate)
            row = self.man.get("opencode", sid)
            self.assertEqual("pending", row["status"])
            self.assertEqual(0, row["attempts"])
            self.assertEqual(0, len(self.man.query_captures()))
            self.assertTrue(any(result[2] == "deferred-source-change" for result in first_results))

            self.man.mark("opencode", sid, next_try=0)
            second_results = pipeline.process_ready(self.cfg, self.man, summarizer=fx.fake_summarize,
                                                    consolidator=fx.fake_consolidate)
            row = self.man.get("opencode", sid)
            self.assertEqual("captured", row["status"])
            self.assertEqual(1, row["parts"])
            self.assertEqual(1, len(self.man.query_captures()))
            self.assertIn(("opencode", sid, "captured"), second_results)

    def test_active_opencode_message_waits_for_the_settle_window(self):
        sid = "ses-open"
        active_ms = int(time.time() * 1000)
        self.make_opencode_db(active_ms)
        self.touch_old("opencode", sid, source=sid)
        data = fx.opencode_export(sid, self.cwd, fx.SUBSTANTIAL)

        with mock.patch.object(adapters, "export_opencode", return_value=data):
            results = pipeline.process_ready(self.cfg, self.man, summarizer=fx.fake_summarize,
                                             consolidator=fx.fake_consolidate)
            row = self.man.get("opencode", sid)
            self.assertEqual("pending", row["status"])
            self.assertEqual(0, row["attempts"])
            self.assertEqual(0, len(self.man.query_captures()))
            self.assertTrue(any(result[2] == "deferred-active-source" for result in results))

            quiet_ms = int((time.time() - 3600) * 1000)
            fx.write_opencode_activity(self.db_path, sid, quiet_ms)
            self.man.mark("opencode", sid, next_try=0)
            results = pipeline.process_ready(self.cfg, self.man, summarizer=fx.fake_summarize,
                                             consolidator=fx.fake_consolidate)
            row = self.man.get("opencode", sid)
            self.assertEqual("captured", row["status"])
            self.assertEqual(1, row["parts"])
            self.assertIn(("opencode", sid, "captured"), results)

    def test_codex_memory_curation_is_skipped_with_a_reason(self):
        for dirname in ("memories", "memories_v2"):
            with self.subTest(dirname=dirname):
                sid = "codex-memory-" + dirname
                cwd = str(Path.home() / ".codex" / dirname)
                self.touch_old("codex", sid, cwd=cwd, source=str(self.tmp / "missing-rollout.jsonl"))
                pipeline.process_ready(self.cfg, self.man, summarizer=fx.fake_summarize,
                                       consolidator=fx.fake_consolidate)
                row = self.man.get("codex", sid)
                self.assertEqual("skipped", row["status"])
                self.assertEqual("internal memory curation", row["skip_reason"])
                self.assertEqual(0, row["attempts"])
                self.assertIsNone(self.man.capture("codex", sid, 1))

    def test_normal_codex_session_is_unaffected(self):
        sid = "01a16d2b-0000-7000-8000-0000000000d2"
        source = self.tmp / "transcripts" / "codex" / f"rollout-2026-10-24T10-00-00-{sid}.jsonl"
        fx.write_jsonl(source, fx.codex_lines(sid, self.cwd, fx.SUBSTANTIAL))
        self.touch_old("codex", sid, source=str(source))
        results = pipeline.process_ready(self.cfg, self.man, summarizer=fx.fake_summarize,
                                         consolidator=fx.fake_consolidate)
        row = self.man.get("codex", sid)
        self.assertEqual("captured", row["status"])
        self.assertEqual(1, row["parts"])
        self.assertIn(("codex", sid, "captured"), results)


if __name__ == "__main__":
    unittest.main()
