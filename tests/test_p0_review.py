import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agentmem import llm, notes, pipeline, projects
from agentmem.config import Config


def state(*lines):
    return "\n".join(["### Current state", *lines, "### Active decisions", "- none",
                     "### Superseded decisions", "- none", "### Open questions and next steps", "- none"])


class PendingReconciliationReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name)
        (self.vault / "1-Projects").mkdir()
        (self.vault / "_meta/locks").mkdir(parents=True)
        self.cfg = Config(self.vault)
        self.note = projects.note_path(self.vault, "Synthetic")
        notes.update_project_note(self.note, "Synthetic", "2026-10-23", state=state("- Seed backend JSON."))
        self.when = datetime(2026, 10, 23, 10, tzinfo=timezone.utc)

    def test_older_disjoint_pending_update_and_open_question_survive_newer_success(self):
        older = {"summary": "Use SQLite.", "facts": ["Keep offline support."],
                 "open_questions": ["CANARY_ROLLOUT_BLOCKER remains unresolved."],
                 "decisions": [{"decision": "Use SQLite.", "rationale": "Atomic updates."}]}

        def outage(*args, **kwargs):
            raise llm.LLMError("temporary outage")

        newer = lambda *args, **kwargs: (state("- Parser is faster."), 0)
        self.assertFalse(pipeline.consolidate_into_project(self.cfg, "Synthetic", older, "0-Sessions/A",
                                                            self.when, "claude-haiku", outage)[1])
        self.assertTrue(pipeline.consolidate_into_project(self.cfg, "Synthetic", {"summary": "Parser is faster."},
                                                           "0-Sessions/B", self.when + timedelta(seconds=1),
                                                           "claude-haiku", newer)[1])
        ok = pipeline.consolidate_into_project(self.cfg, "Synthetic", older, "0-Sessions/A", self.when,
                                               "claude-haiku", lambda *args, **kwargs: self.fail("must not reconcile"))
        text = self.note.read_text(encoding="utf-8")
        pending = notes.get_block(text, notes.PENDING_BEGIN, notes.PENDING_END)
        self.assertFalse(ok[1])
        self.assertIn("Keep offline support", pending)
        self.assertIn("CANARY_ROLLOUT_BLOCKER remains unresolved", pending)
        self.assertIn("Parser is faster", notes.get_block(text, notes.STATE_BEGIN, notes.STATE_END))

    def test_grounding_keeps_supported_decisions_whitespace_and_abbreviations(self):
        digest = "Parser guard added. Use SQLite. Atomic updates. Use SQLite\nfor atomic updates. " \
                 "Use e.g. SQLite for local mode."
        data = {
            "summary": "Parser guard added. Use e.g. SQLite for local mode.",
            "decisions": [{"decision": "Use SQLite.", "rationale": "Atomic updates."}],
            "facts": ["Use SQLite for atomic updates."],
            "citations": [
                {"section": "summary", "claim": "Parser guard added.", "quote": "Parser guard added."},
                {"section": "decisions", "claim": "Use SQLite.", "quote": "Use SQLite."},
                {"section": "decisions", "claim": "Atomic updates.", "quote": "Atomic updates."},
                {"section": "facts", "claim": "Use SQLite for atomic updates.",
                 "quote": "Use SQLite for atomic updates."},
                {"section": "summary", "claim": "Use e.g. SQLite for local mode.",
                 "quote": "Use e.g. SQLite for local mode."},
            ],
        }
        with patch.object(llm, "_run", return_value=data):
            result = llm.summarize(digest)

        self.assertEqual(result["decisions"], [{"decision": "Use SQLite.", "rationale": "Atomic updates."}])
        self.assertEqual(result["facts"], ["Use SQLite for atomic updates."])
        self.assertIn("Use e.g. SQLite for local mode.", result["summary"])
