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

ENV = dict(os.environ, GIT_AUTHOR_NAME="Synthetic", GIT_AUTHOR_EMAIL="synthetic@example.invalid",
           GIT_COMMITTER_NAME="Synthetic", GIT_COMMITTER_EMAIL="synthetic@example.invalid")


def iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def git(repo, *args, when=None):
    env = ENV if when is None else dict(ENV, GIT_AUTHOR_DATE=iso(when), GIT_COMMITTER_DATE=iso(when))
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def write_at(path, stamp):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("synthetic\n", encoding="utf-8")
    os.utime(path, (stamp, stamp))


def transcript(cwd, begin, events):
    return adapters.Transcript("codex", "synthetic", "synthetic", cwd=str(cwd), started=iso(begin),
                               ended=iso(begin + 60), events=events)


class GitGateTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.begin = time.time() - 120

    def tearDown(self):
        self._temp.cleanup()

    def test_dirty_files_in_a_repository_without_commits_are_collected(self):
        repo = Path(self._temp.name) / "unborn"
        repo.mkdir()
        git(repo, "init", "-q")
        for name in ("one.py", "two.py", "three.py"):
            write_at(repo / name, self.begin + 20)
        digest = extract.build(transcript(repo, self.begin, [
            adapters.Event("user", "text", "Implement the three files"),
            adapters.Event("assistant", "command", "write three files")]), 2, 1500, 45000)
        self.assertTrue(digest.substantial, digest.gate_reason)
        self.assertEqual(["one.py", "three.py", "two.py"], digest.files)

    def test_commit_from_a_sibling_project_does_not_pass_the_gate(self):
        repo = Path(self._temp.name) / "siblings"
        repo.mkdir()
        git(repo, "init", "-q")
        write_at(repo / "other.py", self.begin + 20)
        git(repo, "add", "--all")
        git(repo, "commit", "-q", "-m", "Other session work", when=self.begin + 30)
        child = repo / "project-a"
        child.mkdir()
        chat = [adapters.Event("user", "text", "What time is it?"), adapters.Event("assistant", "text", "Noon.")]
        digest = extract.build(transcript(child, self.begin, chat), 2, 1500, 45000)
        self.assertFalse(digest.substantial, digest.gate_reason)
        self.assertEqual([], digest.files)

        write_at(child / "one.py", self.begin + 30)
        git(child, "add", "--all")
        git(child, "commit", "-q", "-m", "Project A work", when=self.begin + 35)
        digest = extract.build(transcript(child, self.begin, chat), 2, 1500, 45000)
        self.assertTrue(digest.substantial, digest.gate_reason)
        self.assertEqual(["one.py"], digest.files)

    def test_dirty_files_below_the_repository_root_resolve_from_the_toplevel(self):
        repo = Path(self._temp.name) / "subdirectory"
        repo.mkdir()
        git(repo, "init", "-q")
        write_at(repo / "root.txt", self.begin - 600)
        git(repo, "add", "--all")
        git(repo, "commit", "-q", "-m", "Earlier work", when=self.begin - 600)
        child = repo / "project-a"
        for name in ("one.py", "two.py", "three.py"):
            write_at(child / name, self.begin + 40)
        digest = extract.build(transcript(child, self.begin, [
            adapters.Event("user", "text", "Implement the three files"),
            adapters.Event("assistant", "command", "write three files")]), 2, 1500, 45000)
        self.assertTrue(digest.substantial, digest.gate_reason)
        self.assertEqual(["one.py", "three.py", "two.py"], digest.files)


if __name__ == "__main__":
    unittest.main()
