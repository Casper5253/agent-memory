import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentmem import adapters, extract  # noqa: E402


class GitSubstantiveGateTests(unittest.TestCase):
    def test_shell_only_session_commit_counts_as_substantive_work(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".audit") as temp:
            repo = Path(temp) / "p4-shell-only"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            env = dict(os.environ, GIT_AUTHOR_NAME="Synthetic", GIT_AUTHOR_EMAIL="synthetic@example.invalid",
                       GIT_COMMITTER_NAME="Synthetic", GIT_COMMITTER_EMAIL="synthetic@example.invalid")
            start = time.time() - 30
            for name in ("decision.txt", "status.txt", "next.txt"):
                (repo / name).write_text(name, encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=env)
            stamp = datetime.fromtimestamp(start + 15, timezone.utc).isoformat()
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=Synthetic", "-c",
                            "user.email=synthetic@example.invalid", "commit", "-q", "-m", "P4 shell-only decision",
                            "--date", stamp], check=True, env=dict(env, GIT_COMMITTER_DATE=stamp))
            transcript = adapters.Transcript(agent="codex", session_id="p4-shell-only-session", source="synthetic",
                cwd=str(repo), started=datetime.fromtimestamp(start, timezone.utc).isoformat(),
                ended=datetime.fromtimestamp(start + 25, timezone.utc).isoformat(), events=[])
            digest = extract.build(transcript, 2, 1500, 45000)
            self.assertTrue(digest.substantial, digest.gate_reason)
            self.assertCountEqual(["decision.txt", "status.txt", "next.txt"], digest.files)


if __name__ == "__main__":
    unittest.main()
