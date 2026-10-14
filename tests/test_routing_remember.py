"""Automatic project routing (file location / first prompt) and the /remember + /obsidian paths."""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentmem import notes, projects, remember, worker  # noqa: E402
from agentmem.manifest import Manifest  # noqa: E402
import fixtures as fx  # noqa: E402
from test_acceptance import Base  # noqa: E402

HOME = str(Path.home())


class Routing(Base):
    def setUp(self):
        super().setUp()
        other = self.tmp / "work" / "other-tool"
        other.mkdir(parents=True)
        data = json.loads(self.cfg.projects_map.read_text(encoding="utf-8"))
        data["projects"].append({"name": "Other Tool", "roots": [str(other)], "status": "active"})
        self.cfg.projects_map.write_text(json.dumps(data), encoding="utf-8")
        self.other = str(other)

    def r(self, cwd, files=(), text=""):
        return projects.resolve_session(cwd, self.cfg.projects_map, files=files, text=text, create=False)

    def test_explicit_cwd_wins_over_prompt(self):
        self.assertEqual(self.r(self.cwd, text="also look at Other Tool"), "Demo App")

    def test_generic_cwd_uses_edited_file_locations(self):
        self.assertEqual(self.r(HOME, files=[f"{self.cwd}\\db.py", f"{self.cwd}\\api.py", f"{self.other}\\x.py"]), "Demo App")

    def test_generic_cwd_uses_paths_in_first_prompt(self):
        self.assertEqual(self.r(HOME, text=f"Fix the failing test in {self.other}\\tests\\test_x.py please"), "Other Tool")

    def test_generic_cwd_uses_project_names_in_first_prompt(self):
        self.assertEqual(self.r(HOME, text="In the demo-app repo, add pagination"), "Demo App")
        self.assertEqual(self.r(HOME, text="Ship the Other Tool release notes"), "Other Tool")

    def test_ambiguous_or_unknown_falls_back(self):
        self.assertEqual(self.r(HOME, text="compare Demo App with Other Tool"), projects.ENVIRONMENT)
        self.assertEqual(self.r(str(projects.PROJECTS_ROOT), text="what time is it"), projects.UNSORTED)
        self.assertEqual(self.r(str(projects.CODEX_SCRATCH / "2026-10-14" / "some-task"), text="refactor other-tool"), "Other Tool")

    def test_pipeline_routes_generic_cwd_session_by_files(self):
        sid = "s-route"
        path = self.tmp / "claude" / f"{sid}.jsonl"
        lines = fx.claude_lines(sid, HOME, fx.SUBSTANTIAL)
        for l in lines:  # edits happen in the Demo App repo while the agent sat in the home folder
            for b in (l.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("name") == "Edit":
                    b["input"]["file_path"] = b["input"]["file_path"].replace(HOME, self.cwd)
        fx.write_jsonl(path, lines)
        worker.enqueue(self.cfg, "claude", sid, event="end", source=str(path), cwd=HOME)
        self.run_worker()
        self.assertEqual(self.man.get("claude", sid)["project"], "Demo App")

    def test_resumed_part_keeps_first_project(self):
        src = self.claude_session("s-keep", fx.SUBSTANTIAL, 120)
        self.end("claude", "s-keep", src)
        self.run_worker()
        fx.write_jsonl(src, fx.claude_lines("s-keep", self.other, fx.SWITCH, 10), append=True)
        self.end("claude", "s-keep", src)
        self.run_worker()
        rows = self.man.db.execute("SELECT part, project FROM captures WHERE session_id='s-keep' ORDER BY part").fetchall()
        self.assertEqual([tuple(r) for r in rows], [(1, "Demo App"), (2, "Demo App")])


class Remember(Base):
    def test_remember_routes_and_reconciles(self):
        res = remember.remember_item(self.cfg, "Deploy target is Fly.io, region sin", kind="decision", cwd=self.cwd,
                                     agent="claude", rationale="closest region", consolidator=fx.fake_consolidate)
        self.assertEqual(res["project"], "Demo App")
        self.assertTrue(res["consolidated"])
        state = notes.get_block(self.project_note(), notes.STATE_BEGIN, notes.STATE_END)
        self.assertIn("Fly.io", state)
        ev = Path(res["evidence"])
        self.assertTrue(ev.read_text(encoding="utf-8").startswith("---\ntype: memory"))
        self.assertFalse(remember.create_once(ev, "overwrite"))
        self.assertEqual(self.man.query_captures(project="Demo App")[0]["summarizer"], "remember")

    def test_remember_by_prompt_content_from_generic_cwd(self):
        res = remember.remember_item(self.cfg, f"The demo-app uses port 8443 in {self.cwd}", cwd=HOME,
                                     consolidator=fx.fake_consolidate)
        self.assertEqual(res["project"], "Demo App")

    def test_remember_without_project_goes_to_resources(self):
        res = remember.remember_item(self.cfg, "I prefer concise replies", kind="preference",
                                     cwd=str(projects.PROJECTS_ROOT), consolidator=fx.fake_consolidate)
        self.assertEqual(res["project"], projects.UNSORTED)
        text = self.cfg.vault.joinpath("3-Resources", "Remembered.md").read_text(encoding="utf-8")
        self.assertIn("I prefer concise replies", text)

    def test_remember_redacts_and_survives_model_failure(self):
        res = remember.remember_item(self.cfg, f"API key is {fx.SECRET_SAMPLES[0]} for staging", cwd=self.cwd,
                                     consolidator=fx.failing_llm)
        self.assertFalse(res["consolidated"])
        for p in self.cfg.vault.rglob("*.md"):
            self.assertNotIn(fx.SECRET_SAMPLES[0], p.read_text(encoding="utf-8"))
        self.assertIn("fact:", notes.get_block(self.project_note(), notes.PENDING_BEGIN, notes.PENDING_END))

    def test_current_session_from_claude_env(self):
        fake_home = self.tmp / "home"
        t = fake_home / ".claude" / "projects" / "slug" / "abc-123.jsonl"
        fx.write_jsonl(t, fx.claude_lines("abc-123", self.cwd, fx.SUBSTANTIAL))
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "abc-123"}), \
                mock.patch.object(remember.Path, "home", return_value=fake_home):
            self.assertEqual(remember.current_session(), ("claude", "abc-123", str(t)))


if __name__ == "__main__":
    unittest.main()
