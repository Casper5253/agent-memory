"""Synthetic repository ownership regressions for attribution."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agentmem import projects


class FileOwnership(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / ".audit" / "a5-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / ".git").write_text("gitdir: unavailable", encoding="utf-8")
        self.home = self.root / "home"
        self.agent = self.home / ".claude"
        self.agent.mkdir(parents=True)
        self.parent = self.root / "real-project"
        self.parent.mkdir()
        self.map_path = self.root / "projects.json"
        self.data = {"projects": [
            {"name": "Real Project", "roots": [str(self.parent)]},
            {"name": "Agent Environment", "roots": [str(self.agent)]},
        ]}
        self.save()
        home = patch.object(projects, "HOME", self.home)
        home.start()
        self.addCleanup(home.stop)

    def save(self):
        self.map_path.write_text(json.dumps(self.data), encoding="utf-8")

    def git(self, directory, *args):
        return subprocess.run(["git", "-C", str(directory), "-c", "commit.gpgsign=false",
            "-c", "core.hooksPath=" + str(self.root / "no-hooks"), *args],
            capture_output=True, check=True, timeout=10)

    def worktree(self):
        self.git(self.parent, "init", "-q")
        self.git(self.parent, "-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.invalid",
                 "commit", "--allow-empty", "-qm", "Synthetic root")
        tree = self.agent / "worktrees" / "synthetic"
        tree.parent.mkdir(parents=True)
        self.git(self.parent, "worktree", "add", "--detach", str(tree))
        return tree

    def attribute(self, files):
        return projects.attribute(str(self.parent), self.map_path, files=files, create=False)

    def test_worktree_under_agent_home_credits_parent_project(self):
        tree = self.worktree()
        before = self.map_path.read_bytes()
        result = self.attribute([str(tree / "code.py")])
        self.assertEqual((result.project, result.method), ("Real Project", "content"))
        self.assertEqual(result.signals["Real Project"]["files"], 1)
        self.assertEqual(result.signals["Agent Environment"]["files"], 0)
        self.assertEqual(self.map_path.read_bytes(), before)

    def test_direct_agent_config_outside_repo_still_credits_environment(self):
        result = self.attribute([str(self.agent / "settings.json")])
        self.assertEqual(result.project, "Agent Environment")
        self.assertEqual(result.signals["Agent Environment"]["files"], 1)

    def test_nonrepository_nested_roots_credit_only_most_specific(self):
        nested = self.agent / "shared-tools"
        nested.mkdir()
        self.data["projects"].append({"name": "Shared Tools", "roots": [str(nested)]})
        self.save()
        result = self.attribute([str(nested / "helper.py")])
        self.assertEqual(result.project, "Shared Tools")
        self.assertEqual(result.signals["Shared Tools"]["files"], 1)
        self.assertEqual(result.signals["Agent Environment"]["files"], 0)

    def test_unmapped_repository_never_credits_containing_agent_root(self):
        unknown = self.agent / "unmapped-repository"
        unknown.mkdir()
        self.git(unknown, "init", "-q")
        result = self.attribute([str(unknown / "code.py")])
        self.assertEqual(result.signals["Agent Environment"]["files"], 0)
        self.assertEqual(result.method, "cwd-fallback")

    def test_unmapped_repository_credits_most_specific_project_inside_it(self):
        repository = self.root / "unmapped-repository"
        nested = repository / "workspace" / "nested-project"
        nested.mkdir(parents=True)
        self.git(repository, "init", "-q")
        self.data["projects"].extend([
            {"name": "Workspace", "roots": [str(nested.parent)]},
            {"name": "Nested Project", "roots": [str(nested)]},
        ])
        self.save()
        before = self.map_path.read_bytes()
        path = str(nested / "code.py")
        for create in (False, True):
            with self.subTest(create=create):
                result = projects.attribute(str(self.parent), self.map_path,
                    files=[path], text="Inspect " + path, create=create)
                self.assertEqual((result.project, result.method), ("Nested Project", "content"))
                self.assertEqual(result.signals["Nested Project"]["files"], 1)
                self.assertEqual(result.signals["Nested Project"]["paths"], 1)
                self.assertEqual(result.signals["Workspace"]["files"], 0)
                self.assertNotIn("Unmapped Repository", result.signals)
                self.assertEqual(self.map_path.read_bytes(), before)

        result = projects.attribute(str(self.parent), self.map_path,
            files=[path, str(repository / "outside.py")], create=False)
        self.assertEqual(result.signals["Nested Project"]["files"], 1)
        self.assertEqual(result.signals["Unmapped Repository"]["files"], 1)
        self.assertEqual(self.map_path.read_bytes(), before)

        self.data["projects"].append({"name": "Repository Owner", "roots": [str(repository)]})
        self.save()
        result = self.attribute([path])
        self.assertEqual(result.signals["Repository Owner"]["files"], 1)
        self.assertEqual(result.signals["Nested Project"]["files"], 0)

    def test_nested_repository_is_not_hidden_by_cached_outer_repository(self):
        self.git(self.parent, "init", "-q")
        nested = self.parent / "nested"
        nested.mkdir()
        self.git(nested, "init", "-q")
        self.data["projects"].append({"name": "Nested Repository", "roots": [str(nested)]})
        self.save()
        result = self.attribute([str(self.parent / "a.py"), str(nested / "b.py")])
        self.assertEqual(result.signals["Real Project"]["files"], 1)
        self.assertEqual(result.signals["Nested Repository"]["files"], 1)

    def test_many_files_share_one_repository_resolution(self):
        tree = self.worktree()
        with patch.object(projects,"repository_root",wraps=projects.repository_root) as resolve:
            result = self.attribute([str(tree / (str(i) + ".py")) for i in range(80)])
        self.assertEqual(result.signals["Real Project"]["files"], 10)
        self.assertEqual(result.signals["Agent Environment"]["files"], 0)
        self.assertLessEqual(resolve.call_count, 3)

    def test_worker_brief_config_paths_cannot_outvote_project_edits(self):
        self.git(self.parent, "init", "-q")
        paths = [str(self.agent / ("skill-" + str(i)) / "SKILL.md") for i in range(10)]
        text = "Follow the worker brief:\n" + "\n".join(paths + paths)
        result = projects.attribute(str(self.parent),self.map_path,
            files=[str(self.parent / (str(i) + ".py")) for i in range(3)],text=text,create=False)
        self.assertEqual(result.project,"Real Project")
        self.assertEqual(result.signals["Real Project"]["files"],3)
        self.assertEqual(result.signals["Agent Environment"]["paths"],3)


if __name__ == "__main__":
    unittest.main()
