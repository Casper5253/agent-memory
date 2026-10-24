import unittest
import sys
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentmem import llm, notes, status


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
        output = dict(sections={**status.empty(), "latest": [{"text": "Uncited", "ref": "[[wrong#e1]]"}]}, displaced=[])
        summary = {"citations": [{"claim": "Changed status.", "quote": "Changed status."}]}
        with patch.object(llm, "_run", return_value=output):
            result, _ = llm.consolidate(notes.EMPTY_STATE, summary, "[[session-a]]", "2026-10-23")
        self.assertEqual(result.sections["latest"], [])
        self.assertIn(("dropped", "invalid new item"), result.events)

    def test_existing_facts_are_preserved_and_new_status_links_to_evidence(self):
        old = status.empty()
        old["working_now"] = [status.Item("i1", "Existing endpoint remains supported.", "[[old#e1]]")]
        output = dict(sections={**status.empty(), "working_now": [
            {"keep": "i1"}, {"text": "Added endpoint support.", "ref": "[[session-b#e1]]"}]}, displaced=[])
        summary = {"citations": [{"claim": "Added endpoint support.", "quote": "Added endpoint support."}]}
        with patch.object(llm, "_run", return_value=output):
            result, _ = llm.consolidate(status.render(old), summary, "[[session-b]]", "2026-10-23")
        self.assertEqual(result.sections["working_now"][0], old["working_now"][0])
        self.assertEqual(result.sections["working_now"][1].ref, "[[session-b#e1]]")

    def test_oversized_new_item_is_dropped_without_retry(self):
        output = dict(sections={**status.empty(), "latest": [{"text": "x" * 9000, "ref": "[[session-a#e1]]"}]}, displaced=[])
        summary = {"citations": [{"claim": "x", "quote": "x"}]}
        with patch.object(llm, "_run", return_value=output) as invoke:
            result, _ = llm.consolidate(notes.EMPTY_STATE, summary, "[[session-a]]", "2026-10-23")
        self.assertEqual(result.sections["latest"], [])
        self.assertEqual(invoke.call_count, 1)

    def test_large_existing_page_preserved_by_item_caps_without_shorten(self):
        old = status.empty()
        for n, key in enumerate(status.SECTIONS):
            old[key] = [status.Item(f"i{n + 1}", "old detail " * 899 + "end", "[[old#e1]]")]
        with patch.object(llm, "_run", return_value=dict(sections=status.empty(), displaced=[])) as invoke:
            result, _ = llm.consolidate(status.render(old), {}, "[[session-a]]", "2026-10-23")
        self.assertEqual(result.sections, old)
        self.assertEqual(invoke.call_count, 1)


if __name__ == "__main__":
    unittest.main()
