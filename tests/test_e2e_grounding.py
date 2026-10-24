import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentmem import llm, notes, status


DIGEST = "Added a parser guard. The parser.py change passed the failing case."


class GroundingRuleTests(unittest.TestCase):
    def grounded(self, claim, quote, digest=DIGEST):
        data = {"citations": [{"section": "summary", "claim": claim, "quote": quote}]}
        _, citations = llm._grounded_statements(data, digest)
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


if __name__ == "__main__":
    unittest.main()
