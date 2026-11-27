"""A3: the hook queue is redacted at write and survives a mid-drain kill exactly once."""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentmem import worker  # noqa: E402
from agentmem.manifest import Manifest  # noqa: E402
from agentmem.redact import MASK  # noqa: E402
import fixtures as fx  # noqa: E402


class QueueSafety(unittest.TestCase):
    def setUp(self):
        self._tmp = fx.tmpdir()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.cfg = fx.make_vault(self.tmp)
        self.cwd = str(self.tmp / "work" / "demo-app")
        self.man = Manifest(self.cfg.manifest_path)
        self.addCleanup(self.man.close)

    def queue_files(self):
        return sorted(self.cfg.queue_dir.glob("*.json"))

    def test_enqueue_redacts_secrets_before_they_touch_disk(self):
        secret = fx.SECRET_SAMPLES[0]
        worker.enqueue(self.cfg, "claude", "sess-redact", event="turn",
                       source=str(self.tmp / secret / "transcript.jsonl"), cwd=str(self.tmp / secret))
        files = self.queue_files()
        self.assertEqual(len(files), 1)
        text = files[0].read_text(encoding="utf-8")
        self.assertNotIn(secret, text)
        self.assertIn(MASK, text)
        self.assertEqual(json.loads(text)["session_id"], "sess-redact")

    def test_a_kill_after_the_commit_replays_the_entry_once(self):
        job = worker.enqueue(self.cfg, "claude", "sess-crash", event="turn", cwd=self.cwd)
        stamp = json.loads(job.read_text(encoding="utf-8"))["ts"]
        original = self.man.record_event

        def commit_then_die(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("worker killed after the commit")

        with mock.patch.object(self.man, "record_event", new=commit_then_die):
            with self.assertRaises(OSError):
                worker.drain_queue(self.cfg, self.man)
        self.assertEqual(self.queue_files(), [job], "the killed drain must leave its entry")
        worker.drain_queue(self.cfg, self.man)
        events = [(row["event"], row["ts"]) for row in self.man.events("claude", "sess-crash")]
        self.assertEqual(events, [("turn", stamp)])
        self.assertEqual(self.queue_files(), [])

    def test_a_kill_before_the_commit_replays_the_entry_once(self):
        job = worker.enqueue(self.cfg, "claude", "sess-crash", event="turn", cwd=self.cwd)
        stamp = json.loads(job.read_text(encoding="utf-8"))["ts"]

        def die_before_commit(*args, **kwargs):
            raise OSError("worker killed before the commit")

        with mock.patch.object(self.man, "record_event", new=die_before_commit):
            with self.assertRaises(OSError):
                worker.drain_queue(self.cfg, self.man)
        self.assertEqual(self.queue_files(), [job], "the killed drain must leave its entry")
        worker.drain_queue(self.cfg, self.man)
        events = [(row["event"], row["ts"]) for row in self.man.events("claude", "sess-crash")]
        self.assertEqual(events, [("turn", stamp)])
        self.assertEqual(self.queue_files(), [])

    def test_a_duplicate_drain_captures_once(self):
        sid = "sess-once"
        source = self.tmp / "claude" / f"{sid}.jsonl"
        fx.write_jsonl(source, fx.claude_lines(sid, self.cwd, fx.SUBSTANTIAL, 30))
        job = worker.enqueue(self.cfg, "claude", sid, event="end", source=str(source), cwd=self.cwd)
        record = job.read_text(encoding="utf-8")
        worker.drain_queue(self.cfg, self.man)
        (self.cfg.queue_dir / job.name).write_text(record, encoding="utf-8")
        worker.drain_queue(self.cfg, self.man)
        self.assertEqual(len(self.man.events("claude", sid)), 1)
        for _ in range(2):
            worker.run(self.cfg, summarizer=fx.fake_summarize, consolidator=fx.fake_consolidate)
        notes = sorted(self.cfg.sessions_dir.rglob("*.md"))
        self.assertEqual(len(notes), 1)
        self.assertEqual(self.man.db.execute("SELECT count(*) FROM captures").fetchone()[0], 1)
        self.assertEqual(self.man.get("claude", sid)["status"], "captured")


if __name__ == "__main__":
    unittest.main()
