import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentmem import adapters, extract, pipeline
from agentmem.manifest import Manifest
import fixtures as fx

FIRST = [("Set up storage for the demo app. Use SQLite for storage, it is local only.", "Will do.", [], [])]
SECOND = [("Go ahead and create db.py now.",
           "Done. We decided to use SQLite for storage because the app is local only. Created db.py. " * 5,
           ["db.py"], ["pytest -q"])]
WRITERS = {"claude": (fx.claude_lines, "{sid}.jsonl", "40000000-0000-4000-8000-0000000000d1"),
           "codex": (fx.codex_lines, "rollout-2026-10-24T10-00-00-{sid}.jsonl", "01a16d2b-0000-7000-8000-0000000000d2")}


class DeferredCapture(unittest.TestCase):
    def setUp(self):
        self.tmp_ctx = fx.tmpdir()
        self.tmp = Path(self.tmp_ctx.name)
        self.cfg = fx.make_vault(self.tmp)
        self.man = Manifest(self.cfg.manifest_path)
        self.cwd = str(self.tmp / "repo")

    def tearDown(self):
        self.man.close()
        self.tmp_ctx.cleanup()

    def drain(self, agent, sid):
        return pipeline.finalize(self.cfg, self.man, self.man.get(agent, sid), fx.fake_summarize, fx.fake_consolidate)

    def test_two_trivial_turns_are_judged_together(self):
        for agent, (write, name, sid) in WRITERS.items():
            with self.subTest(agent=agent):
                source = self.tmp / "transcripts" / agent / name.format(sid=sid)
                fx.write_jsonl(source, write(sid, self.cwd, FIRST))
                self.man.touch(agent, sid, source=str(source), cwd=self.cwd, end=True)
                self.assertEqual(self.drain(agent, sid), "trivial")
                first_end = source.stat().st_size
                row = self.man.get(agent, sid)
                self.assertEqual((row["status"], row["parts"], row["deferred_start"], row["captured_until"]),
                                 ("skipped", 0, 0, first_end))

                fx.write_jsonl(source, write(sid, self.cwd, FIRST + SECOND)[len(write(sid, self.cwd, FIRST)):], append=True)
                increment = adapters.read(agent, sid, str(source), start=first_end)
                self.assertFalse(extract.build(increment, self.cfg.min_user_prompts, self.cfg.min_assistant_chars,
                                               self.cfg.digest_char_cap).substantial)
                self.man.touch(agent, sid, source=str(source), cwd=self.cwd, end=True)
                self.assertEqual(self.drain(agent, sid), "captured")
                row, capture = self.man.get(agent, sid), self.man.capture(agent, sid, 1)
                self.assertEqual((row["status"], row["parts"], row["deferred_start"]), ("captured", 1, None))
                self.assertEqual((capture["start_offset"], capture["end_offset"]), (0, source.stat().st_size))

                self.man.touch(agent, sid, source=str(source), cwd=self.cwd, end=True)
                self.assertEqual(self.drain(agent, sid), "no-new-content")
                self.assertIsNone(self.man.capture(agent, sid, 2))

    def test_deferral_is_logged_without_session_text(self):
        write, name, sid = WRITERS["claude"]
        source = self.tmp / "transcripts" / "claude" / name.format(sid=sid)
        fx.write_jsonl(source, write(sid, self.cwd, FIRST))
        self.man.touch("claude", sid, source=str(source), cwd=self.cwd, end=True)
        self.drain("claude", sid)
        events = [json.loads(line) for line in (self.cfg.vault / "_meta" / "logs" / "capture.jsonl").read_text(
            encoding="utf-8").splitlines()]
        deferred = [e for e in events if e["event"] == "capture-deferred"]
        self.assertEqual([(e["session"], e["start"], e["parts"]) for e in deferred], [(sid, 0, 0)])
        self.assertNotIn("SQLite", json.dumps(deferred))


if __name__ == "__main__":
    unittest.main()
