"""The 12 required acceptance scenarios, plus safeguards. Run: python -I -m unittest discover -s tests -v"""
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentmem import adapters, fsutil, hook, notes, pipeline, worker  # noqa: E402
from agentmem.manifest import Manifest  # noqa: E402
from agentmem.redact import contains_secret, redact  # noqa: E402
from agentmem.search import fallback_search  # noqa: E402
import fixtures as fx  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = fx.tmpdir()
        self.tmp = Path(self._tmp.name)
        self.cfg = fx.make_vault(self.tmp)
        self.cwd = str(self.tmp / "work" / "demo-app")
        self.man = Manifest(self.cfg.manifest_path)

    def tearDown(self):
        self.man.close()
        self._tmp.cleanup()

    def claude_session(self, sid, turns, minutes_ago=30):
        path = self.tmp / "claude" / f"{sid}.jsonl"
        fx.write_jsonl(path, fx.claude_lines(sid, self.cwd, turns, minutes_ago))
        return path

    def end(self, agent, sid, source):
        worker.enqueue(self.cfg, agent, sid, event="end", source=str(source), cwd=self.cwd)

    def run_worker(self, summarizer=fx.fake_summarize, consolidator=fx.fake_consolidate):
        return worker.run(self.cfg, summarizer=summarizer, consolidator=consolidator)

    def session_notes(self):
        return sorted(self.cfg.sessions_dir.rglob("*.md"))

    def project_note(self):
        return (self.cfg.projects_dir / "Demo App" / "Demo App.md").read_text(encoding="utf-8")


