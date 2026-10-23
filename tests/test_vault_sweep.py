"""WP-S1 vault sweep dry-run plan, tested on a synthetic vault only."""
import importlib.util
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
        f"- repository HEAD: {lib_head}\n\n"
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
        self.assertIn("abc1234", stale[0]["line"])
        self.assertTrue(stale[0]["stale"])
        self.assertIsNotNone(stale[0]["current_head"])
        self.assertTrue(stale[0]["auto_apply"])
        fresh = [i for i in plan["stale_state_lines"] if "my-lib" in i["note"]]
        self.assertTrue(len(fresh) >= 1)
        self.assertFalse(fresh[0]["stale"])

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


if __name__ == "__main__":
    unittest.main()
