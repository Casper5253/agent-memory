"""WP-S1 vault sweep dry-run plan and apply, tested on a synthetic vault only."""
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

FIXTURES = ROOT / "tests" / "fixtures" / "sweep"
TOOLS = ROOT / "tools"


def _load_sweep():
    spec = importlib.util.spec_from_file_location("vault_sweep", TOOLS / "vault_sweep.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _snapshot(vault: Path) -> dict[str, bytes]:
    snap = {}
    for dirpath, _, filenames in os.walk(vault):
        for fn in filenames:
            p = Path(dirpath) / fn
            snap[str(p.resolve())] = p.read_bytes()
    return snap


class _Tmp:
    def __init__(self, base: Path):
        base.mkdir(parents=True, exist_ok=True)
        self._d = tempfile.mkdtemp(prefix="sweep-", dir=str(base))
        self.path = Path(self._d)

    def cleanup(self):
        shutil.rmtree(self.path, ignore_errors=True)


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return subprocess.run(
        ["git"] + list(args), cwd=str(cwd), env=env,
        capture_output=True, text=True, timeout=30,
    )


def _build_vault(base: Path) -> Path:
    tmp = _Tmp(base)
    vault = tmp.path / "vault"
    work = tmp.path / "work"
    work.mkdir(parents=True)
    # Synthetic non-repository paths must not inherit the checkout's containing repository.
    (work / ".git").write_text("gitdir: unavailable", encoding="utf-8")
    for d in ("_meta", "1-Projects/demo-app", "1-Projects/my-lib",
              "0-Sessions/2026/01", "Projects-Old"):
        (vault / d).mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        FIXTURES / "Projects-Old" / "readme.txt",
        vault / "Projects-Old" / "readme.txt",
    )
    shutil.copy2(FIXTURES / "_meta" / "projects.json", vault / "_meta" / "projects.json")
    proj_text = (vault / "_meta" / "projects.json").read_text(encoding="utf-8")
    work_json = str(work).replace("\\", "/")
    (vault / "_meta" / "projects.json").write_text(
        proj_text.replace("REPLACE_WORK", work_json), encoding="utf-8",
    )
    shutil.copy2(
        FIXTURES / "1-Projects" / "demo-app" / "demo-app.md",
        vault / "1-Projects" / "demo-app" / "demo-app.md",
    )
    (vault / "1-Projects" / "my-lib").mkdir(parents=True, exist_ok=True)

    demo_src = work / "parent-repos" / "demo-repo"
    demo_src.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main", cwd=demo_src)
    _git("config", "user.email", "test@test", cwd=demo_src)
    _git("config", "user.name", "Test", cwd=demo_src)
    (demo_src / "f.txt").write_text("demo", encoding="utf-8")
    _git("add", ".", cwd=demo_src)
    _git("commit", "-q", "-m", "init", cwd=demo_src)
    demo_remote = work / "remotes" / "demo.git"
    demo_remote.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "--bare", cwd=demo_remote)
    _git("remote", "add", "origin", str(demo_remote), cwd=demo_src)
    _git("push", "-q", "origin", "main", cwd=demo_src)

    da = vault / "1-Projects" / "demo-app"
    _git("init", "-q", "-b", "main", cwd=da)
    _git("config", "user.email", "test@test", cwd=da)
    _git("config", "user.name", "Test", cwd=da)
    (da / "x.txt").write_text("x", encoding="utf-8")
    _git("add", ".", cwd=da)
    _git("commit", "-q", "-m", "init", cwd=da)
    old_demo_head = _git("rev-parse", "HEAD", cwd=da).stdout.strip()
    (da / "x.txt").write_text("x2", encoding="utf-8")
    _git("add", "x.txt", cwd=da)
    _git("commit", "-q", "-m", "advance main", cwd=da)
    demo_note = da / "demo-app.md"
    demo_text = demo_note.read_text(encoding="utf-8")
    demo_note.write_text(demo_text.replace("repository HEAD: abc1234 (stale, real head is dynamic)",
                                           f"main at {old_demo_head}"), encoding="utf-8")
    _git("remote", "add", "origin", str(demo_remote), cwd=da)

    _git("worktree", "add", "-q", str(vault / "1-Projects" / "frag-a"), "HEAD", cwd=da)

    look = vault / "1-Projects" / "look-alike-demo"
    look.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main", cwd=look)
    _git("config", "user.email", "test@test", cwd=look)
    _git("config", "user.name", "Test", cwd=look)
    (look / "x.txt").write_text("look", encoding="utf-8")
    _git("add", ".", cwd=look)
    _git("commit", "-q", "-m", "init", cwd=look)
    look_remote = work / "remotes" / "look.git"
    look_remote.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "--bare", cwd=look_remote)
    _git("remote", "add", "origin", str(look_remote), cwd=look)

    lib_src = work / "parent-repos" / "lib-repo"
    lib_src.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main", cwd=lib_src)
    _git("config", "user.email", "test@test", cwd=lib_src)
    _git("config", "user.name", "Test", cwd=lib_src)
    (lib_src / "f.txt").write_text("lib", encoding="utf-8")
    _git("add", ".", cwd=lib_src)
    _git("commit", "-q", "-m", "init", cwd=lib_src)
    lib_remote = work / "remotes" / "lib.git"
    lib_remote.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "--bare", cwd=lib_remote)
    _git("remote", "add", "origin", str(lib_remote), cwd=lib_src)
    _git("push", "-q", "origin", "main", cwd=lib_src)

    ml = vault / "1-Projects" / "my-lib"
    _git("init", "-q", "-b", "main", cwd=ml)
    _git("config", "user.email", "test@test", cwd=ml)
    _git("config", "user.name", "Test", cwd=ml)
    (ml / "x.txt").write_text("lib", encoding="utf-8")
    _git("add", ".", cwd=ml)
    _git("commit", "-q", "-m", "init", cwd=ml)
    _git("remote", "add", "origin", str(lib_remote), cwd=ml)

    lib_head = _git("rev-parse", "HEAD", cwd=ml).stdout.strip()
    (ml / "my-lib.md").write_text(
        "---\ntype: project\n---\n# My Lib\n\n"
        "## Current state (maintained by agentmem)\n\n"
        "<!-- agentmem:state:begin -->\n"
        "### Current state\n"
        f"- HEAD {lib_head}\n\n"
        "### Active decisions\n- none\n\n"
        "### Superseded decisions\n- none\n\n"
        "### Open questions and next steps\n- none\n"
        "<!-- agentmem:state:end -->\n",
        encoding="utf-8",
    )

    shutil.copy2(
        FIXTURES / "0-Sessions" / "2026" / "01" / "session-sorted.md",
        vault / "0-Sessions" / "2026" / "01" / "session-sorted.md",
    )
    shutil.copy2(
        FIXTURES / "0-Sessions" / "2026" / "01" / "session-unsorted.md",
        vault / "0-Sessions" / "2026" / "01" / "session-unsorted.md",
    )
    for name in ("session-reattr-misfiled.md", "session-reattr-nosignal.md", "session-reattr-relative.md"):
        content = (FIXTURES / "0-Sessions" / "2026" / "01" / name).read_text(encoding="utf-8")
        (vault / "0-Sessions" / "2026" / "01" / name).write_text(
            content.replace("REPLACE_WORK", work_json), encoding="utf-8",
        )

    (vault / "1-Projects" / "demo-app" / "broken.md").write_text(
        "# broken\nSee [[1-Projects/Nope/Nope]] and [[1-Projects/demo-app/demo-app]].\n",
        encoding="utf-8",
    )

    return vault


class SweepTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = _Tmp(ROOT / ".audit" / "tmp")
        cls.vault = _build_vault(cls._tmp.path)
        cls._snap_before = _snapshot(cls.vault)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _plan(self) -> dict:
        return _load_sweep().build_plan(self.vault)

    def test_a_worktree_fragment_detected(self):
        plan = self._plan()
        frags = {f["folder"]: f for f in plan["fragments"]}
        self.assertIn("frag-a", frags)
        frag = frags["frag-a"]
        self.assertEqual(frag["proposed_parent"], "demo-app")
        self.assertEqual(frag["evidence"], "worktree")
        self.assertTrue(frag["auto_apply"])

    def test_a_look_alike_is_guess_not_auto(self):
        plan = self._plan()
        frags = {f["folder"]: f for f in plan["fragments"]}
        self.assertIn("look-alike-demo", frags)
        frag = frags["look-alike-demo"]
        self.assertEqual(frag["evidence"], "guess")
        self.assertFalse(frag["auto_apply"])
        self.assertEqual(frag["proposed_parent"], "demo-app")

    def test_plan_does_not_mutate_vault(self):
        out = self._tmp.path / "plan.json"
        vs = _load_sweep()
        old_argv = sys.argv
        try:
            sys.argv = ["vault_sweep", "plan", "--vault", str(self.vault), "--out", str(out)]
            vs.main()
        finally:
            sys.argv = old_argv
        snap_after = _snapshot(self.vault)
        self.assertEqual(self._snap_before, snap_after)

    def test_worktree_detected_through_projects_json_roots(self):
        tmp = _Tmp(ROOT / ".audit" / "tmp")
        try:
            vault, work = tmp.path / "vault", tmp.path / "work"
            for d in ("_meta", "1-Projects/Tool", "1-Projects/Tool Wp9"):
                (vault / d).mkdir(parents=True)
            repo = work / "tool"
            repo.mkdir(parents=True)
            _git("init", "-q", "-b", "main", cwd=repo)
            _git("config", "user.email", "test@test", cwd=repo)
            _git("config", "user.name", "Test", cwd=repo)
            (repo / "f.txt").write_text("t", encoding="utf-8")
            _git("add", ".", cwd=repo)
            _git("commit", "-q", "-m", "init", cwd=repo)
            _git("worktree", "add", "-q", str(work / "tool-wp9"), "HEAD", cwd=repo)
            (vault / "_meta" / "projects.json").write_text(json.dumps({"projects": [
                {"name": "Tool", "roots": [str(repo)], "status": "active"},
                {"name": "Tool Wp9", "roots": [str(work / "tool-wp9")], "status": "active"}]}), encoding="utf-8")
            frags = {f["folder"]: f for f in _load_sweep().build_plan(vault)["fragments"]}
            self.assertEqual(frags["Tool Wp9"]["evidence"], "worktree")
            self.assertEqual(frags["Tool Wp9"]["proposed_parent"], "Tool")
            self.assertTrue(frags["Tool Wp9"]["auto_apply"])
        finally:
            tmp.cleanup()

    def test_plan_refuses_out_inside_vault(self):
        vs = _load_sweep()
        old_argv = sys.argv
        try:
            sys.argv = ["vault_sweep", "plan", "--vault", str(self.vault), "--out", str(self.vault / "plan.json")]
            with self.assertRaises(SystemExit):
                vs.main()
        finally:
            sys.argv = old_argv
        self.assertEqual(self._snap_before, _snapshot(self.vault))

    def test_b_leftover_folder_detected(self):
        plan = self._plan()
        names = [item["folder"] for item in plan["leftover_folders"]]
        self.assertIn("Projects-Old", names)
        leftover = next(i for i in plan["leftover_folders"] if i["folder"] == "Projects-Old")
        self.assertTrue(leftover["auto_apply"])

    def test_c_stale_state_line_detected(self):
        plan = self._plan()
        stale = [i for i in plan["stale_state_lines"] if "demo-app" in i["note"]]
        self.assertTrue(len(stale) >= 1, f"expected stale demo-app lines, got {stale}")
        self.assertIn("main at ", stale[0]["line"])
        self.assertTrue(stale[0]["stale"])
        self.assertIsNotNone(stale[0]["current_head"])
        self.assertTrue(stale[0]["auto_apply"])
        fresh = [i for i in plan["stale_state_lines"] if "my-lib" in i["note"]]
        self.assertTrue(len(fresh) >= 1)
        self.assertFalse(fresh[0]["stale"])

    def test_c_stale_scan_requires_resolvable_current_ref_claim(self):
        tmp = _Tmp(ROOT / ".audit" / "tmp")
        try:
            vault = tmp.path / "vault"
            repo = vault / "1-Projects" / "Demo"
            repo.mkdir(parents=True)
            _git("init", "-q", "-b", "main", cwd=repo)
            _git("config", "user.email", "test@test", cwd=repo)
            _git("config", "user.name", "Test", cwd=repo)
            (repo / "f.txt").write_text("one", encoding="utf-8")
            _git("add", ".", cwd=repo)
            _git("commit", "-q", "-m", "first", cwd=repo)
            old = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
            _git("branch", "deleted", old, cwd=repo)
            (repo / "f.txt").write_text("two", encoding="utf-8")
            _git("commit", "-q", "-am", "second", cwd=repo)
            _git("branch", "-D", "deleted", cwd=repo)
            note = repo / "Demo.md"
            note.write_text(
                "<!-- agentmem:state:begin -->\n"
                f"- session [[old-session-{old[:7]}]] repo hash\n"
                f"- history on other-branch commit {old}\n"
                f"- repo main at {old}\n"
                f"- deleted at {old}\n"
                "<!-- agentmem:state:end -->\n", encoding="utf-8",
            )
            items = _load_sweep()._scan_stale(vault, {"Demo": repo})
            self.assertEqual(len(items), 2, items)
            stale = next(item for item in items if item["line"].startswith("- repo main at"))
            self.assertTrue(stale["stale"])
            self.assertTrue(stale["auto_apply"])
            unresolved = next(item for item in items if item["line"].startswith("- deleted at"))
            self.assertFalse(unresolved["auto_apply"])
            self.assertFalse(unresolved["stale"])
            self.assertEqual(unresolved["reason"], "unresolved")
        finally:
            tmp.cleanup()

    def test_c_stale_scan_marks_non_repository_project_unresolved(self):
        tmp = _Tmp(ROOT / ".audit" / "tmp")
        try:
            vault = tmp.path / "vault"
            project = vault / "1-Projects" / "Demo"
            project.mkdir(parents=True)
            project_root = tmp.path / "unversioned"
            project_root.mkdir()
            (project_root / ".git").write_text("gitdir: unavailable", encoding="utf-8")
            note = project / "Demo.md"
            note.write_text(
                "<!-- agentmem:state:begin -->\n"
                "- main at 1234567\n"
                "<!-- agentmem:state:end -->\n", encoding="utf-8",
            )
            items = _load_sweep()._scan_stale(vault, {"Demo": project_root})
            self.assertEqual(len(items), 1, items)
            self.assertIsNone(items[0]["current_head"])
            self.assertFalse(items[0]["stale"])
            self.assertFalse(items[0]["auto_apply"])
            self.assertEqual(items[0]["reason"], "unresolved")
        finally:
            tmp.cleanup()

    def test_d_broken_links_detected(self):
        plan = self._plan()
        targets = [b["target"] for b in plan["broken_links"]]
        self.assertIn("1-Projects/Nope/Nope", targets)
        self.assertNotIn("1-Projects/demo-app/demo-app", targets)
        broken = next(b for b in plan["broken_links"] if b["target"] == "1-Projects/Nope/Nope")
        self.assertIn("broken", broken["source"])

    def test_e_unsorted_sessions_detected(self):
        plan = self._plan()
        sessions = [s["session"] for s in plan["unsorted_sessions"]]
        self.assertTrue(any("session-unsorted" in s for s in sessions))

    def test_f_misfiled_session_proposed_for_correct_project(self):
        plan = self._plan()
        ra = plan["re_attribution"]
        misfiled = [i for i in ra["items"] if "reattr-misfiled" in i["note"]]
        self.assertEqual(len(misfiled), 1)
        item = misfiled[0]
        self.assertEqual(item["filed_project"], "Demo App")
        self.assertEqual(item["proposed_project"], "My Lib")
        self.assertEqual(item["method"], "content")
        self.assertFalse(item["auto_apply"])
        self.assertIn("My Lib", item["signals"])
        self.assertGreater(item["signals"]["My Lib"]["score"], 0)

    def test_f_no_signal_session_not_proposed(self):
        plan = self._plan()
        ra = plan["re_attribution"]
        nosignal = [i for i in ra["items"] if "reattr-nosignal" in i["note"]]
        self.assertEqual(nosignal, [])

    def test_f_relative_project_files_outweigh_one_foreign_path(self):
        ra = self._plan()["re_attribution"]
        self.assertEqual([i for i in ra["items"] if "reattr-relative" in i["note"]], [])

    def test_f_summary_paths_are_not_stated_user_path_signals(self):
        data = json.loads((self.vault / "_meta" / "projects.json").read_text(encoding="utf-8"))
        foreign = next(p["roots"][0] for p in data["projects"] if p["name"] == "My Lib")
        paths = [str(Path(foreign) / ("config-" + str(i) + ".json")) for i in range(10)]
        path = self.vault / "0-Sessions/2026/01/summary-path-noise.md"
        text = ('---\ntype: session\nproject: "Demo App"\n---\n\n# My Lib summary references ' + " ".join(paths)
                + '\n\n## Executive summary\n\n' + " ".join(paths)
                + '\n\n## Artifacts and modified files\n\n- `none recorded`\n\n## Commands run\n\n'
                + "\n".join('- `' + p + '`' for p in paths) + '\n')
        path.write_text(text,encoding="utf-8")
        try:
            items = self._plan()["re_attribution"]["items"]
            self.assertFalse(any(i["note"].endswith(path.name) for i in items))
            self.assertTrue(any("reattr-misfiled" in i["note"] for i in items))
            for item in items:
                for signal in item["signals"].values():
                    self.assertEqual((signal["paths"],signal["names"]),(0,0))
        finally:
            path.unlink()

    def test_f_auto_apply_false_on_every_item(self):
        plan = self._plan()
        for item in plan["re_attribution"]["items"]:
            self.assertFalse(item["auto_apply"])

    def test_f_calibration_sample_deterministic(self):
        plan1 = self._plan()
        plan2 = self._plan()
        s1 = [i["note"] for i in plan1["re_attribution"]["calibration_sample"]]
        s2 = [i["note"] for i in plan2["re_attribution"]["calibration_sample"]]
        self.assertEqual(s1, s2)
        self.assertLessEqual(len(plan1["re_attribution"]["calibration_sample"]), 30)

    def test_f_re_attribution_item_schema(self):
        plan = self._plan()
        for item in plan["re_attribution"]["items"]:
            self.assertEqual(
                set(item.keys()),
                {"note", "filed_project", "proposed_project", "method", "signals", "auto_apply"},
            )

    def test_f_vault_unchanged_after_plan(self):
        self._plan()
        snap_after = _snapshot(self.vault)
        self.assertEqual(self._snap_before, snap_after)

    def test_all_sections_present(self):
        plan = self._plan()
        for key in ("fragments", "leftover_folders", "stale_state_lines",
                    "broken_links", "unsorted_sessions", "re_attribution"):
            self.assertIn(key, plan)

    def test_fragment_item_schema(self):
        plan = self._plan()
        for frag in plan["fragments"]:
            self.assertEqual(
                set(frag.keys()),
                {"folder", "proposed_parent", "evidence", "auto_apply"},
            )
            self.assertIn(frag["evidence"], ("worktree", "shared_remote", "guess"))

    def test_fake_worktree_not_auto_applied(self):
        tmp = _Tmp(ROOT / ".audit" / "tmp")
        try:
            vault, work = tmp.path / "vault", tmp.path / "work"
            for d in ("_meta", "1-Projects"):
                (vault / d).mkdir(parents=True)
            repo = work / "real-repo"
            repo.mkdir(parents=True)
            _git("init", "-q", "-b", "main", cwd=repo)
            _git("config", "user.email", "test@test", cwd=repo)
            _git("config", "user.name", "Test", cwd=repo)
            (repo / "f.txt").write_text("r", encoding="utf-8")
            _git("add", ".", cwd=repo)
            _git("commit", "-q", "-m", "init", cwd=repo)
            _git("worktree", "add", "-q", str(work / "real-wt"), "HEAD", cwd=repo)
            real_proj = vault / "1-Projects" / "Real"
            shutil.copytree(repo, real_proj)
            fake = vault / "1-Projects" / "Real-copy"
            shutil.copytree(work / "real-wt", fake)
            fake_git = fake / ".git"
            fake_git.unlink()
            admin_dir = repo / ".git" / "worktrees" / "fake-id"
            admin_dir.mkdir(parents=True)
            (admin_dir / "gitdir").write_text("wrong-path", encoding="utf-8")
            fake_git.write_text(f"gitdir: {admin_dir}".replace("\\", "/"), encoding="utf-8")
            (vault / "_meta" / "projects.json").write_text(json.dumps({"projects": [
                {"name": "Real", "roots": [str(repo)], "status": "active"},
                {"name": "Real-copy", "roots": [str(fake)], "status": "active"}]}), encoding="utf-8")
            frags = {f["folder"]: f for f in _load_sweep().build_plan(vault)["fragments"]}
            self.assertIn("Real-copy", frags)
            frag = frags["Real-copy"]
            self.assertNotEqual(frag["evidence"], "worktree")
            self.assertFalse(frag["auto_apply"])
        finally:
            tmp.cleanup()

    def test_relative_remote_url_normalized(self):
        tmp = _Tmp(ROOT / ".audit" / "tmp")
        try:
            vault = tmp.path / "vault"
            for d in ("_meta", "1-Projects"):
                (vault / d).mkdir(parents=True)
            remote = tmp.path / "shared.git"
            remote.mkdir(parents=True)
            _git("init", "-q", "--bare", cwd=remote)
            parent = vault / "1-Projects" / "Parent"
            parent.mkdir(parents=True)
            _git("init", "-q", "-b", "main", cwd=parent)
            _git("config", "user.email", "test@test", cwd=parent)
            _git("config", "user.name", "Test", cwd=parent)
            (parent / "f.txt").write_text("p", encoding="utf-8")
            _git("add", ".", cwd=parent)
            _git("commit", "-q", "-m", "init", cwd=parent)
            _git("remote", "add", "origin", str(remote), cwd=parent)
            child = vault / "1-Projects" / "Child-copy"
            child.mkdir(parents=True)
            _git("init", "-q", "-b", "main", cwd=child)
            _git("config", "user.email", "test@test", cwd=child)
            _git("config", "user.name", "Test", cwd=child)
            (child / "f.txt").write_text("c", encoding="utf-8")
            _git("add", ".", cwd=child)
            _git("commit", "-q", "-m", "init", cwd=child)
            _git("remote", "add", "origin", "../../../shared.git", cwd=child)
            (vault / "_meta" / "projects.json").write_text(json.dumps({"projects": [
                {"name": "Parent", "roots": [str(parent)], "status": "active"},
                {"name": "Child-copy", "roots": [str(child)], "status": "active"}]}), encoding="utf-8")
            frags = {f["folder"]: f for f in _load_sweep().build_plan(vault)["fragments"]}
            self.assertIn("Child-copy", frags)
            frag = frags["Child-copy"]
            self.assertEqual(frag["evidence"], "shared_remote")
            self.assertFalse(frag["auto_apply"])
        finally:
            tmp.cleanup()

    def test_worktree_of_nested_foreign_repo_is_not_parent_worktree(self):
        tmp = _Tmp(ROOT / ".audit" / "tmp")
        try:
            vault, work = tmp.path / "vault", tmp.path / "work"
            for d in ("_meta", "1-Projects/Parent", "1-Projects/Parent Wt"):
                (vault / d).mkdir(parents=True)
            parent = work / "parent"
            parent.mkdir(parents=True)
            _git("init", "-q", "-b", "main", cwd=parent)
            _git("config", "user.email", "test@test", cwd=parent)
            _git("config", "user.name", "Test", cwd=parent)
            (parent / "f.txt").write_text("p", encoding="utf-8")
            _git("add", ".", cwd=parent)
            _git("commit", "-q", "-m", "init", cwd=parent)
            foreign = parent / "foreign.git"
            _git("init", "-q", "--bare", "-b", "main", str(foreign), cwd=work)
            _git("push", "-q", str(foreign), "main", cwd=parent)
            _git("worktree", "add", "-q", str(work / "parent-wt"), "main", cwd=foreign)
            (vault / "_meta" / "projects.json").write_text(json.dumps({"projects": [
                {"name": "Parent", "roots": [str(parent)], "status": "active"},
                {"name": "Parent Wt", "roots": [str(work / "parent-wt")], "status": "active"}]}), encoding="utf-8")
            frags = {f["folder"]: f for f in _load_sweep().build_plan(vault)["fragments"]}
            self.assertFalse(frags.get("Parent Wt", {"auto_apply": False})["auto_apply"])
        finally:
            tmp.cleanup()

    def test_hand_made_worktree_files_are_not_registration(self):
        tmp = _Tmp(ROOT / ".audit" / "tmp")
        try:
            vault, work = tmp.path / "vault", tmp.path / "work"
            for d in ("_meta", "1-Projects/Parent", "1-Projects/Parent Fake"):
                (vault / d).mkdir(parents=True)
            parent = work / "parent"
            parent.mkdir(parents=True)
            _git("init", "-q", "-b", "main", cwd=parent)
            _git("config", "user.email", "test@test", cwd=parent)
            _git("config", "user.name", "Test", cwd=parent)
            (parent / "f.txt").write_text("p", encoding="utf-8")
            _git("add", ".", cwd=parent)
            _git("commit", "-q", "-m", "init", cwd=parent)
            frag, admin = work / "parent-fake", work / "unregistered-admin"
            frag.mkdir()
            admin.mkdir()
            (frag / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
            (admin / "gitdir").write_text(f"{frag / '.git'}\n", encoding="utf-8")
            (admin / "commondir").write_text(f"{parent / '.git'}\n", encoding="utf-8")
            (vault / "_meta" / "projects.json").write_text(json.dumps({"projects": [
                {"name": "Parent", "roots": [str(parent)], "status": "active"},
                {"name": "Parent Fake", "roots": [str(frag)], "status": "active"}]}), encoding="utf-8")
            frags = {f["folder"]: f for f in _load_sweep().build_plan(vault)["fragments"]}
            self.assertFalse(frags.get("Parent Fake", {"auto_apply": False})["auto_apply"])
        finally:
            tmp.cleanup()

    def test_registered_worktree_with_broken_head_is_not_proof(self):
        tmp = _Tmp(ROOT / ".audit" / "tmp")
        try:
            vault, work = tmp.path / "vault", tmp.path / "work"
            for d in ("_meta", "1-Projects/Parent", "1-Projects/Parent Broken"):
                (vault / d).mkdir(parents=True)
            parent = work / "parent"
            parent.mkdir(parents=True)
            _git("init", "-q", "-b", "main", cwd=parent)
            _git("config", "user.email", "test@test", cwd=parent)
            _git("config", "user.name", "Test", cwd=parent)
            (parent / "f.txt").write_text("p", encoding="utf-8")
            _git("add", ".", cwd=parent)
            _git("commit", "-q", "-m", "init", cwd=parent)
            frag = work / "parent-broken"
            _git("worktree", "add", "-q", "--detach", str(frag), "HEAD", cwd=parent)
            admin = parent / ".git" / "worktrees" / "parent-broken"
            (admin / "HEAD").rename(admin / "HEAD.saved")
            (vault / "_meta" / "projects.json").write_text(json.dumps({"projects": [
                {"name": "Parent", "roots": [str(parent)], "status": "active"},
                {"name": "Parent Broken", "roots": [str(frag)], "status": "active"}]}), encoding="utf-8")
            frags = {f["folder"]: f for f in _load_sweep().build_plan(vault)["fragments"]}
            self.assertFalse(frags.get("Parent Broken", {"auto_apply": False})["auto_apply"])
        finally:
            tmp.cleanup()

    def test_ambiguous_root_marked_guess(self):
        tmp = _Tmp(ROOT / ".audit" / "tmp")
        try:
            vault = tmp.path / "vault"
            for d in ("_meta", "1-Projects"):
                (vault / d).mkdir(parents=True)
            repo = tmp.path / "shared-repo"
            repo.mkdir(parents=True)
            _git("init", "-q", "-b", "main", cwd=repo)
            _git("config", "user.email", "test@test", cwd=repo)
            _git("config", "user.name", "Test", cwd=repo)
            (repo / "f.txt").write_text("s", encoding="utf-8")
            _git("add", ".", cwd=repo)
            _git("commit", "-q", "-m", "init", cwd=repo)
            remote = tmp.path / "shared.git"
            remote.mkdir(parents=True)
            _git("init", "-q", "--bare", cwd=remote)
            _git("remote", "add", "origin", str(remote), cwd=repo)
            shutil.copytree(repo, vault / "1-Projects" / "ProjA")
            shutil.copytree(repo, vault / "1-Projects" / "ProjB")
            shutil.copytree(repo, vault / "1-Projects" / "ProjA-wt")
            (vault / "_meta" / "projects.json").write_text(json.dumps({"projects": [
                {"name": "ProjA", "roots": [str(vault / "1-Projects" / "ProjA")], "status": "active"},
                {"name": "ProjB", "roots": [str(vault / "1-Projects" / "ProjA")], "status": "active"},
                {"name": "ProjA-wt", "roots": [str(vault / "1-Projects" / "ProjA-wt")], "status": "active"}]}), encoding="utf-8")
            frags = {f["folder"]: f for f in _load_sweep().build_plan(vault)["fragments"]}
            self.assertIn("ProjA-wt", frags)
            frag = frags["ProjA-wt"]
            self.assertEqual(frag["evidence"], "guess")
            self.assertFalse(frag["auto_apply"])
        finally:
            tmp.cleanup()

    def test_same_root_marked_same_root(self):
        tmp = _Tmp(ROOT / ".audit" / "tmp")
        try:
            vault = tmp.path / "vault"
            for d in ("_meta", "1-Projects"):
                (vault / d).mkdir(parents=True)
            repo = tmp.path / "main-repo"
            repo.mkdir(parents=True)
            _git("init", "-q", "-b", "main", cwd=repo)
            _git("config", "user.email", "test@test", cwd=repo)
            _git("config", "user.name", "Test", cwd=repo)
            (repo / "f.txt").write_text("m", encoding="utf-8")
            _git("add", ".", cwd=repo)
            _git("commit", "-q", "-m", "init", cwd=repo)
            remote = tmp.path / "main.git"
            remote.mkdir(parents=True)
            _git("init", "-q", "--bare", cwd=remote)
            _git("remote", "add", "origin", str(remote), cwd=repo)
            frag = tmp.path / "frag-repo"
            frag.mkdir(parents=True)
            _git("init", "-q", "-b", "main", cwd=frag)
            _git("config", "user.email", "test@test", cwd=frag)
            _git("config", "user.name", "Test", cwd=frag)
            (frag / "f.txt").write_text("f", encoding="utf-8")
            _git("add", ".", cwd=frag)
            _git("commit", "-q", "-m", "init", cwd=frag)
            _git("remote", "add", "origin", str(remote), cwd=frag)
            shutil.copytree(repo, vault / "1-Projects" / "Main")
            shutil.copytree(repo, vault / "1-Projects" / "Main-copy")
            (vault / "_meta" / "projects.json").write_text(json.dumps({"projects": [
                {"name": "Main", "roots": [str(vault / "1-Projects" / "Main")], "status": "active"},
                {"name": "Main-copy", "roots": [str(vault / "1-Projects" / "Main")], "status": "active"}]}), encoding="utf-8")
            frags = {f["folder"]: f for f in _load_sweep().build_plan(vault)["fragments"]}
            if "Main-copy" in frags:
                frag_item = frags["Main-copy"]
                self.assertEqual(frag_item["evidence"], "guess")
                self.assertFalse(frag_item["auto_apply"])
        finally:
            tmp.cleanup()


def _count_files_in(path: Path) -> int:
    n = 0
    for _, _, files in os.walk(path):
        n += len(files)
    return n


def _build_git_vault(base: Path) -> Path:
    vault = _build_vault(base)
    env = dict(os.environ)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    for args in [
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "test@test"],
        ["git", "config", "user.name", "Test"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "initial"],
    ]:
        subprocess.run(args, cwd=str(vault), env=env, capture_output=True, timeout=10)
    return vault


