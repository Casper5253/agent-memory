import hashlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentmem import hook, pipeline, status
from agentmem.manifest import Manifest
from test_p3_startup import StartupBase
import fixtures as fx


def long_ref(day, n):
    digest = hashlib.sha256(f"{day}-{n}".encode("utf-8")).hexdigest()
    return f"[[0-Sessions/2026/10/2026-10-{day:02d}-1641-claude-quantlab-u16-speed-up-merge-{digest}-p2#e{n}]]"


class StartupFitTests(StartupBase):
    def test_full_page_at_caps_keeps_every_section_within_cap(self):
        caps = {'purpose': 3, 'working_now': 8, 'latest': 5, 'active': 6, 'questions': 6, 'decisions': 8, 'next': 5}
        page = status.empty()
        for key, count in caps.items():
            page[key] = [status.Item(f'{key}{n}', f'{key} item {n} ' + 'detail ' * 9 + 'end', long_ref(10 + n, n))
                         for n in range(1, count + 1)]
        self.write_state(status.render(page))
        package = hook.startup_package(self.cfg, str(self.repo))
        for key in status.SECTIONS:
            self.assertIn(f'### {status.SECTIONS[key][0]}', package)
        self.assertLessEqual(len(package), hook.CONTEXT_CAP)
        self.assertIn('(2026-10-11#e1)', package)
        self.assertNotIn(long_ref(11, 1), package)


class NoProgressHoldTests(unittest.TestCase):
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

    def test_no_new_item_is_held_after_one_retry(self):
        def accepts_nothing(state, summary, link, date, timeout=0):
            return dict(sections=status.empty(), displaced=[]), 0.0

        def facts_only(digest, timeout=0):
            summary = fx.fake_summarize(digest, timeout)
            summary['decisions'] = []
            return summary
        self.capture("no-progress", facts_only, fx.failing_llm)
        result = pipeline.consolidate_pending(self.cfg, self.man, fx.fake_summarize, accepts_nothing)

        self.assertEqual({"consolidated": 0, "still_pending": 1}, result)
        self.assertEqual("no new item accepted", self.man.capture("claude", "no-progress", 1)["retry_hold"])
        self.assertEqual(0, len(self.man.retryable()))
        self.assertEqual(1, len(self.man.unconsolidated()))

    def test_model_unavailable_keeps_retrying(self):
        self.capture("transient", fx.fake_summarize, fx.failing_llm)
        result = pipeline.consolidate_pending(self.cfg, self.man, fx.fake_summarize, fx.failing_llm)

        self.assertEqual({"consolidated": 0, "still_pending": 1}, result)
        self.assertIsNone(self.man.capture("claude", "transient", 1)["retry_hold"])
        self.assertEqual(1, len(self.man.retryable()))


if __name__ == "__main__":
    unittest.main()
