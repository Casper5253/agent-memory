import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentmem import llm
from agentmem.adapters import Transcript
from agentmem.extract import Digest, _render


class CommandsLabelTests(unittest.TestCase):
    def render(self, count):
        digest = Digest(commands=[f"command {i}" for i in range(count)])
        return _render(Transcript(agent="claude", session_id="s", source="x"), digest, cap=100000)

    def test_uncut_commands_have_plain_label(self):
        text = self.render(3)
        self.assertIn("COMMANDS\n", text)
        self.assertNotIn("first", text)

    def test_truncated_commands_report_shown_and_total(self):
        text = self.render(45)
        self.assertIn("COMMANDS (first 40 of 45)", text)
        self.assertIn("- command 39", text)
        self.assertNotIn("- command 40", text)


class SummaryListTests(unittest.TestCase):
    # Exact shape of the summary array in raw/912297cd46a846fa94d369d84cbed3dc-model.json (P4-after2, 07).
    RAW_SUMMARY = [
        "The session set storage.json to backend sqlite with reason atomic updates, and wrote decision.txt and open-work.txt.",
        "The three files were read back after writing.",
        "The harness will commit the three files after the turn.",
    ]

    def test_list_summary_is_joined_as_sentences(self):
        self.assertEqual(llm._summary_text(["First fact", "Second fact."]), "First fact. Second fact.")

    def test_list_summary_keeps_terminal_punctuation(self):
        self.assertEqual(llm._summary_text(["Did it work?", "Yes!"]), "Did it work? Yes!")

    def test_non_text_summary_is_an_error(self):
        for value in ({"text": "x"}, [1, 2], 3):
            with self.subTest(value=value), self.assertRaises(llm.LLMError):
                llm._summary_text(value)

    def test_raw_list_summary_is_stored_as_prose(self):
        digest = "\n".join(self.RAW_SUMMARY)
        model_output = {
            "title": "Storage backend",
            "summary": self.RAW_SUMMARY,
            "what_happened": [],
            "decisions": [],
            "facts": [],
            "open_questions": [],
            "next_steps": [],
            "citations": [{"section": "summary", "claim": s, "quote": s} for s in self.RAW_SUMMARY],
        }
        with patch.object(llm, "_run", return_value=model_output):
            summary = llm.summarize(digest)

        self.assertEqual(summary["summary"], " ".join(self.RAW_SUMMARY))
        self.assertNotIn("[", summary["summary"])
        self.assertNotIn("', '", summary["summary"])


class ConsolidatorNextStepTests(unittest.TestCase):
    def test_prompt_replaces_old_next_step_with_new_one(self):
        self.assertIn("next step or open item about the same work", llm.CONSOLIDATE_SYSTEM)
        self.assertIn('displace the old id as "superseded"', llm.CONSOLIDATE_SYSTEM)

    def test_existing_replacement_rules_are_kept(self):
        self.assertIn('{"keep": "iN"}', llm.CONSOLIDATE_SYSTEM)
        self.assertIn("Never silently omit an item.", llm.CONSOLIDATE_SYSTEM)


if __name__ == "__main__":
    unittest.main()
