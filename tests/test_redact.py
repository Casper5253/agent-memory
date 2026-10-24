import unittest

from agentmem import redact as redact_module

MASK = "[REDACTED]"
redact = redact_module.redact


class PersonalDataRedactionTests(unittest.TestCase):
    def test_checksum_valid_personal_data_is_redacted(self):
        samples = {
            "iban": "GB82 WEST 1234 5698 7654 32",
            "payment_card": "4111 1111 1111 1111",
            "bsn": "BSN 111222333",
            "phone": "+31 6 12345678",
            "email": "private.person@example.com",
        }
        for category, value in samples.items():
            with self.subTest(category=category):
                self.assertEqual(redact(value), f"BSN {MASK}" if category == "bsn" else MASK)
                self.assertEqual([item.category for item in redact_module.findings(value)], [category])

    def test_checksum_failures_and_identifier_lookalikes_are_preserved(self):
        samples = [
            "GB82 WEST 1234 5698 7654 33",
            "4111 1111 1111 1112",
            "111222334",
            "+000 123456789",
            "git commit 3f2a9c1e5b7d9f1a3c5e7b9d1f3a5c7e9b1d3f5a",
            "source id 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "[[0-Sessions/2026/10/2026-10-23-0900-claude-switch-storage-2d3058d9b1d7bd8206c65fc33a5542c7186e4b4e16c712a18f6745e1df6475c8|Switch storage]]",
            "session 01a13a34-04b6-75c0-8998-21d7db60ac4b",
            "timestamp 20261023123456",
            "version 2026.10.23.1234",
        ]
        for value in samples:
            with self.subTest(value=value):
                self.assertEqual(redact(value), value)

    def test_luhn_valid_numbers_without_an_issuer_prefix_are_preserved(self):
        samples = [
            "1792799155000000",
            "order=1792799155000000:1",
            "- [ ] 2026-10-24 status update pending (base=abc; order=1792799155000000:1)",
            "agentmem_state_order: 1792799155000000:1",
        ]
        for value in samples:
            with self.subTest(value=value):
                self.assertEqual(redact(value), value)

    def test_luhn_valid_issuer_numbers_are_masked(self):
        samples = ["5555 5555 5555 4444", "3782 822463 10005", "6011 1111 1111 1117"]
        for value in samples:
            with self.subTest(value=value):
                self.assertEqual(redact(value), MASK)

    def test_email_allowlist_is_case_insensitive(self):
        value = "Owner@Example.com"
        self.assertEqual(redact(value, email_allowlist=["owner@example.com"]), value)
        self.assertEqual(redact(value), MASK)

    def test_masked_secret_assignment_is_not_a_finding_or_rewritten(self):
        value = "api_key: [REDACTED]"
        self.assertEqual(redact_module.findings(value), [])
        self.assertEqual(redact(value), value)

    def test_frontmatter_session_identity_is_preserved_but_cookie_session_is_redacted(self):
        value = ("---\nsession_id: 0123456789abcdef0123456789abcdef\n"
                 "claude_session_id: aZ7qN4vT2mB8xK5pW9cD3rF6hJ1sL0yQ\n---\nbody\n")
        self.assertEqual(redact_module.findings(value), [])
        self.assertEqual(redact_module.redact_session_note(value), value)
        cookie = "Cookie: session_id=0123456789abcdef0123456789abcdef"
        self.assertNotIn("0123456789abcdef0123456789abcdef", redact(cookie))
        self.assertEqual(redact_module.findings(cookie)[0].category, "secret")

    def test_package_versions_and_decorator_examples_are_not_emails(self):
        value = "npm create vite@6.5.0; @playwright/test@1.64.1; typescript@7.0.2; \"@router.get(\""
        self.assertEqual(redact_module.findings(value), [])
        self.assertEqual(redact(value), value)
        self.assertEqual(redact("email user@example.com"), "email [REDACTED]")

    def test_no_reply_service_addresses_are_not_personal(self):
        value = "Co-Authored-By: Claude <noreply@anthropic.com>; 123+bot@users.noreply.github.com"
        self.assertEqual(redact(value), "Co-Authored-By: Claude <noreply@anthropic.com>; 123+bot@users.noreply.github.com")
        self.assertEqual(redact("mail jan.noreplyfan@example.com"), "mail [REDACTED]")

    def test_bsn_requires_nearby_keyword_and_strict_identifier_boundaries(self):
        self.assertEqual(redact("BSN 111222333"), "BSN [REDACTED]")
        for value in ("111222333", "line-111222333", "id.111222333", "x111222333"):
            with self.subTest(value=value):
                self.assertEqual(redact_module.findings(value), [])
                self.assertEqual(redact(value), value)
        too_far = "BSN" + (" " * 31) + "111222333"
        self.assertEqual(redact_module.findings(too_far), [])

    def test_iban_requires_known_country_length_and_checksum(self):
        self.assertEqual(redact("NL91 ABNA 0417 1643 00"), "[REDACTED]")
        for value in ("NL91ABNA0417164300EXTRA", "ZZ82 WEST 1234 5698 7654 32"):
            with self.subTest(value=value):
                self.assertEqual(redact_module.findings(value), [])
                self.assertEqual(redact(value), value)


if __name__ == "__main__":
    unittest.main()
