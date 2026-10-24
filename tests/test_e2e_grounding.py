import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentmem import grounding, llm, notes, status


DIGEST = "Added a parser guard. The parser.py change passed the failing case."


class GroundingRuleTests(unittest.TestCase):
    def grounded(self, claim, quote, digest=DIGEST):
        data = {"citations": [{"section": "summary", "claim": claim, "quote": quote}]}
        citations = llm._grounded_statements(data, digest)
        return bool(citations)

    def test_paraphrased_claim_with_matching_quote_is_accepted(self):
        digest = "The team moved the session cache to Redis after the outage."
        self.assertTrue(self.grounded("Moved sessions to Redis after the outage.",
                                      "The team moved the session cache to Redis after the outage.", digest))

    def test_claim_naming_path_absent_from_digest_is_rejected(self):
        claim = "The parser guard lives at C:\\Users\\caspe\\.agents\\memory\\runtime\\tmp."
        self.assertFalse(self.grounded(claim, "Added a parser guard."))

    def test_quote_not_in_digest_is_rejected(self):
        self.assertFalse(self.grounded("Added a parser guard.", "Added a parser guard and a cache."))

    def test_unrelated_quote_without_shared_content_words_is_rejected(self):
        self.assertFalse(self.grounded("Chose Postgres for storage.", "Lunch was a sandwich."))

    def test_status_item_paraphrasing_its_claim_is_accepted(self):
        evidence = ("Moved sessions to Redis after the outage. "
                    "The team moved the session cache to Redis after the outage.")
        old = status.empty()
        output = {**status.empty(), "working_now": [{"text": "Session storage moved to Redis after the outage.",
                                                      "ref": "[[new#e1]]"}]}
        result = status.reconcile(old, dict(sections=output, displaced=[]), {"[[new#e1]]": evidence})
        self.assertEqual([item.text for item in result.sections["working_now"]],
                         ["Session storage moved to Redis after the outage."])

    def test_status_item_adding_absent_file_name_is_dropped(self):
        evidence = ("Moved sessions to Redis after the outage. "
                    "The team moved the session cache to Redis after the outage.")
        old = status.empty()
        output = {**status.empty(), "working_now": [{"text": "Moved sessions to Redis in redis_session.py after the outage.",
                                                      "ref": "[[new#e1]]"}]}
        result = status.reconcile(old, dict(sections=output, displaced=[]), {"[[new#e1]]": evidence})
        self.assertEqual(result.sections["working_now"], [])
        self.assertIn(("dropped", "invalid new item"), result.events)

    def test_consolidate_accepts_paraphrased_status_item_from_cited_summary(self):
        summary = {"citations": [{"section": "summary", "claim": "Moved sessions to Redis after the outage.",
                                  "quote": "The team moved the session cache to Redis after the outage."}]}
        output = dict(sections={**status.empty(), "latest": [
            {"text": "Session cache now runs on Redis after the outage.", "ref": "[[s#e1]]"}]}, displaced=[])
        with patch.object(llm, "_run", return_value=output):
            result, _ = llm.consolidate(notes.EMPTY_STATE, summary, "[[s]]", "2026-10-24")
        self.assertEqual([item.text for item in result.sections["latest"]],
                         ["Session cache now runs on Redis after the outage."])


class SpecificsAndStemTests(unittest.TestCase):
    def test_ss_words_stem_consistently_so_passing_matches_pass(self):
        self.assertTrue(grounding.grounded("12 passing tests", "all 12 tests pass", "all 12 tests pass"))

    def test_path_at_sentence_end_matches_digest_without_the_period(self):
        digest = r"Runs in C:\Users\caspe\demo-app now."
        self.assertTrue(grounding.grounded(r"Runs in C:\Users\caspe\demo-app.", r"Runs in C:\Users\caspe\demo-app", digest))

    def test_absent_path_at_sentence_end_is_still_rejected(self):
        digest = r"Runs in C:\Users\caspe\demo-app now."
        self.assertFalse(grounding.grounded(r"Runs in C:\Users\caspe\other-app.", r"Runs in C:\Users\caspe\demo-app", digest))


class SentenceSupportTests(unittest.TestCase):
    DIGEST = "Set up SQLite storage for the demo app. Created db.py and wired migrations."

    def summarize(self, summary, citations, what_happened=()):
        output = {"title": "Demo storage", "summary": summary, "what_happened": list(what_happened),
                  "decisions": [], "facts": [], "open_questions": [], "next_steps": [], "citations": citations}
        with patch.object(llm, "_run", return_value=output):
            return llm.summarize(self.DIGEST)

    def test_one_claim_covering_the_whole_two_sentence_summary_is_kept(self):
        summary = "Set up SQLite storage for the demo app. Created db.py and wired migrations."
        result = self.summarize(summary, [{"section": "summary", "claim": summary, "quote": self.DIGEST}])
        self.assertEqual(result["summary"], summary)

    def test_claim_without_final_period_supports_sentence(self):
        result = self.summarize("Set up SQLite storage for the demo app.",
                                [{"section": "summary", "claim": "Set up SQLite storage for the demo app",
                                  "quote": self.DIGEST}])
        self.assertEqual(result["summary"], "Set up SQLite storage for the demo app.")

    def test_list_item_matches_claim_ignoring_case_and_trailing_punctuation(self):
        result = self.summarize("Set up SQLite storage for the demo app.",
                                [{"section": "summary", "claim": "Set up SQLite storage for the demo app.",
                                  "quote": self.DIGEST},
                                 {"section": "what_happened", "claim": "SET UP SQLITE STORAGE FOR THE DEMO APP!",
                                  "quote": self.DIGEST}],
                                what_happened=["Set up SQLite storage for the demo app"])
        self.assertEqual(result["what_happened"], ["Set up SQLite storage for the demo app"])


if __name__ == "__main__":
    unittest.main()
