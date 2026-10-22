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
    for d in ("_meta", "1-Projects/demo-app", "1-Projects/my-lib",
              "0-Sessions/2026/01", "Projects-Old"):
        (vault / d).mkdir(parents=True, exist_ok=True)
    shutil.copy2(FIXTURES / "_meta" / "projects.json", vault / "_meta" / "projects.json")
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

    def test_f_re_attribution_empty_with_todo(self):
        plan = self._plan()
        ra = plan["re_attribution"]
        self.assertEqual(ra["items"], [])
        self.assertIn("TODO", ra["note"])
        self.assertIn("A-5", ra["note"])

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


if __name__ == "__main__":
    unittest.main()