def _name_parent_registry_entry(vault: Path, name: str) -> None:
    pjson = vault / "_meta" / "projects.json"
    data = json.loads(pjson.read_text(encoding="utf-8"))
    parent = next(p for p in data["projects"] if p["name"].lower() == "demo app")
    parent["name"] = name
    pjson.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _session_bytes(vault: Path) -> dict[str, bytes]:
    result = {}
    sess = vault / "0-Sessions"
    if sess.is_dir():
        for dirpath, _, filenames in os.walk(sess):
            for fn in filenames:
                p = Path(dirpath) / fn
                result[str(p.resolve())] = p.read_bytes()
    return result


def _run_sweep(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return subprocess.run(
        [sys.executable, str(TOOLS / "vault_sweep.py")] + list(args),
        cwd=str(cwd) if cwd else None,
        env=env, capture_output=True, text=True, timeout=60,
    )


def _snapshot_no_git(vault: Path) -> dict[str, bytes]:
    snap = {}
    for dirpath, dirnames, filenames in os.walk(vault):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for fn in filenames:
            p = Path(dirpath) / fn
            snap[str(p.resolve())] = p.read_bytes()
    return snap


def _snapshot_content(vault: Path) -> dict[str, bytes]:
    snap = {}
    for dirpath, dirnames, filenames in os.walk(vault):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for fn in filenames:
            p = Path(dirpath) / fn
            rel = p.resolve().relative_to(vault.resolve())
            if str(rel).startswith("_meta"):
                continue
            snap[str(p.resolve())] = p.read_bytes()
    return snap


class SweepApplyOnlyAutoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = _Tmp(ROOT / ".audit" / "tmp")
        cls.vault = _build_git_vault(cls._tmp.path)
        _name_parent_registry_entry(cls.vault, "demo-app")
        cls.vs = _load_sweep()
        cls.plan = cls.vs.build_plan(cls.vault)
        cls.plan["vault_path"] = str(cls.vault)
        cls.plan_path = cls._tmp.path / "plan.json"
        cls.plan_path.write_text(
            json.dumps(cls.plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
        )
        cls._session_bytes_before = _session_bytes(cls.vault)
        cls._file_count_before = _count_files_in(cls.vault)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_a_only_auto_applies_worktree_fragment(self):
        result = _run_sweep("apply", "--vault", str(self.vault), "--plan", str(self.plan_path), "--only-auto")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        frag_dir = self.vault / "1-Projects" / "frag-a"
        self.assertFalse(frag_dir.exists(), "frag-a should have been moved to archive")
        archive = self.vault / "4-Archive" / "merged" / "frag-a"
        self.assertTrue(archive.exists(), "frag-a should be in 4-Archive/merged/")

    def test_b_only_auto_does_not_apply_guess_fragments(self):
        _run_sweep("apply", "--vault", str(self.vault), "--plan", str(self.plan_path), "--only-auto")
        look = self.vault / "1-Projects" / "look-alike-demo"
        self.assertTrue(look.exists(), "look-alike-demo (guess) should not be moved by --only-auto")

    def test_c_session_note_bytes_unchanged(self):
        _run_sweep("apply", "--vault", str(self.vault), "--plan", str(self.plan_path), "--only-auto")
        session_after = _session_bytes(self.vault)
        self.assertEqual(self._session_bytes_before, session_after)

    def test_d_file_count_never_drops(self):
        _run_sweep("apply", "--vault", str(self.vault), "--plan", str(self.plan_path), "--only-auto")
        count_after = _count_files_in(self.vault)
        self.assertGreaterEqual(count_after, self._file_count_before)

    def test_e_root_leftover_is_archived(self):
        result = _run_sweep("apply", "--vault", str(self.vault), "--plan", str(self.plan_path), "--only-auto")
        leftover = self.vault / "Projects-Old"
        archived = self.vault / "4-Archive" / "Projects-Old"
        self.assertFalse(leftover.exists(), "listed top-level leftovers should be moved to the archive")
        self.assertTrue(archived.is_dir())
        self.assertTrue((archived / "readme.txt").is_file())
        logs = list((self.vault / "_meta" / "logs").glob("sweep-*.jsonl"))
        entries = [json.loads(line) for line in logs[-1].read_text(encoding="utf-8").splitlines() if line.strip()]
        leftover_entry = next(e for e in entries if e.get("action") == "leftover_archive")
        self.assertIn(leftover_entry["status"], {"ok", "skip"})

    def test_f_stale_line_prefixed(self):
        _run_sweep("apply", "--vault", str(self.vault), "--plan", str(self.plan_path), "--only-auto")
        demo_note = self.vault / "1-Projects" / "demo-app" / "demo-app.md"
        text = demo_note.read_text(encoding="utf-8")
        self.assertIn("(as of unknown commit)", text)
        self.assertIn("main at ", text)

    def test_g_pause_removed_when_not_preexisting(self):
        pause = self.vault / "_meta" / "PAUSE"
        if pause.exists():
            pause.unlink()
        _run_sweep("apply", "--vault", str(self.vault), "--plan", str(self.plan_path), "--only-auto")
        self.assertFalse(pause.exists(), "PAUSE should be removed after apply if it didn't exist before")

    def test_h_log_written(self):
        _run_sweep("apply", "--vault", str(self.vault), "--plan", str(self.plan_path), "--only-auto")
        logs = list((self.vault / "_meta" / "logs").glob("sweep-*.jsonl"))
        self.assertTrue(len(logs) >= 1, "apply log should be written")
        entries = [json.loads(line) for line in logs[0].read_text(encoding="utf-8").splitlines() if line.strip()]
        snapshot_entries = [e for e in entries if e.get("action") == "snapshot"]
        self.assertTrue(len(snapshot_entries) >= 1)
        self.assertEqual(snapshot_entries[-1]["status"], "ok")
        self.assertIn("sha", snapshot_entries[-1])

    def test_i_rerun_is_noop(self):
        _run_sweep("apply", "--vault", str(self.vault), "--plan", str(self.plan_path), "--only-auto")
        snap_after_first = _snapshot_content(self.vault)
        _run_sweep("apply", "--vault", str(self.vault), "--plan", str(self.plan_path), "--only-auto")
        snap_after_second = _snapshot_content(self.vault)
        self.assertEqual(snap_after_first, snap_after_second)


class SweepApplyGitResetTest(unittest.TestCase):
    def test_git_reset_to_snapshot_restores_files(self):
        tmp = _Tmp(ROOT / ".audit" / "tmp")
        try:
            vault = _build_git_vault(tmp.path)
            _name_parent_registry_entry(vault, "demo-app")
            vs = _load_sweep()
            plan = vs.build_plan(vault)
            plan["vault_path"] = str(vault)
            plan_path = tmp.path / "plan.json"
            plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
            env = dict(os.environ)
            env["GIT_CONFIG_NOSYSTEM"] = "1"
            config = subprocess.run(
                ["git", "config", "core.autocrlf", "true"],
                cwd=str(vault), env=env, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(config.returncode, 0, msg=config.stderr)
            tracked = subprocess.run(
                ["git", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false", "ls-files", "-z"],
                cwd=str(vault), env=env, capture_output=True, timeout=10,
            )
            self.assertEqual(tracked.returncode, 0, msg=tracked.stderr)
            tracked_paths = [vault / p.decode("utf-8") for p in tracked.stdout.split(b"\0") if p and (vault / p.decode("utf-8")).is_file()]
            snapshot_bytes = {p.relative_to(vault).as_posix(): p.read_bytes() for p in tracked_paths}
            result = _run_sweep("apply", "--vault", str(vault), "--plan", str(plan_path), "--only-auto")
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            sha = None
            for line in result.stdout.splitlines():
                if line.startswith("sweep snapshot"):
                    sha = line.split()[-1]
                    break
            self.assertIsNotNone(sha, f"snapshot SHA not found in output: {result.stdout}")
            self.assertRegex(sha, r"^[0-9a-f]{7,}$")
            leftover = vault / "Projects-Old"
            self.assertFalse(leftover.exists(), "listed top-level leftover should be archived")
            self.assertTrue((vault / "4-Archive" / "Projects-Old").is_dir())
            reset_result = subprocess.run(
                ["git", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false", "reset", "--hard", sha],
                cwd=str(vault), env=env, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(reset_result.returncode, 0, msg=reset_result.stderr)
            leftover_restored = vault / "Projects-Old"
            self.assertTrue(leftover_restored.exists(), "the Git snapshot should restore tracked root-leftover bytes")
            restored_bytes = {p.relative_to(vault).as_posix(): p.read_bytes() for p in tracked_paths}
            self.assertEqual(snapshot_bytes, restored_bytes)
        finally:
            tmp.cleanup()


class SweepApplyVaultMismatchTests(unittest.TestCase):
    def test_plan_from_another_vault_is_refused(self):
        tmp = _Tmp(ROOT / ".audit" / "tmp")
        try:
            vault = _build_git_vault(tmp.path)
            plan = {"vault_path": "/nonexistent/other/vault", "fragments": [], "leftover_folders": [],
                    "stale_state_lines": [], "broken_links": [], "unsorted_sessions": [],
                    "re_attribution": {"items": [], "calibration_sample": []}}
            plan_path = tmp.path / "bad-plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            result = _run_sweep("apply", "--vault", str(vault), "--plan", str(plan_path))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("different vault", result.stderr)
        finally:
            tmp.cleanup()

    def test_plan_inside_vault_is_refused(self):
        tmp = _Tmp(ROOT / ".audit" / "tmp")
        try:
            vault = _build_git_vault(tmp.path)
            plan_path = vault / "plan.json"
            plan_path.write_text("{}", encoding="utf-8")
            result = _run_sweep("apply", "--vault", str(vault), "--plan", str(plan_path))
            self.assertNotEqual(result.returncode, 0)
        finally:
            tmp.cleanup()

    def test_non_git_vault_is_refused(self):
        tmp = _Tmp(ROOT / ".audit" / "tmp")
        try:
            vault = tmp.path / "no-git-vault"
            vault.mkdir(parents=True)
            (vault / "_meta").mkdir()
            plan = {"vault_path": str(vault), "fragments": [], "leftover_folders": [],
                    "stale_state_lines": [], "broken_links": [], "unsorted_sessions": [],
                    "re_attribution": {"items": [], "calibration_sample": []}}
            plan_path = tmp.path / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            result = _run_sweep("apply", "--vault", str(vault), "--plan", str(plan_path))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not a git", result.stderr.lower())
        finally:
            tmp.cleanup()


class SweepApplyPauseTests(unittest.TestCase):
    def test_pause_preexisting_is_preserved(self):
        tmp = _Tmp(ROOT / ".audit" / "tmp")
        try:
            vault = _build_git_vault(tmp.path)
            pause = vault / "_meta" / "PAUSE"
            pause.parent.mkdir(parents=True, exist_ok=True)
            pause.write_text("pre-existing", encoding="utf-8")
            vs = _load_sweep()
            plan = vs.build_plan(vault)
            plan["vault_path"] = str(vault)
            plan_path = tmp.path / "plan.json"
            plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
            _run_sweep("apply", "--vault", str(vault), "--plan", str(plan_path), "--only-auto")
            self.assertTrue(pause.exists(), "pre-existing PAUSE should still exist after apply")
            self.assertEqual(pause.read_text(encoding="utf-8"), "pre-existing")
        finally:
            tmp.cleanup()

    def test_pause_created_then_removed(self):
        tmp = _Tmp(ROOT / ".audit" / "tmp")
        try:
            vault = _build_git_vault(tmp.path)
            pause = vault / "_meta" / "PAUSE"
            if pause.exists():
                pause.unlink()
            vs = _load_sweep()
            plan = vs.build_plan(vault)
            plan["vault_path"] = str(vault)
            plan_path = tmp.path / "plan.json"
            plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
            _run_sweep("apply", "--vault", str(vault), "--plan", str(plan_path), "--only-auto")
            self.assertFalse(pause.exists(), "PAUSE should be removed after apply when it was not pre-existing")
        finally:
            tmp.cleanup()


class SweepApplyDryRunTests(unittest.TestCase):
    def test_dry_run_writes_nothing(self):
        tmp = _Tmp(ROOT / ".audit" / "tmp")
        try:
            vault = _build_git_vault(tmp.path)
            snap_before = _snapshot(vault)
            vs = _load_sweep()
            plan = vs.build_plan(vault)
            plan["vault_path"] = str(vault)
            plan_path = tmp.path / "plan.json"
            plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
            result = _run_sweep("apply", "--vault", str(vault), "--plan", str(plan_path), "--dry-run")
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("Dry run", result.stdout)
            snap_after = _snapshot(vault)
            self.assertEqual(snap_before, snap_after)
        finally:
            tmp.cleanup()


class SweepApplyFragmentPendingAppendTests(unittest.TestCase):
    def test_fragment_state_appended_to_parent_pending(self):
        tmp = _Tmp(ROOT / ".audit" / "tmp")
        try:
            vault = tmp.path / "vault"
            work = tmp.path / "work"
            for d in ("_meta", "1-Projects/parent-proj", "1-Projects/parent-proj-wt"):
                (vault / d).mkdir(parents=True)
            repo = work / "parent-repo"
            repo.mkdir(parents=True)
            _git("init", "-q", "-b", "main", cwd=repo)
            _git("config", "user.email", "t@t", cwd=repo)
            _git("config", "user.name", "T", cwd=repo)
            (repo / "f.txt").write_text("p", encoding="utf-8")
            _git("add", ".", cwd=repo)
            _git("commit", "-q", "-m", "init", cwd=repo)
            _git("worktree", "add", "-q", str(vault / "1-Projects" / "parent-proj-wt"), "HEAD", cwd=repo)
            (vault / "_meta" / "projects.json").write_text(json.dumps({"projects": [
                {"name": "parent-proj", "roots": [str(repo)], "status": "active"},
                {"name": "parent-proj-wt", "roots": [str(vault / "1-Projects" / "parent-proj-wt")], "status": "active"}
            ]}), encoding="utf-8")
            parent_note = vault / "1-Projects" / "parent-proj" / "parent-proj.md"
            parent_note.write_text(
                "---\ntype: project\n---\n# Parent\n\n"
                "## Current state (maintained by agentmem)\n\n"
                "<!-- agentmem:state:begin -->\n- state\n<!-- agentmem:state:end -->\n\n"
                "## Pending\n\n"
                "<!-- agentmem:pending:begin -->\n<!-- agentmem:pending:end -->\n",
                encoding="utf-8",
            )
            frag_note = vault / "1-Projects" / "parent-proj-wt" / "parent-proj-wt.md"
            frag_note.write_text(
                "---\ntype: project\n---\n# WT\n\n"
                "<!-- agentmem:state:begin -->\n- frag state\n<!-- agentmem:state:end -->\n\n"
                "<!-- agentmem:pending:begin -->\n- frag pending\n<!-- agentmem:pending:end -->\n",
                encoding="utf-8",
            )
            _git("init", "-q", "-b", "main", cwd=vault)
            _git("config", "user.email", "t@t", cwd=vault)
            _git("config", "user.name", "T", cwd=vault)
            _git("add", "-A", cwd=vault)
            _git("commit", "-q", "-m", "init", cwd=vault)
            vs = _load_sweep()
            plan = vs.build_plan(vault)
            plan["vault_path"] = str(vault)
            frags = {f["folder"]: f for f in plan["fragments"]}
            self.assertIn("parent-proj-wt", frags)
            self.assertTrue(frags["parent-proj-wt"]["auto_apply"])
            plan_path = tmp.path / "plan.json"
            plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
            _run_sweep("apply", "--vault", str(vault), "--plan", str(plan_path), "--only-auto")
            text = parent_note.read_text(encoding="utf-8")
            self.assertIn("Merged from parent-proj-wt", text)
            self.assertIn("frag state", text)
            self.assertIn("frag pending", text)
        finally:
            tmp.cleanup()


class SweepApplyCorrectionTests(unittest.TestCase):
    """Regression tests for WP-S1 apply correction round 1."""

    def _build_minimal_git_vault(self, tmp: Path) -> Path:
        vault = tmp / "vault"
        for d in ("_meta", "1-Projects/parent", "1-Projects/frag", "0-Sessions"):
            (vault / d).mkdir(parents=True, exist_ok=True)
        (vault / "0-Sessions" / "s.md").write_bytes(b"session\n")
        (vault / "1-Projects" / "parent" / "parent.md").write_bytes(
            b"# P\n<!-- agentmem:state:begin -->\nrepo head abc1234 commit\n<!-- agentmem:state:end -->\n"
            b"<!-- agentmem:pending:begin -->\n- old todo\n<!-- agentmem:pending:end -->\n"
        )
        (vault / "1-Projects" / "frag" / "frag.md").write_bytes(
            b"# F\n<!-- agentmem:state:begin -->\nfrag state\n<!-- agentmem:state:end -->\n"
            b"<!-- agentmem:pending:begin -->\nfrag pending\n<!-- agentmem:pending:end -->\n"
        )
        (vault / "_meta" / "projects.json").write_text(
            json.dumps({
                "projects": [
                    {"name": "Parent", "roots": ["C:\\r\\parent"], "status": "active"},
                    {"name": "frag", "roots": ["C:\\r\\frag"], "aliases": ["fragalias"], "status": "active"},
                ]
            }, indent=2),
            encoding="utf-8",
        )
        env = dict(os.environ)
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        for args in (
            ["git", "init", "-q", "-b", "main"],
            ["git", "config", "user.email", "test@test"],
            ["git", "config", "user.name", "Test"],
            ["git", "add", "-A"],
            ["git", "commit", "-q", "-m", "init"],
        ):
            subprocess.run(args, cwd=str(vault), env=env, capture_output=True, timeout=10)
        return vault

    def test_pause_is_not_part_of_snapshot_commit(self):
        tmp = _Tmp(ROOT / ".audit" / "tmp")
        try:
            vault = self._build_minimal_git_vault(tmp.path)
            vs = _load_sweep()
            plan = {"vault_path": str(vault), "fragments": [], "leftover_folders": [],
                    "stale_state_lines": [], "broken_links": [], "unsorted_sessions": [],
                    "re_attribution": {"items": [], "calibration_sample": []}}
            result = vs.apply_plan(vault, plan)
            self.assertTrue(result.get("ok"), msg=result)
            sha = result["sha"]
            env = dict(os.environ)
            env["GIT_CONFIG_NOSYSTEM"] = "1"
            show = subprocess.run(
                ["git", "show", "--stat", "--format=%s", sha],
                cwd=str(vault), env=env, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(show.returncode, 0, msg=show.stderr)
            self.assertNotIn("_meta/PAUSE", show.stdout)
            self.assertFalse((vault / "_meta" / "PAUSE").exists())
        finally:
            tmp.cleanup()

    def test_embedded_repository_snapshot_warning_does_not_abort_after_backup(self):
        tmp = _Tmp(ROOT / ".audit" / "tmp")
        try:
            vault = self._build_minimal_git_vault(tmp.path)
            emb = vault / "emb"
            emb.mkdir()
            env = dict(os.environ)
            env["GIT_CONFIG_NOSYSTEM"] = "1"
            subprocess.run(["git", "init", "-q"], cwd=str(emb), env=env, capture_output=True, timeout=10)
            vs = _load_sweep()
            plan = {"vault_path": str(vault), "fragments": [], "leftover_folders": [],
                    "stale_state_lines": [], "broken_links": [], "unsorted_sessions": [],
                    "re_attribution": {"items": [], "calibration_sample": []}}
            result = vs.apply_plan(vault, plan)
            self.assertTrue(result.get("ok"), msg=result)
            self.assertTrue(Path(result["backup_dir"]).is_dir())
            snapshot = next(e for e in result["log_entries"] if e.get("action") == "snapshot")
            self.assertEqual(snapshot["status"], "warning")
            self.assertIn("embedded repository", snapshot["reason"].lower())
            self.assertIn(str(emb / ".git"), snapshot["reason"])
            self.assertFalse((vault / "_meta" / "PAUSE").exists())
        finally:
            tmp.cleanup()

    def test_crlf_true_repo_config_reset_restores_bytes(self):
        tmp = _Tmp(ROOT / ".audit" / "tmp")
        try:
            vault = self._build_minimal_git_vault(tmp.path)
            env = dict(os.environ)
            env["GIT_CONFIG_NOSYSTEM"] = "1"
            subprocess.run(["git", "config", "core.autocrlf", "true"], cwd=str(vault), env=env, capture_output=True, timeout=10)
            note = vault / "1-Projects" / "parent" / "parent.md"
            note.write_bytes(
                b"# P\r\n<!-- agentmem:state:begin -->\r\nrepo head abc1234 commit\r\n<!-- agentmem:state:end -->\r\n"
                b"<!-- agentmem:pending:begin -->\r\n- old todo\r\n<!-- agentmem:pending:end -->\r\n"
            )
            subprocess.run(["git", "add", "-A"], cwd=str(vault), env=env, capture_output=True, timeout=10)
            subprocess.run(["git", "commit", "-q", "-m", "crlf"], cwd=str(vault), env=env, capture_output=True, timeout=10)
            before = note.read_bytes()
            self.assertGreater(before.count(b"\r\n"), 0)
            tracked_before = subprocess.run(
                ["git", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false", "ls-files", "-z"],
                cwd=str(vault), env=env, capture_output=True, timeout=10,
            )
            self.assertEqual(tracked_before.returncode, 0, msg=tracked_before.stderr)
            file_paths = [vault / p.decode("utf-8") for p in tracked_before.stdout.split(b"\0") if p]
            snapshot_bytes = {p.relative_to(vault).as_posix(): p.read_bytes() for p in file_paths}
            vs = _load_sweep()
            plan = {"vault_path": str(vault), "fragments": [], "leftover_folders": [],
                    "stale_state_lines": [{"note": "1-Projects/parent/parent.md", "line": "repo head abc1234 commit",
                                            "line_index": 1, "stale": True, "auto_apply": True}],
                    "broken_links": [], "unsorted_sessions": [],
                    "re_attribution": {"items": [], "calibration_sample": []}}
            result = vs.apply_plan(vault, plan)
            self.assertTrue(result.get("ok"), msg=result)
            after_apply = note.read_bytes()
            self.assertGreater(after_apply.count(b"\r\n"), 0, "CRLF line endings must be preserved")
            sha = result["sha"]
            reset = subprocess.run(
                ["git", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false", "reset", "--hard", sha],
                cwd=str(vault), env=env, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(reset.returncode, 0, msg=reset.stderr)
            after_reset = note.read_bytes()
            self.assertEqual(before, after_reset, "reset to snapshot must restore exact bytes")
            restored = {p.relative_to(vault).as_posix(): p.read_bytes() for p in file_paths}
            self.assertEqual(snapshot_bytes, restored, "reset to snapshot must restore every tracked file byte for byte")
        finally:
            tmp.cleanup()

    def test_crlf_stale_index_stat_snapshot_reset_restores_bytes(self):
        tmp = _Tmp(ROOT / ".audit" / "tmp")
        try:
            vault = self._build_minimal_git_vault(tmp.path)
            env = dict(os.environ)
            env["GIT_CONFIG_NOSYSTEM"] = "1"
            config = subprocess.run(
                ["git", "config", "core.autocrlf", "true"], cwd=vault, env=env,
                capture_output=True, timeout=10,
            )
            self.assertEqual(config.returncode, 0, msg=config.stderr)
            note = vault / "1-Projects" / "parent" / "parent.md"
            before = note.read_bytes().replace(b"\n", b"\r\n")
            note.write_bytes(before)
            stage = subprocess.run(
                ["git", "-c", "core.autocrlf=true", "add", "--", "1-Projects/parent/parent.md"], cwd=vault,
                env=env, capture_output=True, timeout=10,
            )
            self.assertEqual(stage.returncode, 0, msg=stage.stderr)
            committed_blob = subprocess.run(
                ["git", "rev-parse", "HEAD:1-Projects/parent/parent.md"], cwd=vault,
                env=env, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(committed_blob.returncode, 0, msg=committed_blob.stderr)
            indexed_blob = subprocess.run(
                ["git", "rev-parse", ":1-Projects/parent/parent.md"], cwd=vault,
                env=env, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(indexed_blob.returncode, 0, msg=indexed_blob.stderr)
            self.assertEqual(committed_blob.stdout.strip(), indexed_blob.stdout.strip())
            clean = subprocess.run(
                ["git", "diff-files", "--quiet", "--", "1-Projects/parent/parent.md"],
                cwd=vault, env=env, capture_output=True, timeout=10,
            )
            self.assertEqual(clean.returncode, 0, msg=clean.stderr)
            self.assertEqual(note.read_bytes(), before)
            other = vault / "0-Sessions" / "s.md"
            other.write_bytes(b"changed to create the sweep snapshot\n")
            sha = _load_sweep()._snapshot(vault, "crlf-test")
            reset = subprocess.run(
                ["git", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false",
                 "reset", "--hard", sha], cwd=vault, env=env,
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(reset.returncode, 0, msg=reset.stderr)
            self.assertEqual(note.read_bytes(), before, "reset to snapshot must restore exact CRLF bytes")
        finally:
            tmp.cleanup()

    def test_snapshot_initializes_fresh_vault_repo_without_head(self):
        tmp = _Tmp(ROOT / ".audit" / "tmp")
        try:
            vault = tmp.path / "fresh-vault"
            (vault / "1-Projects" / "new-project").mkdir(parents=True)
            note = vault / "1-Projects" / "new-project" / "new-project.md"
            note.write_bytes(b"# Fresh vault\r\n")
            env = dict(os.environ)
            env["GIT_CONFIG_NOSYSTEM"] = "1"
            init = subprocess.run(
                ["git", "init", "-q", "-b", "main"], cwd=vault, env=env,
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(init.returncode, 0, msg=init.stderr)

            sha = _load_sweep()._snapshot(vault, "fresh-vault")
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=vault, env=env,
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(head.returncode, 0, msg=head.stderr)
            self.assertEqual(head.stdout.strip(), sha)
            self.assertEqual(note.read_bytes(), b"# Fresh vault\r\n")
        finally:
            tmp.cleanup()

    def test_projects_json_merges_roots_and_aliases(self):
        tmp = _Tmp(ROOT / ".audit" / "tmp")
        try:
            vault = self._build_minimal_git_vault(tmp.path)
            vs = _load_sweep()
            plan = {"vault_path": str(vault),
                    "fragments": [{"folder": "frag", "proposed_parent": "parent", "auto_apply": True}],
                    "leftover_folders": [], "stale_state_lines": [], "broken_links": [],
                    "unsorted_sessions": [], "re_attribution": {"items": [], "calibration_sample": []}}
            result = vs.apply_plan(vault, plan)
            self.assertTrue(result.get("ok"), msg=result)
            data = json.loads((vault / "_meta" / "projects.json").read_text(encoding="utf-8"))
            self.assertEqual(len(data["projects"]), 1)
            parent = data["projects"][0]
            self.assertEqual(parent["name"], "Parent")
            self.assertIn("C:\\r\\frag", parent["roots"])
            self.assertIn("fragalias", parent["aliases"])
        finally:
            tmp.cleanup()

    def test_missing_parent_projects_entry_leaves_fragment_unchanged(self):
        tmp = _Tmp(ROOT / ".audit" / "tmp")
        try:
            vault = self._build_minimal_git_vault(tmp.path)
            pjson = vault / "_meta" / "projects.json"
            original = json.loads(pjson.read_text(encoding="utf-8"))
            original["projects"] = [entry for entry in original["projects"] if entry["name"].lower() != "parent"]
            pjson.write_text(json.dumps(original, indent=2) + "\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=str(vault), capture_output=True, timeout=10)
            subprocess.run(["git", "commit", "-q", "-m", "remove parent registry entry"], cwd=str(vault), capture_output=True, timeout=10)
            before = pjson.read_bytes()
            vs = _load_sweep()
            plan = {"vault_path": str(vault),
                    "fragments": [{"folder": "frag", "proposed_parent": "parent", "auto_apply": True}],
                    "leftover_folders": [], "stale_state_lines": [], "broken_links": [],
                    "unsorted_sessions": [], "re_attribution": {"items": [], "calibration_sample": []}}
            result = vs.apply_plan(vault, plan)
            item = next(e for e in result["log_entries"] if e.get("action") == "fragment_merge")
            self.assertEqual(item["status"], "error")
            self.assertIn("parent project not found", item["reason"])
            self.assertEqual(before, pjson.read_bytes())
            self.assertTrue((vault / "1-Projects" / "frag").exists())
        finally:
            tmp.cleanup()

    def test_stale_line_rerun_is_byte_for_byte_noop(self):
        tmp = _Tmp(ROOT / ".audit" / "tmp")
        try:
            vault = self._build_minimal_git_vault(tmp.path)
            note = vault / "1-Projects" / "parent" / "parent.md"
            note.write_bytes(
                b"# P\nrepo head abc1234 commit\n" + note.read_bytes().split(b"\n", 1)[1]
            )
            vs = _load_sweep()
            plan = {"vault_path": str(vault), "fragments": [], "leftover_folders": [],
                    "stale_state_lines": [{"note": "1-Projects/parent/parent.md", "line": "repo head abc1234 commit",
                                            "line_index": 1, "stale": True, "auto_apply": True}],
                    "broken_links": [], "unsorted_sessions": [],
                    "re_attribution": {"items": [], "calibration_sample": []}}
            r1 = vs.apply_plan(vault, plan)
            self.assertTrue(r1.get("ok"), msg=r1)
            after_first = note.read_bytes()
            r2 = vs.apply_plan(vault, plan)
            self.assertTrue(r2.get("ok"), msg=r2)
            after_second = note.read_bytes()
            self.assertEqual(after_first, after_second)
        finally:
            tmp.cleanup()

    def test_fragment_creates_pending_block_when_missing(self):
        tmp = _Tmp(ROOT / ".audit" / "tmp")
        try:
            vault = self._build_minimal_git_vault(tmp.path)
            note = vault / "1-Projects" / "parent" / "parent.md"
            note.write_bytes(b"# P no blocks\n")
            subprocess.run(["git", "add", "-A"], cwd=str(vault), capture_output=True, timeout=10)
            subprocess.run(["git", "commit", "-q", "-m", "noblocks"], cwd=str(vault), capture_output=True, timeout=10)
            vs = _load_sweep()
            plan = {"vault_path": str(vault),
                    "fragments": [{"folder": "frag", "proposed_parent": "parent", "auto_apply": True}],
                    "leftover_folders": [], "stale_state_lines": [], "broken_links": [],
                    "unsorted_sessions": [], "re_attribution": {"items": [], "calibration_sample": []}}
            result = vs.apply_plan(vault, plan)
            self.assertTrue(result.get("ok"), msg=result)
            text = note.read_text(encoding="utf-8")
            self.assertIn("<!-- agentmem:pending:begin -->", text)
            self.assertIn("Merged from frag", text)
            self.assertFalse((vault / "1-Projects" / "frag").exists())
        finally:
            tmp.cleanup()

    def test_fragment_rerun_after_move_failure_does_not_duplicate_pending(self):
        from unittest.mock import patch
        tmp = _Tmp(ROOT / ".audit" / "tmp")
        try:
            vault = self._build_minimal_git_vault(tmp.path)
            vs = _load_sweep()
            plan = {"vault_path": str(vault),
                    "fragments": [{"folder": "frag", "proposed_parent": "parent", "auto_apply": True}],
                    "leftover_folders": [], "stale_state_lines": [], "broken_links": [],
                    "unsorted_sessions": [], "re_attribution": {"items": [], "calibration_sample": []}}
            with patch.object(vs.os, "rename", side_effect=OSError("simulated move failure")):
                failed = vs.apply_plan(vault, plan)
            failed_item = next(e for e in failed["log_entries"] if e.get("action") == "fragment_merge")
            self.assertEqual(failed_item["status"], "error")
            succeeded = vs.apply_plan(vault, plan)
            self.assertTrue(succeeded.get("ok"), msg=succeeded)
            parent = (vault / "1-Projects" / "parent" / "parent.md").read_bytes()
            self.assertEqual(parent.count(b"### Merged from frag"), 1)
            self.assertIn(b"frag state", parent)
            self.assertFalse((vault / "1-Projects" / "frag").exists())
        finally:
            tmp.cleanup()

    def test_symlink_into_sessions_is_refused(self):
        tmp = _Tmp(ROOT / ".audit" / "tmp")
        try:
            vault = self._build_minimal_git_vault(tmp.path)
            vs = _load_sweep()
            plan = {"vault_path": str(vault),
                    "fragments": [{"folder": "../0-Sessions", "proposed_parent": "parent", "auto_apply": True}],
                    "leftover_folders": [], "stale_state_lines": [], "broken_links": [],
                    "unsorted_sessions": [], "re_attribution": {"items": [], "calibration_sample": []}}
            result = vs.apply_plan(vault, plan)
            item = next(e for e in result["log_entries"] if e.get("action") == "fragment_merge")
            self.assertEqual(item["status"], "error")
            self.assertIn("refused", item["reason"])
        finally:
            tmp.cleanup()

    def test_log_has_timestamp_file_and_sha_in_every_entry(self):
        tmp = _Tmp(ROOT / ".audit" / "tmp")
        try:
            vault = self._build_minimal_git_vault(tmp.path)
            vs = _load_sweep()
            plan = {"vault_path": str(vault),
                    "fragments": [{"folder": "frag", "proposed_parent": "parent", "auto_apply": True}],
                    "leftover_folders": [], "stale_state_lines": [], "broken_links": [],
                    "unsorted_sessions": [], "re_attribution": {"items": [], "calibration_sample": []}}
            r1 = vs.apply_plan(vault, plan)
            self.assertTrue(r1.get("ok"), msg=r1)
            r2 = vs.apply_plan(vault, plan)
            self.assertTrue(r2.get("ok"), msg=r2)
            logs = list((vault / "_meta" / "logs").glob("sweep-*.jsonl"))
            self.assertEqual(len(logs), 2, "each run should create a separate log file")
            for log_path in logs:
                for line in log_path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    entry = json.loads(line)
                    self.assertIn("sha", entry)
        finally:
            tmp.cleanup()

    def test_path_safety_refuses_unsafe_items(self):
        tmp = _Tmp(ROOT / ".audit" / "tmp")
        try:
            vault = self._build_minimal_git_vault(tmp.path)
            bad_session = vault / "0-Sessions" / "evil.md"
            bad_session.write_bytes(b"evil\n")
            vs = _load_sweep()
            plan = {"vault_path": str(vault),
                    "fragments": [{"folder": "../outside", "proposed_parent": "parent", "auto_apply": True}],
                    "leftover_folders": [{"folder": "0-Sessions", "auto_apply": True}],
                    "stale_state_lines": [{"note": "0-Sessions/evil.md", "line": "evil",
                                            "stale": True, "auto_apply": True}],
                    "broken_links": [], "unsorted_sessions": [],
                    "re_attribution": {"items": [], "calibration_sample": []}}
            result = vs.apply_plan(vault, plan)
            self.assertTrue(result.get("ok"), msg=result)
            entries = {e.get("action"): e for e in result.get("log_entries", [])}
            self.assertEqual(entries.get("fragment_merge", {}).get("status"), "error")
            self.assertIn("refused", entries.get("fragment_merge", {}).get("reason", ""))
            self.assertEqual(entries.get("leftover_archive", {}).get("status"), "error")
            self.assertIn("refused", entries.get("leftover_archive", {}).get("reason", ""))
            self.assertEqual(entries.get("stale_line", {}).get("status"), "error")
            self.assertIn("refused", entries.get("stale_line", {}).get("reason", ""))
            self.assertTrue(bad_session.exists(), "0-Sessions file must not be touched")
        finally:
            tmp.cleanup()

    def test_r2_restore_backup_preserves_embedded_repo_ignored_and_crlf_bytes(self):
        tmp = _Tmp(ROOT / ".audit" / "tmp")
        try:
            vault = self._build_minimal_git_vault(tmp.path)
            env = dict(os.environ)
            env["GIT_CONFIG_NOSYSTEM"] = "1"
            subprocess.run(["git", "config", "core.autocrlf", "true"], cwd=vault, env=env, check=True)
            parent_note = vault / "1-Projects" / "parent" / "parent.md"
            parent_note.write_bytes(
                b"# P\r\n<!-- agentmem:state:begin -->\r\nrepo head abc1234 commit\r\n"
                b"<!-- agentmem:state:end -->\r\n<!-- agentmem:pending:begin -->\r\n- old todo\r\n"
                b"<!-- agentmem:pending:end -->\r\n"
            )
            subprocess.run(["git", "-c", "core.autocrlf=false", "add", "-A"], cwd=vault, env=env, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "crlf source"], cwd=vault, env=env, check=True)
            frag = vault / "1-Projects" / "frag"
            subprocess.run(["git", "init", "-q"], cwd=frag, env=env, check=True)
            subprocess.run(["git", "config", "user.email", "inner@test"], cwd=frag, env=env, check=True)
            subprocess.run(["git", "config", "user.name", "Inner"], cwd=frag, env=env, check=True)
            (frag / ".gitignore").write_bytes(b"*.local\n")
            (frag / "inner.txt").write_bytes(b"committed inner bytes\r\n")
            subprocess.run(["git", "add", "-A"], cwd=frag, env=env, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "inner"], cwd=frag, env=env, check=True)
            (frag / "inner.txt").write_bytes(b"uncommitted inner bytes\xfe\r\n")
            ignored = frag / "secret.local"
            ignored.write_bytes(b"ignored precious bytes\xff\r\n")
            before = {p: b for p, b in _snapshot_no_git(vault).items()
                      if f"{os.sep}_meta{os.sep}logs{os.sep}" not in p}
            inner_git_before = {
                str(Path(root) / name).removeprefix(str(frag) + os.sep): (Path(root) / name).read_bytes()
                for root, _, names in os.walk(frag)
                for name in names
            }
            self.assertGreater(parent_note.read_bytes().count(b"\r\n"), 0)
            plan = {"vault_path": str(vault),
                    "fragments": [{"folder": "frag", "proposed_parent": "parent", "auto_apply": True}],
                    "leftover_folders": [],
                    "stale_state_lines": [{"note": "1-Projects/parent/parent.md", "line": "repo head abc1234 commit",
                                            "line_index": 1, "stale": True, "auto_apply": True}],
                    "broken_links": [], "unsorted_sessions": [], "re_attribution": {"items": [], "calibration_sample": []}}
            vs = _load_sweep()
            stdout = io.StringIO()
            from unittest.mock import patch
            with patch("sys.stdout", stdout):
                result = vs.apply_plan(vault, plan)
            self.assertTrue(result.get("ok"), msg=result)
            backup = Path(result["backup_dir"])
            self.assertIn(str(backup), stdout.getvalue())
            self.assertTrue((backup / "manifest.json").is_file())
            backup_record = next(e for e in result["log_entries"] if e.get("action") == "backup")
            self.assertEqual(backup_record["path"], str(backup))
            self.assertTrue(ignored.exists() is False)
            restore = _run_sweep("restore", "--backup", str(backup), "--vault", str(vault))
            self.assertEqual(restore.returncode, 0, msg=restore.stderr)
            after = {p: b for p, b in _snapshot_no_git(vault).items()
                     if f"{os.sep}_meta{os.sep}logs{os.sep}" not in p}
            self.assertEqual(before, after)
            inner_git_after = {
                str(Path(root) / name).removeprefix(str(vault / "1-Projects" / "frag") + os.sep):
                    (Path(root) / name).read_bytes()
                for root, _, names in os.walk(vault / "1-Projects" / "frag")
                for name in names
            }
            self.assertEqual(inner_git_before, inner_git_after)
            self.assertEqual((vault / "1-Projects" / "frag" / "inner.txt").read_bytes(),
                             b"uncommitted inner bytes\xfe\r\n")
            self.assertEqual(b"ignored precious bytes\xff\r\n", ignored.read_bytes())
            self.assertGreater(parent_note.read_bytes().count(b"\r\n"), 0)
        finally:
            tmp.cleanup()

    def test_restore_keeps_changes_made_after_the_sweep(self):
        tmp = _Tmp(ROOT / ".audit" / "tmp")
        try:
            vault = self._build_minimal_git_vault(tmp.path)
            plan = {"vault_path": str(vault),
                    "fragments": [{"folder": "frag", "proposed_parent": "parent", "auto_apply": True}],
                    "leftover_folders": [], "stale_state_lines": [], "broken_links": [], "unsorted_sessions": [],
                    "re_attribution": {"items": [], "calibration_sample": []}}
            vs = _load_sweep()
            from unittest.mock import patch
            with patch("sys.stdout", io.StringIO()):
                result = vs.apply_plan(vault, plan)
            self.assertTrue(result.get("ok"), msg=result)
            backup = Path(result["backup_dir"])
            parent_note = vault / "1-Projects" / "parent" / "parent.md"
            later = parent_note.read_bytes() + b"\n- captured after the sweep\n"
            parent_note.write_bytes(later)
            restore = _run_sweep("restore", "--backup", str(backup), "--vault", str(vault))
            self.assertEqual(restore.returncode, 0, msg=restore.stderr)
            kept = list(backup.parent.glob(f"{backup.name}-pre-restore-*/1-Projects/parent/parent.md"))
            self.assertEqual(len(kept), 1)
            self.assertEqual(kept[0].read_bytes(), later)
        finally:
            tmp.cleanup()

    def test_r2_backup_verification_failure_aborts_before_vault_changes(self):
        from unittest.mock import patch
        tmp = _Tmp(ROOT / ".audit" / "tmp")
        try:
            vault = self._build_minimal_git_vault(tmp.path)
            before = _snapshot_no_git(vault)
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=vault, capture_output=True,
                                  text=True, check=True).stdout.strip()
            vs = _load_sweep()
            copy_path = vs._copy_path

            def corrupt_copy(source, target):
                copy_path(source, target)
                if target.is_file():
                    target.write_bytes(target.read_bytes() + b"corrupt")

            plan = {"fragments": [], "leftover_folders": [],
                    "stale_state_lines": [{"note": "1-Projects/parent/parent.md", "line": "repo head abc1234 commit",
                                            "line_index": 1, "stale": True}]}
            with patch.object(vs, "_copy_path", side_effect=corrupt_copy):
                result = vs.apply_plan(vault, plan)
            self.assertFalse(result.get("ok"), msg=result)
            self.assertIn("backup verification mismatch", result.get("error", ""))
            self.assertEqual(before, _snapshot_no_git(vault))
            after_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=vault, capture_output=True,
                                        text=True, check=True).stdout.strip()
            self.assertEqual(head, after_head)
            self.assertFalse((vault / "_meta" / "PAUSE").exists())
            self.assertFalse((vault / "_meta" / "logs").exists())
            self.assertFalse(list(vault.parent.glob(f"{vault.name}-sweep-backup-*")))
        finally:
            tmp.cleanup()

    def test_r2_failed_rename_leaves_fragment_and_related_files_unchanged(self):
        from unittest.mock import patch
        tmp = _Tmp(ROOT / ".audit" / "tmp")
        try:
            vault = self._build_minimal_git_vault(tmp.path)
            pjson = (vault / "_meta" / "projects.json").read_bytes()
            parent_note = (vault / "1-Projects" / "parent" / "parent.md").read_bytes()
            fragment_files = _snapshot_no_git(vault / "1-Projects" / "frag")
            plan = {"vault_path": str(vault),
                    "fragments": [{"folder": "frag", "proposed_parent": "parent", "auto_apply": True}],
                    "leftover_folders": [], "stale_state_lines": [], "broken_links": [],
                    "unsorted_sessions": [], "re_attribution": {"items": [], "calibration_sample": []}}
            vs = _load_sweep()
            with patch.object(vs.os, "rename", side_effect=OSError("rename blocked")):
                result = vs.apply_plan(vault, plan)
            item = next(e for e in result["log_entries"] if e.get("action") == "fragment_merge")
            self.assertEqual(item["status"], "error")
            self.assertIn("rename blocked", item["reason"])
            self.assertTrue((vault / "1-Projects" / "frag").is_dir())
            self.assertEqual((vault / "_meta" / "projects.json").read_bytes(), pjson)
            self.assertEqual((vault / "1-Projects" / "parent" / "parent.md").read_bytes(), parent_note)
            self.assertEqual(_snapshot_no_git(vault / "1-Projects" / "frag"), fragment_files)
            self.assertFalse((vault / "4-Archive" / "merged" / "frag").exists())
        finally:
            tmp.cleanup()

    def test_r2_rejects_self_and_non_child_fragment_and_leftover_names(self):
        tmp = _Tmp(ROOT / ".audit" / "tmp")
        try:
            vault = self._build_minimal_git_vault(tmp.path)
            vs = _load_sweep()
            pjson = (vault / "_meta" / "projects.json").read_bytes()
            parent_note = (vault / "1-Projects" / "parent" / "parent.md").read_bytes()
            for fragment, parent in (("parent", "parent"), ("parent", "PARENT"), (".", "parent"),
                                     ("..", "parent"), ("frag/..", "parent"), ("frag\\..", "parent"),
                                     ("frag", "."), ("frag", ".."), ("frag", "parent/..")):
                result = vs.apply_plan(vault, {"fragments": [{"folder": fragment, "proposed_parent": parent}],
                                                "leftover_folders": [], "stale_state_lines": []})
                item = next(e for e in result["log_entries"] if e.get("action") == "fragment_merge")
                self.assertEqual(item["status"], "error", (fragment, parent, item))
                self.assertIn("refused", item["reason"])
            for folder in (".", "..", "Projects-Old/..", "bad\\name"):
                result = vs.apply_plan(vault, {"fragments": [], "leftover_folders": [{"folder": folder}],
                                                "stale_state_lines": []})
                item = next(e for e in result["log_entries"] if e.get("action") == "leftover_archive")
                self.assertEqual(item["status"], "error", (folder, item))
                self.assertIn("refused", item["reason"])
            self.assertEqual((vault / "_meta" / "projects.json").read_bytes(), pjson)
            self.assertEqual((vault / "1-Projects" / "parent" / "parent.md").read_bytes(), parent_note)
        finally:
            tmp.cleanup()

    def test_r2_missing_parent_note_refuses_fragment_without_project_or_file_edits(self):
        tmp = _Tmp(ROOT / ".audit" / "tmp")
        try:
            vault = self._build_minimal_git_vault(tmp.path)
            parent_note = vault / "1-Projects" / "parent" / "parent.md"
            parent_note.unlink()
            before = (vault / "_meta" / "projects.json").read_bytes()
            result = _load_sweep().apply_plan(vault, {
                "fragments": [{"folder": "frag", "proposed_parent": "parent"}],
                "leftover_folders": [], "stale_state_lines": []})
            item = next(e for e in result["log_entries"] if e.get("action") == "fragment_merge")
            self.assertEqual(item["status"], "error")
            self.assertIn("parent note not found", item["reason"])
            self.assertTrue((vault / "1-Projects" / "frag").is_dir())
            self.assertEqual((vault / "_meta" / "projects.json").read_bytes(), before)
            self.assertEqual((vault / "1-Projects" / "frag" / "frag.md").read_bytes(), b"# F\n<!-- agentmem:state:begin -->\nfrag state\n<!-- agentmem:state:end -->\n<!-- agentmem:pending:begin -->\nfrag pending\n<!-- agentmem:pending:end -->\n")
        finally:
            tmp.cleanup()

    def test_r2_stale_duplicate_lines_use_plan_index_and_exact_text(self):
        tmp = _Tmp(ROOT / ".audit" / "tmp")
        try:
            vault = self._build_minimal_git_vault(tmp.path)
            note = vault / "1-Projects" / "parent" / "parent.md"
            note.write_bytes(
                b"<!-- agentmem:state:begin -->\nrepo head abc1234 commit\n repo head abc1234 commit\n"
                b"<!-- agentmem:state:end -->\n"
            )
            vs = _load_sweep()
            generated = vs._scan_stale(vault, {})
            target = next(item for item in generated if item["line"] == " repo head abc1234 commit")
            self.assertEqual(target["line_index"], 2)
            target["stale"] = True
            plan = {"stale_state_lines": [{"note": "1-Projects/parent/parent.md", "line": " repo head abc1234 commit",
                                            "line_index": target["line_index"], "stale": True}]}
            first = vs.apply_plan(vault, plan)
            stale_result = next(e for e in first["log_entries"] if e.get("action") == "stale_line")
            self.assertEqual(stale_result["status"], "ok")
            expected = (b"<!-- agentmem:state:begin -->\nrepo head abc1234 commit\n"
                        b" (as of unknown commit) repo head abc1234 commit\n<!-- agentmem:state:end -->\n")
            self.assertEqual(note.read_bytes(), expected)
            after_first = note.read_bytes()
            second = vs.apply_plan(vault, plan)
            stale_result = next(e for e in second["log_entries"] if e.get("action") == "stale_line")
            self.assertEqual(stale_result["status"], "skip")
            self.assertEqual(note.read_bytes(), after_first)
        finally:
            tmp.cleanup()

    def test_r2_root_leftover_folder_is_archived(self):
        tmp = _Tmp(ROOT / ".audit" / "tmp")
        try:
            vault = self._build_minimal_git_vault(tmp.path)
            old = vault / "Projects-Old"
            old.mkdir()
            (old / "legacy.txt").write_bytes(b"archive me\xff")
            plan = {"fragments": [], "leftover_folders": [{"folder": "Projects-Old", "auto_apply": True}],
                    "stale_state_lines": []}
            result = _load_sweep().apply_plan(vault, plan)
            item = next(e for e in result["log_entries"] if e.get("action") == "leftover_archive")
            self.assertEqual(item["status"], "ok", msg=item)
            self.assertFalse(old.exists())
            self.assertEqual((vault / item["dest"] / "legacy.txt").read_bytes(), b"archive me\xff")
        finally:
            tmp.cleanup()

    def test_non_utf8_note_does_not_crash_and_cleans_up(self):
        tmp = _Tmp(ROOT / ".audit" / "tmp")
        try:
            vault = self._build_minimal_git_vault(tmp.path)
            note = vault / "1-Projects" / "parent" / "parent.md"
            note.write_bytes(note.read_bytes() + b"\xff\xfe latin\n")
            subprocess.run(["git", "add", "-A"], cwd=str(vault), capture_output=True, timeout=10)
            subprocess.run(["git", "commit", "-q", "-m", "nonutf8"], cwd=str(vault), capture_output=True, timeout=10)
            vs = _load_sweep()
            plan = {"vault_path": str(vault), "fragments": [], "leftover_folders": [],
                    "stale_state_lines": [{"note": "1-Projects/parent/parent.md", "line": "repo head abc1234 commit",
                                            "line_index": 1, "stale": True, "auto_apply": True}],
                    "broken_links": [], "unsorted_sessions": [],
                    "re_attribution": {"items": [], "calibration_sample": []}}
            result = vs.apply_plan(vault, plan)
            self.assertTrue(result.get("ok"), msg=result)
            self.assertFalse((vault / "_meta" / "PAUSE").exists())
            logs = list((vault / "_meta" / "logs").glob("sweep-*.jsonl"))
            self.assertTrue(logs)
            self.assertIn(b"\xff\xfe latin\n", note.read_bytes())
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
