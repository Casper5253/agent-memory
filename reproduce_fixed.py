"""Adversarial reproduction runner for WP-03 fixes.

Runs the safety and poisoning tests that must hold after the WP-03 changes.
Exit code equals the number of adversarial failures; output is one JSON object.
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def run(pattern: str) -> int:
    result = subprocess.run(
        [sys.executable, "-B", "-I", "-m", "unittest", "discover", "-s", "tests", "-p", pattern, "-v"],
        cwd=ROOT, capture_output=True, text=True)
    return result.returncode


def main() -> int:
    patterns = [
        "test_knowledge_sources.py",
        "test_knowledge_proposals.py",
        "test_knowledge_store.py",
        "test_knowledge_capture_recovery.py",
    ]
    failures = 0
    for pattern in patterns:
        rc = run(pattern)
        if rc:
            failures += rc
    report = {"adversarial_failures": failures, "total_failures": failures, "patterns": patterns}
    print(json.dumps(report, indent=2))
    return min(failures, 255)


if __name__ == "__main__":
    sys.exit(main())