class Acceptance(Base):
    def test_01_captured_exactly_once(self):
        src = self.claude_session("s-once", fx.SUBSTANTIAL)
        self.end("claude", "s-once", src)
        self.run_worker()
        self.end("claude", "s-once", src)  # a duplicate SessionEnd delivery
        self.run_worker()
        self.run_worker()
        self.assertEqual(len(self.session_notes()), 1)
        self.assertEqual(self.man.db.execute("SELECT count(*) FROM captures").fetchone()[0], 1)
        self.assertEqual(self.man.get("claude", "s-once")["status"], "captured")

    def test_02_trivial_session_skipped(self):
        src = self.claude_session("s-trivial", fx.TRIVIAL)
        self.end("claude", "s-trivial", src)
        self.run_worker()
        self.assertEqual(self.session_notes(), [])
        row = self.man.get("claude", "s-trivial")
        self.assertEqual(row["status"], "skipped")
        self.assertIn("trivial", row["skip_reason"])

    def test_03_resumed_session_no_duplicates(self):
        src = self.claude_session("s-resume", fx.SUBSTANTIAL)
        self.end("claude", "s-resume", src)
        self.run_worker()
        first = self.session_notes()
        fx.write_jsonl(src, fx.claude_lines("s-resume", self.cwd, fx.TRIVIAL, 5), append=True)  # trivial resume
        self.end("claude", "s-resume", src)
        self.run_worker()
        self.assertEqual(self.session_notes(), first, "a trivial resume must not create a note")
        fx.write_jsonl(src, fx.claude_lines("s-resume", self.cwd, fx.SWITCH, 4), append=True)  # substantial resume
        self.end("claude", "s-resume", src)
        self.run_worker()
        self.run_worker()
        allnotes = self.session_notes()
        self.assertEqual(len(allnotes), 1)
        text = allnotes[0].read_text(encoding="utf-8")
        self.assertIn("Postgres", text)
        self.assertIn("Set up storage for the demo app", text, "continued notes retain the earlier session section")
        self.assertEqual(1, text.count("## Continued"))

    def test_03b_promptless_continuation_is_captured(self):
        src = self.claude_session("s-cont", fx.SUBSTANTIAL, 120)
        self.end("claude", "s-cont", src)
        self.run_worker()
        lines = fx.claude_lines("s-cont", self.cwd, [("x", "Implemented the installer and verified hooks. " * 20,
                                                      ["install.py", "hook.py", "plugin.js", "README.md"], ["pytest -q"] * 3)], 10)
        fx.write_jsonl(src, [l for l in lines if not (l["type"] == "user" and isinstance(l["message"]["content"], str))], append=True)
        self.end("claude", "s-cont", src)
        self.run_worker()
        notes = self.session_notes()
        self.assertEqual(len(notes), 1, "work without new prompts must still update the session note")
        self.assertIn("install.py", notes[0].read_text(encoding="utf-8"))

    def test_04_concurrent_sessions_do_not_corrupt_metadata(self):
        home = self.tmp / "rt"
        home.mkdir()
        scan_roots = home / "transcripts"
        claude_root, codex_root = scan_roots / "claude", scan_roots / "codex"
        claude_root.mkdir(parents=True)
        codex_root.mkdir(parents=True)
        (home / "config.json").write_text(json.dumps({
            "summarizer": "none", "auto_index": False,
            "claude_transcripts_root": str(claude_root), "codex_transcripts_root": str(codex_root),
        }), encoding="utf-8")
        sids = [f"s-conc-{i}" for i in range(6)]
        for sid in sids:
            self.end("claude", sid, self.claude_session(sid, fx.SUBSTANTIAL))
        env = dict(os.environ, OBSIDIAN_VAULT_PATH=str(self.cfg.vault), AGENTMEM_HOME=str(home))
        env.pop("AGENTMEM_CAPTURE", None)
        procs = [subprocess.Popen([sys.executable, "-I", str(ROOT / "run.py"), "capture", "--budget", "120"], env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) for _ in range(4)]
        for sid in sids[:3]:  # more activity arrives while workers run
            worker.enqueue(self.cfg, "claude", sid, event="end", source=str(self.tmp / "claude" / f"{sid}.jsonl"), cwd=self.cwd)
        for p in procs:
            _, err = p.communicate(timeout=180)
            self.assertEqual(p.returncode, 0, err.decode()[-500:])
        worker.run(self.cfg, summarizer=fx.fake_summarize, consolidator=fx.fake_consolidate)
        self.assertEqual(self.man.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(len(self.session_notes()), 6)
        self.assertEqual(self.man.db.execute("SELECT count(*) FROM captures").fetchone()[0], 6)
        note = self.project_note()
        for marker in (notes.STATE_BEGIN, notes.STATE_END, notes.SESS_BEGIN, notes.PENDING_BEGIN):
            self.assertEqual(note.count(marker), 1)
        self.assertEqual(len([l for l in notes.get_block(note, notes.SESS_BEGIN, notes.SESS_END).splitlines() if l.strip()]), 6)

    def test_05_failed_capture_can_be_retried(self):
        src = self.claude_session("s-retry", fx.SUBSTANTIAL)
        self.end("claude", "s-retry", src)
        real = Manifest.record_capture
        with mock.patch.object(Manifest, "record_capture", side_effect=RuntimeError("disk hiccup")):
            self.run_worker()
        row = self.man.get("claude", "s-retry")
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(len(self.session_notes()), 1, "note was written before the crash")
        self.man.mark("claude", "s-retry", next_try=0)  # skip backoff
        self.run_worker()
        self.assertEqual(self.man.get("claude", "s-retry")["status"], "captured")
        self.assertEqual(len(self.session_notes()), 1, "retry must reuse the existing note")
        self.assertIs(Manifest.record_capture, real)

    def test_06_continuation_updates_the_session_note_once(self):
        src = self.claude_session("s-immut", fx.SUBSTANTIAL)
        self.end("claude", "s-immut", src)
        self.run_worker()
        note = self.session_notes()[0]
        before = note.read_bytes()
        fx.write_jsonl(src, fx.claude_lines("s-immut", self.cwd, fx.SWITCH, 4), append=True)
        self.end("claude", "s-immut", src)
        self.run_worker()
        pipeline.consolidate_pending(self.cfg, self.man, fx.fake_summarize, fx.fake_consolidate)
        after = note.read_bytes()
        self.assertNotEqual(after, before)
        self.assertEqual(1, after.decode("utf-8").count("## Continued"))
        self.assertEqual(1, len(self.session_notes()))
        self.assertFalse(fsutil.create_once(note, "overwrite attempt"))
        self.assertEqual(note.read_bytes(), after)
        self.assertIn("immutable: true", before.decode())

    def test_07_project_note_reflects_updated_decisions(self):
        a = self.claude_session("s-dec1", fx.SUBSTANTIAL, 120)
        self.end("claude", "s-dec1", a)
        self.run_worker()
        state1 = notes.get_block(self.project_note(), notes.STATE_BEGIN, notes.STATE_END)
        self.assertIn("Use SQLite for storage", state1)
        b = self.claude_session("s-dec2", fx.SWITCH, 30)
        self.end("claude", "s-dec2", b)
        self.run_worker()
        state2 = notes.get_block(self.project_note(), notes.STATE_BEGIN, notes.STATE_END)
        self.assertIn("Postgres", state2)
        self.assertNotIn("Use SQLite for storage", state2)
        history = self.cfg.projects_dir / "Demo App" / "History.md"
        self.assertIn("Use SQLite for storage", history.read_text(encoding="utf-8"))

    def test_08_retrieval_finds_previous_decisions(self):
        self.end("claude", "s-find", self.claude_session("s-find", fx.SUBSTANTIAL))
        self.run_worker()
        hits = fallback_search(self.cfg, "SQLite storage", 10)
        self.assertTrue(any(h["label"] == "HISTORICAL" and "Use SQLite for storage" in Path(h["path"]).read_text(encoding="utf-8")
                            for h in hits))
        self.assertTrue(all(Path(h["path"]).exists() for h in hits), "every hit carries a verifiable path")

    def test_09_retrieval_distinguishes_history_from_current(self):
        self.end("claude", "s-h1", self.claude_session("s-h1", fx.SUBSTANTIAL, 120))
        self.run_worker()
        self.end("claude", "s-h2", self.claude_session("s-h2", fx.SWITCH, 30))
        self.run_worker()
        hits = fallback_search(self.cfg, "SQLite", 10)
        labels = {h["label"] for h in hits}
        self.assertIn("HISTORICAL", labels)
        self.assertIn("CURRENT", labels)
        current = [h for h in hits if h["label"] == "CURRENT"][0]
        state = notes.get_block(Path(current["path"]).read_text(encoding="utf-8"), notes.STATE_BEGIN, notes.STATE_END)
        self.assertIn("Postgres", state)
        self.assertNotIn("Use SQLite for storage", state)
        self.assertIn("superseded", (self.cfg.projects_dir / "Demo App" / "History.md").read_text(encoding="utf-8"))
        history_path = self.cfg.projects_dir / 'Demo App' / 'History.md'
        historical = [h for h in hits if h['label'] == 'HISTORICAL']
        self.assertTrue(any(Path(h['path']) == history_path for h in historical))
        for hit in historical:
            text = Path(hit['path']).read_text(encoding='utf-8')
            self.assertIn('superseded' if Path(hit['path']) == history_path else 'Historical record', text)

    def test_10_no_secrets_written_to_vault(self):
        turns = [(f"My key is {fx.SECRET_SAMPLES[0]} and password=hunter2hunter2. Please configure it.",
                  f"Configured with token {fx.SECRET_SAMPLES[2]}. We decided to use AWS key {fx.SECRET_SAMPLES[1]}. " * 4,
                  [".env", "config.py"], [f"export API_KEY={fx.SECRET_SAMPLES[0]}", "cat .env"])] + fx.SUBSTANTIAL
        self.end("claude", "s-secret", self.claude_session("s-secret", turns))
        self.run_worker(summarizer=lambda d, timeout=0: dict(fx.fake_summarize(d), facts=[d[:4000]]))
        self.end("codex", "c-secret", self._codex("c-secret", turns))
        self.run_worker(summarizer=fx.failing_llm)
        written = [p for p in self.cfg.vault.rglob("*") if p.is_file() and p.suffix in (".md", ".json", ".jsonl")]
        self.assertTrue(any(p.parent.parent.parent == self.cfg.sessions_dir for p in written))
        for p in written:
            text = p.read_text(encoding="utf-8", errors="replace")
            for s in fx.SECRET_SAMPLES + ["hunter2hunter2"]:
                self.assertNotIn(s, text, f"secret leaked into {p}")

    def test_11_llm_failure_does_not_corrupt_knowledge_base(self):
        self.end("claude", "s-ok", self.claude_session("s-ok", fx.SUBSTANTIAL, 120))
        self.run_worker()
        good_state = notes.get_block(self.project_note(), notes.STATE_BEGIN, notes.STATE_END)
        self.end("claude", "s-down", self.claude_session("s-down", fx.SWITCH, 30))
        self.run_worker(summarizer=fx.failing_llm, consolidator=fx.failing_llm)
        self.assertEqual(self.man.get("claude", "s-down")["status"], "captured")
        note = [p for p in self.session_notes() if "s-down"[-8:].replace("-", "") in p.stem.replace("-", "") or "summarizer: deterministic" in p.read_text(encoding="utf-8")]
        self.assertTrue(note, "deterministic note still written")
        text = self.project_note()
        self.assertEqual(notes.get_block(text, notes.STATE_BEGIN, notes.STATE_END), good_state, "state untouched by failure")
        self.assertIn("Postgres", notes.get_block(text, notes.PENDING_BEGIN, notes.PENDING_END))
        # The persisted deterministic summary has no evidence anchors for new status claims.
        res = pipeline.consolidate_pending(self.cfg, self.man, fx.fake_summarize, fx.fake_consolidate)
        self.assertEqual(res["consolidated"], 0)
        self.assertEqual(res['still_pending'], 1)
        text = self.project_note()
        self.assertEqual(good_state, notes.get_block(text, notes.STATE_BEGIN, notes.STATE_END))
        self.assertTrue(any('Postgres' in p.read_text(encoding='utf-8') for p in self.session_notes()))
        pending = notes.get_block(text, notes.PENDING_BEGIN, notes.PENDING_END)
        self.assertIn('uncited summary, status not updated', pending)
        self.assertIn('Postgres', pending)
        package = hook.startup_package(self.cfg, self.cwd)
        self.assertIn('1 captured session awaiting consolidation', package)
        self.assertIn('uncited summary', package)
        self.assertIn('Postgres', package)

    def test_11b_pending_older_update_does_not_overwrite_newer_status(self):
        older = self.claude_session("s-pending-a", fx.SUBSTANTIAL, 120)
        self.end("claude", "s-pending-a", older)
        self.run_worker(consolidator=fx.failing_llm)
        note = self.project_note()
        original_state = notes.get_block(note, notes.STATE_BEGIN, notes.STATE_END)
        pending = notes.get_block(note, notes.PENDING_BEGIN, notes.PENDING_END)
        self.assertEqual(notes.get_block(note, notes.STATE_BEGIN, notes.STATE_END), original_state)
        self.assertIn("status update pending", pending)
        self.assertIn("model unavailable", pending)

        newer = self.claude_session("s-newer-b", fx.SWITCH, 60)
        self.end("claude", "s-newer-b", newer)
        self.run_worker()
        state_after_newer = notes.get_block(self.project_note(), notes.STATE_BEGIN, notes.STATE_END)
        self.assertIn("Postgres", state_after_newer)
        self.assertEqual(self.man.capture("claude", "s-pending-a", 1)["consolidated"], 0,
                         "a stale pending update must not be retired without reconciliation")

        result = pipeline.consolidate_pending(self.cfg, self.man, fx.fake_summarize, fx.fake_consolidate)
        final_note = self.project_note()
        final_state = notes.get_block(final_note, notes.STATE_BEGIN, notes.STATE_END)
        final_pending = notes.get_block(final_note, notes.PENDING_BEGIN, notes.PENDING_END)
        self.assertIn("Postgres", final_state)
        self.assertIn("Use SQLite for storage", final_pending)
        self.assertIn("status update pending", final_pending)
        self.assertEqual(result["still_pending"], 1)
        older_link = Path(self.man.capture("claude", "s-pending-a", 1)["note_path"]).with_suffix("").as_posix()
        self.assertIn(f"[[{older_link}]]", final_pending)

    def test_11c_displaced_state_items_are_linked_in_project_history(self):
        path = self.cfg.projects_dir / "Demo App" / "Demo App.md"
        old_state = "\n".join([
            "### Current state", "- Current endpoint is /v1.", "- Blocker: migration is pending.", "",
            "### Active decisions", "- Keep SQLite for local mode.", "", "### Superseded decisions", "- none", "",
            "### Open questions and next steps", "- Confirm rollout date.",
        ])
        new_state = "\n".join([
            "### Current state", "- Current endpoint is /v2.", "", "### Active decisions", "- none", "",
            "### Superseded decisions", "- none", "", "### Open questions and next steps", "- none",
        ])
        notes.update_project_note(path, "Demo App", "2026-10-23", state=old_state)
        notes.update_project_note(path, "Demo App", "2026-10-24", state=new_state, session_link="0-Sessions/new-session")

        history = path.with_name("History.md").read_text(encoding="utf-8")
        self.assertIn("Current endpoint is /v1", history)
        self.assertIn("Blocker: migration is pending", history)
        self.assertIn("Keep SQLite for local mode", history)
        self.assertIn("Confirm rollout date", history)
        self.assertIn("[[0-Sessions/new-session]]", history)

    def _codex(self, sid, turns):
        path = self.tmp / "codex" / f"rollout-2026-10-14T10-00-00-{sid}.jsonl"
        fx.write_jsonl(path, fx.codex_lines(sid, self.cwd, turns))
        return path

    def test_12_works_across_agents(self):
        self.end("claude", "cl-1", self.claude_session("cl-1", fx.SUBSTANTIAL))
        cx = "019f0000-0000-7000-8000-000000000001"
        self.end("codex", cx, self._codex(cx, fx.SUBSTANTIAL))
        export = fx.opencode_export("ses_demo1", self.cwd, fx.SUBSTANTIAL)
        worker.enqueue(self.cfg, "opencode", "ses_demo1", event="turn", cwd=self.cwd)
        self.man.touch("opencode", "ses_demo1", now=time.time() - 3600)  # idle past the settle window
        with mock.patch.object(adapters, "export_opencode", return_value=export):
            worker.drain_queue(self.cfg, self.man)
            self.man.touch("opencode", "ses_demo1", now=time.time() - 3600)
            self.run_worker()
        rows = {r["agent"]: r for r in self.man.db.execute("SELECT * FROM captures").fetchall()}
        self.assertEqual(set(rows), {"claude", "codex", "opencode"})
        self.assertTrue(all(r["project"] == "Demo App" for r in rows.values()))
        for agent in ("claude", "codex", "opencode"):
            text = (self.cfg.vault / rows[agent]["note_path"]).read_text(encoding="utf-8")
            self.assertIn("- `db.py`", text, f"{agent}: modified files are relative to the project")
            self.assertNotIn(str(self.tmp), text.split("## Source")[0], f"{agent}: no absolute work paths in the body")
        cxnote = (self.cfg.vault / rows["codex"]["note_path"]).read_text(encoding="utf-8")
        self.assertNotIn("AGENTS.md instructions", cxnote, "injected context is not a user prompt")


class Safeguards(Base):
    def test_pause_switch(self):
        self.cfg.pause_file.write_text("paused")
        src = self.claude_session("s-pause", fx.SUBSTANTIAL)
        self.end("claude", "s-pause", src)
        self.assertEqual(self.run_worker()["status"], "paused")
        self.assertEqual(self.session_notes(), [])
        self.cfg.pause_file.unlink()
        self.run_worker()
        self.assertEqual(len(self.session_notes()), 1)

    def test_recursion_guard_in_hook(self):
        env = dict(os.environ, AGENTMEM_CAPTURE="1", OBSIDIAN_VAULT_PATH=str(self.cfg.vault))
        payload = json.dumps({"hook_event_name": "SessionEnd", "session_id": "x", "transcript_path": "y", "cwd": self.cwd})
        out = subprocess.run([sys.executable, "-I", str(ROOT / "run.py"), "hook", "claude"], input=payload.encode(),
                             capture_output=True, env=env, timeout=60)
        self.assertEqual(out.stdout.decode().strip(), "{}")
        self.assertEqual(list(self.cfg.queue_dir.glob("*.json")), [])

    def test_unsettled_session_waits(self):
        src = self.claude_session("s-live", fx.SUBSTANTIAL, minutes_ago=3)
        worker.enqueue(self.cfg, "claude", "s-live", event="turn", source=str(src), cwd=self.cwd)
        self.run_worker()
        self.assertEqual(self.session_notes(), [])
        self.assertEqual(self.man.get("claude", "s-live")["status"], "pending")

    def test_redaction_patterns(self):
        for s in fx.SECRET_SAMPLES + ["Bearer abcdefghijklmnopqrstuvwxyz012345", "postgres://user:pa55word@host/db",
                                      'api_key = "q8Xv2LmN0pRsT4"', "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"]:
            self.assertTrue(contains_secret(s), s)
        for ok in ["git commit 3f2a9c1e5b7d9f1a3c5e7b9d1f3a5c7e9b1d3f5a", "C:/Users/caspe/Documents/Projects/agent-memory/run.py",
                   "session 01a13a34-04b6-75c0-8998-21d7db60ac4b", "Use SQLite for storage",
                   "~/.codex/backups/herdr-modernize-20261014/ROLLBACK.md",
                   "[[0-Sessions/2026/10/2026-10-14-2051-claude-remember-agentmem-routes-sessions-from-generic-folders-ceb758a2]]"]:
            self.assertEqual(redact(ok), ok, ok)

    def test_atomic_write_leaves_no_temp_files(self):
        target = self.cfg.vault / "1-Projects" / "x.md"
        for i in range(20):
            fsutil.atomic_write(target, f"v{i}")
        self.assertEqual(target.read_text(), "v19")
        self.assertEqual([p.name for p in target.parent.iterdir()], ["x.md"])


if __name__ == "__main__":
    unittest.main()
