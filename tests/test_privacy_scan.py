import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("vault_sweep_privacy", ROOT / "tools" / "vault_sweep.py")
vault_sweep = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(vault_sweep)


class PrivacyScanTests(unittest.TestCase):
    def test_privacy_command_writes_only_category_path_line_and_totals(self):
        scratch = ROOT / ".audit" / "tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            root = Path(directory)
            vault = root / "vault"
            (vault / "1-Projects" / "Demo").mkdir(parents=True)
            note = vault / "1-Projects" / "Demo" / "Demo.md"
            iban = "GB82 WEST 1234 5698 7654 32"
            email = "private.person@example.com"
            note.write_text(f"Account {iban}\nContact {email}\n", encoding="utf-8")
            out = root / "privacy.json"
            old_argv = sys.argv
            stdout = io.StringIO()
            try:
                sys.argv = ["vault_sweep", "privacy", "--vault", str(vault), "--out", str(out)]
                with contextlib.redirect_stdout(stdout):
                    self.assertEqual(vault_sweep.main(), 0)
            finally:
                sys.argv = old_argv
            report_text = out.read_text(encoding="utf-8")
            report = json.loads(report_text)
            self.assertEqual(stdout.getvalue(), "")
            self.assertNotIn(iban, report_text)
            self.assertNotIn(email, report_text)
            self.assertEqual(report["findings"], [
                {"category": "iban", "path": "1-Projects/Demo/Demo.md", "line": 1},
                {"category": "email", "path": "1-Projects/Demo/Demo.md", "line": 2},
            ])
            self.assertEqual(report["totals_by_category"]["iban"], 1)
            self.assertEqual(report["totals_by_category"]["email"], 1)
            self.assertEqual(set(vault.rglob("*")), {vault / "1-Projects", vault / "1-Projects" / "Demo", note})


if __name__ == "__main__":
    unittest.main()
