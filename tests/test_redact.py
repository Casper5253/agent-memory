import unittest

from agentmem import redact as redact_module

MASK = "[REDACTED]"
redact = redact_module.redact


class PersonalDataRedactionTests(unittest.TestCase):
    def test_checksum_valid_personal_data_is_redacted(self):
        samples = {
            "iban": "GB82 WEST 1234 5698 7654 32",
            "payment_card": "4111 1111 1111 1111",
            "bsn": "111222333",
            "phone": "+31 6 12345678",
            "email": "private.person@example.com",
        }
        for category, value in samples.items():
            with self.subTest(category=category):
                self.assertEqual(redact(value), MASK)
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

    def test_email_allowlist_is_case_insensitive(self):
        value = "Owner@Example.com"
        self.assertEqual(redact(value, email_allowlist=["owner@example.com"]), value)
        self.assertEqual(redact(value), MASK)


if __name__ == "__main__":
    unittest.main()
