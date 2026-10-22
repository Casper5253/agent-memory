import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "proposals"


class ProposalFixtureTests(unittest.TestCase):
    def test_generator_check_mode(self):
        result = subprocess.run(
            [sys.executable, "-I", str(ROOT / "tools" / "generate_proposal_fixtures.py"), "--check"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_index_lists_every_fixture_and_classifies_errors(self):
        index = json.loads((FIXTURE_DIR / "cases.json").read_text(encoding="utf-8"))
        listed = {case["file"] for case in index}
        fixture_files = {path.name for path in FIXTURE_DIR.iterdir() if path.name != "cases.json"}

        self.assertEqual(listed, fixture_files)
        for case in index:
            self.assertIn(case["expect"], {"valid", "error"})
            if case["expect"] == "error":
                self.assertTrue(case["error_code"])


if __name__ == "__main__":
    unittest.main()
