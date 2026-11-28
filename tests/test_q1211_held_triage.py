import hashlib
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentmem import notes, pipeline, status
from agentmem.fsutil import LockTimeout
from agentmem.manifest import Manifest
import fixtures as fx


def accepts_nothing(state, summary, link, date, timeout=0):
    return dict(sections=status.empty(), displaced=[]), 0.0


def cost_consolidate(state, summary, link, date, timeout=0):
    proposal, _ = fx.fake_consolidate(state, summary, link, date, timeout)
    return proposal, 1.25


def uncited_summarize(digest, timeout=0):
    summary = fx.fake_summarize(digest, timeout)
    summary["citations"] = []
    return summary


def facts_only(digest, timeout=0):
    summary = fx.fake_summarize(digest, timeout)
    summary["decisions"] = []
    return summary


def facts_without_next(digest, timeout=0):
    summary = facts_only(digest, timeout)
    summary["next_steps"] = []
    summary["citations"] = [c for c in summary["citations"] if c.get("section") != "next_steps"]
    return summary


def unsupported_fact(digest, timeout=0):
    summary = facts_only(digest, timeout)
    summary["next_steps"] = []
    summary["facts"] = ["tests pass"]
    summary["citations"] = [{"section": "summary", "claim": "tests pass", "quote": "synthetic"}]
    return summary


def citation_only(digest, timeout=0):
    summary = facts_only(digest, timeout)
    summary["facts"] = []
    summary["next_steps"] = []
    summary["citations"] = [{"section": "summary", "claim": "A summary claim.", "quote": "synthetic"}]
    return summary


def cited_omission(digest, timeout=0):
    summary = facts_only(digest, timeout)
    summary["facts"] = ["A local cache keeps reads fast"]
    summary["next_steps"] = []
    summary["open_questions"] = []
    summary["decisions"] = []
    summary["citations"] = [{"section": "facts", "claim": "A cited cache fact the model omitted", "quote": "synthetic fixture evidence"}]
    return summary


