"""The end hook keeps a copy of the transcript so a later cleanup cannot lose the session."""
import os
import stat
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentmem import worker  # noqa: E402
from agentmem.config import Config  # noqa: E402
from agentmem.manifest import Manifest  # noqa: E402
from agentmem.pipeline import transcript_located  # noqa: E402
import fixtures as fx  # noqa: E402


class LostSnapshots(unittest.TestCase):
    def setUp(self):
        self._tmp = fx.tmpdir()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.cfg = fx.make_vault(self.tmp)
        self.cwd = str(self.tmp / "work" / "demo-app")
        self.man = Manifest(self.cfg.manifest_path)
        self.addCleanup(self.man.close)

    def transcript(self, sid="sess-lost"):
        source = self.tmp / "claude" / f"{sid}.jsonl"
        fx.write_jsonl(source, fx.claude_lines(sid, self.cwd, fx.SUBSTANTIAL, 30))
        return source

    def test_the_snapshot_path_is_outside_the_vault(self):
        source = self.transcript()
        worker.enqueue(self.cfg, "claude", "sess-lost", event="end", source=str(source), cwd=self.cwd)
        snap = self.cfg.snapshot_path("claude", "sess-lost")
        self.assertFalse(snap.resolve().is_relative_to(self.cfg.vault.resolve()))

    def test_a_snapshot_inside_the_vault_is_refused(self):
        cfg = Config(self.cfg.vault, snapshots_root=self.cfg.vault / "_meta" / "snapshots")
        source = self.transcript()
        worker.enqueue(cfg, "claude", "sess-lost", event="end", source=str(source), cwd=self.cwd)
        self.assertFalse(cfg.snapshot_path("claude", "sess-lost").exists())
        self.assertEqual(len(list(cfg.queue_dir.glob("*.json"))), 1)

    @unittest.skipIf(os.name == "nt", "Windows does not carry POSIX file modes")
    def test_the_copy_is_owner_only(self):
        source = self.transcript()
        worker.enqueue(self.cfg, "claude", "sess-lost", event="end", source=str(source), cwd=self.cwd)
        mode = stat.S_IMODE(os.stat(self.cfg.snapshot_path("claude", "sess-lost")).st_mode)
        self.assertEqual(mode & 0o077, 0)

    def test_the_end_hook_copies_the_transcript(self):
        source = self.transcript()
        worker.enqueue(self.cfg, "claude", "sess-lost", event="end", source=str(source), cwd=self.cwd)
        snap = self.cfg.snapshot_path("claude", "sess-lost")
        self.assertTrue(snap.exists())
        self.assertEqual(snap.read_bytes(), source.read_bytes())

    def test_a_turn_does_not_copy_the_whole_transcript(self):
        source = self.transcript()
        worker.enqueue(self.cfg, "claude", "sess-lost", event="turn", source=str(source), cwd=self.cwd)
        self.assertFalse(self.cfg.snapshot_path("claude", "sess-lost").exists())

    def test_capture_reads_the_copy_after_the_transcript_is_gone(self):
        source = self.transcript()
        worker.enqueue(self.cfg, "claude", "sess-lost", event="end", source=str(source), cwd=self.cwd)
        worker.drain_queue(self.cfg, self.man)
        source.unlink()
        worker.run(self.cfg, summarizer=fx.fake_summarize, consolidator=fx.fake_consolidate)
        self.assertEqual(self.man.get("claude", "sess-lost")["status"], "captured")
        self.assertEqual(len(list(self.cfg.sessions_dir.rglob("*.md"))), 1)

    def test_a_copy_keeps_the_session_from_being_marked_lost(self):
        source = self.transcript()
        worker.enqueue(self.cfg, "claude", "sess-lost", event="end", source=str(source), cwd=self.cwd)
        worker.drain_queue(self.cfg, self.man)
        source.unlink()
        row = self.man.get("claude", "sess-lost")
        self.assertIs(transcript_located(self.cfg, row), True)
        source.unlink(missing_ok=True)
        (self.cfg.snapshot_path("claude", "sess-lost")).unlink()
        self.assertIs(transcript_located(self.cfg, row), False)

    def test_the_copy_is_dropped_once_the_session_is_captured(self):
        source = self.transcript()
        worker.enqueue(self.cfg, "claude", "sess-lost", event="end", source=str(source), cwd=self.cwd)
        worker.drain_queue(self.cfg, self.man)
        source.unlink()
        worker.run(self.cfg, summarizer=fx.fake_summarize, consolidator=fx.fake_consolidate)
        self.assertFalse(self.cfg.snapshot_path("claude", "sess-lost").exists())

    def test_a_failed_copy_still_queues_the_event(self):
        source = self.transcript()
        with mock.patch("agentmem.worker.shutil.copyfile", side_effect=OSError("disk full")):
            worker.enqueue(self.cfg, "claude", "sess-lost", event="end", source=str(source), cwd=self.cwd)
        self.assertEqual(len(list(self.cfg.queue_dir.glob("*.json"))), 1)
        worker.drain_queue(self.cfg, self.man)
        self.assertEqual(self.man.get("claude", "sess-lost")["source"], str(source))


if __name__ == "__main__":
    unittest.main()
