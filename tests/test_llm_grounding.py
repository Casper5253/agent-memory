import unittest
import sys
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentmem import llm


class SummaryGroundingTests(unittest.TestCase):
    def test_false_runtime_cwd_conflict_without_digest_citation_is_dropped(self):
        digest = "Added a parser guard. The parser.py change passed the failing case."
        model_output = {
            "title": "Parser guard",
            "summary": (
                "Added a parser guard. The project cwd conflicts with "
                "C:\\Users\\caspe\\.agents\\memory\\runtime\\tmp."
            ),
            "what_happened": ["Added a parser guard."],
            "decisions": [],
            "facts": ["The parser guard is in parser.py."],
            "open_questions": [],
            "next_steps": [],
            "citations": [
                {"section": "summary", "claim": "Added a parser guard.",
                 "quote": "Added a parser guard. The parser.py change passed the failing case."},
                {"section": "what_happened", "claim": "Added a parser guard.",
                 "quote": "Added a parser guard. The parser.py change passed the failing case."},
                {"section": "facts", "claim": "The parser guard is in parser.py.",
                 "quote": "Added a parser guard. The parser.py change passed the failing case."},
            ],
        }
        with patch.object(llm, "_run", return_value=model_output):
            summary = llm.summarize(digest)

        self.assertEqual(summary["summary"], "Added a parser guard.")
        self.assertNotIn("runtime\\tmp", summary["summary"].lower())
        self.assertEqual(summary["what_happened"], ["Added a parser guard."])

    def test_model_runs_in_neutral_temporary_directory(self):
        seen = {}

        def run(*args, **kwargs):
            seen.update(kwargs)
            seen["argv"] = args[0]
            return SimpleNamespace(returncode=0, stdout=b'{"result":"{}"}', stderr=b"")

        with patch.object(llm.shutil, "which", return_value="claude.exe"), patch.object(llm.subprocess, "run", side_effect=run):
            llm._run(llm.SUMMARY_SYSTEM, "digest", "haiku", 10)

        self.assertTrue(Path(seen["cwd"]).name.startswith("agentmem-llm-"))
        self.assertNotIn("runtime\\tmp", seen["cwd"].lower())
        prompt = seen["argv"][seen["argv"].index("--system-prompt") + 1]
        self.assertIn("own working directory, environment", prompt)
        self.assertIn("not evidence", prompt)

    def test_new_status_bullet_must_link_to_summary_evidence(self):
        state = "\n".join([
            "### Current state", "- New project status", "", "### Active decisions", "- none",
            "", "### Superseded decisions", "- none", "", "### Open questions and next steps", "- none",
        ])
        summary = {"summary": "Session changed status.", "citations": [
            {"section": "summary", "claim": "Session changed status.", "quote": "Changed status."},
        ]}
        with patch.object(llm, "_run", return_value={"state_markdown": state}):
            with self.assertRaisesRegex(llm.LLMError, "new status statement lacks a matching digest claim citation"):
                llm.consolidate("", summary, "[[0-Sessions/session-a]]", "2026-10-23")

    def test_existing_facts_are_preserved_and_new_status_links_to_evidence(self):
        old_fact = "- Existing endpoint remains supported."
        output = "\n".join([
            "### Current state", old_fact + "\n- Added endpoint support. [[0-Sessions/session-b#e1]]", "",
            "### Active decisions", "- none", "", "### Superseded decisions", "- none", "",
            "### Open questions and next steps", "- none",
        ])
        summary = {"summary": "Added endpoint support.", "citations": [
            {"section": "summary", "claim": "Added endpoint support.", "quote": "Added endpoint support."},
        ]}
        with patch.object(llm, "_run", return_value={"state_markdown": output}) as invoke:
            state, _ = llm.consolidate(old_fact, summary, "[[0-Sessions/session-b]]", "2026-10-23")

        self.assertIn(old_fact, state)
        self.assertIn("[[0-Sessions/session-b#e1]]", state)
        self.assertIn("Keep every prior fact and decision unless the cited session evidence contradicts it.", invoke.call_args.args[0])

    def test_oversized_consolidation_retries_once_with_shorten_instruction(self):
        oversized = "\n".join([
            "### Current state", "- " + ("old detail " * 900), "", "### Active decisions", "- none", "",
            "### Superseded decisions", "- none", "", "### Open questions and next steps", "- none",
        ])
        shortened = "\n".join([
            "### Current state", "- none", "", "### Active decisions", "- none", "",
            "### Superseded decisions", "- none", "", "### Open questions and next steps", "- none",
        ])
        with patch.object(llm, "_run", side_effect=[{"state_markdown": oversized}, {"state_markdown": shortened}]) as invoke:
            state, _ = llm.consolidate("", {}, "[[0-Sessions/session-a]]", "2026-10-23")

        self.assertEqual(state, shortened)
        self.assertEqual(invoke.call_count, 2)
        self.assertIn("Shorten it to 8000 characters or fewer", invoke.call_args.args[1])

    def test_second_oversized_consolidation_fails_after_one_retry(self):
        oversized = "\n".join([
            "### Current state", "- " + ("old detail " * 900), "", "### Active decisions", "- none", "",
            "### Superseded decisions", "- none", "", "### Open questions and next steps", "- none",
        ])
        with patch.object(llm, "_run", side_effect=[{"state_markdown": oversized}, {"state_markdown": oversized}]) as invoke:
            with self.assertRaisesRegex(llm.LLMError, "consolidation output too long"):
                llm.consolidate("", {}, "[[0-Sessions/session-a]]", "2026-10-23")

        self.assertEqual(invoke.call_count, 2)


if __name__ == "__main__":
    unittest.main()