class HeldTriageTests(unittest.TestCase):
    def setUp(self):
        self.tmp_ctx = fx.tmpdir()
        self.tmp = Path(self.tmp_ctx.name)
        self.cfg = fx.make_vault(self.tmp)
        self.man = Manifest(self.cfg.manifest_path)
        self.cwd = str(self.tmp / "work" / "demo-app")

    def tearDown(self):
        self.man.close()
        self.tmp_ctx.cleanup()

    def capture(self, sid, summarizer, consolidator=accepts_nothing):
        source = self.tmp / "transcripts" / "claude" / f"{sid}.jsonl"
        fx.write_jsonl(source, fx.claude_lines(sid, self.cwd, fx.SUBSTANTIAL))
        self.man.touch("claude", sid, source=str(source), cwd=self.cwd, end=True)
        return pipeline.finalize(self.cfg, self.man, self.man.get("claude", sid), summarizer, consolidator)

    def hold(self, sid, summarizer, consolidator=accepts_nothing):
        self.capture(sid, summarizer, consolidator)
        pipeline.consolidate_pending(self.cfg, self.man, summarizer, consolidator)
        row = self.man.capture("claude", sid, 1)
        self.assertIsNotNone(row["retry_hold"])

    def page(self):
        path = self.cfg.projects_dir / "Demo App" / "Demo App.md"
        return status.live_page(notes.get_block(path.read_text(encoding="utf-8"), notes.STATE_BEGIN, notes.STATE_END))

    def cited(self, section, text):
        return any(text in item.text and "#e" in item.ref for item in self.page()[section])

    def held_count(self):
        return self.man.db.execute("SELECT count(*) FROM captures WHERE consolidated=0 AND retry_hold IS NOT NULL").fetchone()[0]

    def test_promotable_held_next_step_becomes_cited(self):
        self.hold("cited-next", facts_only)
        result = pipeline.triage_held(self.cfg, self.man, accepts_nothing)
        self.assertEqual(1, result["promoted"])
        row = self.man.capture("claude", "cited-next", 1)
        self.assertEqual(1, row["consolidated"])
        self.assertIsNone(row["retry_hold"])
        self.assertTrue(self.cited("next", "deploy"))
        self.assertEqual({}, self.man.stats()["held"])
        self.assertEqual(0, self.man.stats()["unconsolidated"])

    def test_uncited_held_closes_missing_evidence(self):
        self.hold("uncited", uncited_summarize)
        result = pipeline.triage_held(self.cfg, self.man, accepts_nothing)
        self.assertEqual(1, result["by_reason"].get("missing-evidence"))
        row = self.man.capture("claude", "uncited", 1)
        self.assertEqual("missing-evidence", row["closed_reason"])
        self.assertEqual(1, row["consolidated"])
        self.assertEqual(0, self.held_count())

    def test_facts_only_held_promotes_cited_fact(self):
        self.hold("facts", facts_without_next)
        result = pipeline.triage_held(self.cfg, self.man, accepts_nothing)
        self.assertEqual(1, result["promoted"])
        self.assertTrue(self.cited("working_now", "tests pass"))
        row = self.man.capture("claude", "facts", 1)
        self.assertEqual(1, row["consolidated"])
        self.assertIsNone(row["closed_reason"])

    def test_unsupported_fact_held_needs_review(self):
        self.hold("facts", unsupported_fact)
        result = pipeline.triage_held(self.cfg, self.man, accepts_nothing)
        self.assertEqual(1, result["review"])
        self.assertEqual(1, result["by_reason"].get("needs-review"))
        row = self.man.capture("claude", "facts", 1)
        self.assertEqual("needs review", row["retry_hold"])
        self.assertIsNone(row["closed_reason"])
        self.assertEqual(1, self.man.stats()["held"].get("needs review"))
        self.assertEqual([], self.man.held(time.time() + 10_000))

    def test_trivial_held_closes(self):
        self.hold("trivial", citation_only)
        result = pipeline.triage_held(self.cfg, self.man, accepts_nothing)
        self.assertEqual(1, result["by_reason"].get("trivial"))
        self.assertEqual("trivial", self.man.capture("claude", "trivial", 1)["closed_reason"])

    def test_real_omission_retries_then_needs_review(self):
        summary = cited_omission("x")
        self.assertFalse(status.agent_process(summary["facts"][0]))
        self.hold("omission", cited_omission)
        first = pipeline.triage_held(self.cfg, self.man, accepts_nothing, now=time.time())
        self.assertEqual(1, first["retrying"])
        row = self.man.capture("claude", "omission", 1)
        self.assertEqual("model omission", row["retry_hold"])
        self.assertEqual(1, row["retry_attempts"])
        second = pipeline.triage_held(self.cfg, self.man, accepts_nothing, now=time.time() + 10_000)
        self.assertEqual(1, second["retrying"])
        third = pipeline.triage_held(self.cfg, self.man, accepts_nothing, now=time.time() + 20_000)
        self.assertEqual(1, third["review"])
        self.assertEqual(1, third["by_reason"].get("needs-review"))
        row = self.man.capture("claude", "omission", 1)
        self.assertEqual("needs review", row["retry_hold"])
        self.assertIsNone(row["closed_reason"])
        self.assertEqual(3, row["retry_attempts"])
        self.assertEqual(1, self.man.stats()["held"].get("needs review"))
        self.assertEqual([], self.man.held(time.time() + 100_000))
        fourth = pipeline.triage_held(self.cfg, self.man, accepts_nothing, now=time.time() + 200_000)
        self.assertEqual(0, fourth["review"])
        self.assertEqual(0, fourth["retrying"])

    def test_temporary_outage_does_not_close(self):
        self.hold("outage", facts_only)
        for index in range(3):
            result = pipeline.triage_held(self.cfg, self.man, fx.failing_llm, now=time.time() + index * 10_000)
            self.assertEqual(1, result["retrying"])
        row = self.man.capture("claude", "outage", 1)
        self.assertIsNone(row["closed_reason"])
        self.assertEqual(0, row["retry_attempts"])
        recovered = pipeline.triage_held(self.cfg, self.man, accepts_nothing, now=time.time() + 100_000)
        self.assertEqual(1, recovered["promoted"])
        self.assertTrue(self.cited("working_now", "tests pass"))

    def test_read_error_is_transient_not_invalid(self):
        self.hold("read-error", facts_only)
        with patch.object(Path, "read_bytes", side_effect=OSError("synthetic read error")):
            result = pipeline.triage_held(self.cfg, self.man, accepts_nothing)
        self.assertEqual(1, result["retrying"])
        row = self.man.capture("claude", "read-error", 1)
        self.assertIsNone(row["closed_reason"])
        self.assertIn(row["retry_hold"], {"transient-error", "cleanup-pending"})

    def test_cleanup_failure_keeps_row_open_then_converges(self):
        self.hold("cleanup", uncited_summarize)
        with patch.object(notes, "update_project_note", side_effect=OSError("synthetic write error")):
            result = pipeline.triage_held(self.cfg, self.man, accepts_nothing)
        self.assertEqual(1, result["retrying"])
        row = self.man.capture("claude", "cleanup", 1)
        self.assertIsNone(row["closed_reason"])
        self.assertEqual("cleanup-pending", row["retry_hold"])
        again = pipeline.triage_held(self.cfg, self.man, accepts_nothing, now=time.time() + 10_000)
        self.assertEqual(1, again["by_reason"].get("missing-evidence"))
        self.assertEqual("missing-evidence", self.man.capture("claude", "cleanup", 1)["closed_reason"])

    def test_lock_timeout_during_cleanup_is_transient(self):
        self.hold("lock", uncited_summarize)
        with patch.object(pipeline, "file_lock", side_effect=LockTimeout("synthetic lock timeout")):
            result = pipeline.triage_held(self.cfg, self.man, accepts_nothing)
        self.assertEqual(1, result["retrying"])
        row = self.man.capture("claude", "lock", 1)
        self.assertIsNone(row["closed_reason"])
        self.assertIn(row["retry_hold"], {"transient-error", "cleanup-pending"})

    def test_promotion_records_cost(self):
        self.hold("cost", facts_only)
        before = self.man.capture("claude", "cost", 1)["cost_usd"]
        pipeline.triage_held(self.cfg, self.man, cost_consolidate)
        after = self.man.capture("claude", "cost", 1)["cost_usd"]
        self.assertAlmostEqual(before + 1.25, after)

    def test_promotion_is_idempotent(self):
        self.hold("repeat", facts_only)
        pipeline.triage_held(self.cfg, self.man, accepts_nothing)
        before = hashlib.sha256((self.cfg.projects_dir / "Demo App" / "Demo App.md").read_bytes()).hexdigest()
        stats = self.man.stats()
        again = pipeline.triage_held(self.cfg, self.man, accepts_nothing)
        self.assertEqual({"promoted": 0, "closed": 0, "retrying": 0, "review": 0, "by_reason": {}}, again)
        self.assertEqual(before, hashlib.sha256((self.cfg.projects_dir / "Demo App" / "Demo App.md").read_bytes()).hexdigest())
        self.assertEqual(stats, self.man.stats())