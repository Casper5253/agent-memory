import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentmem import pipeline  # noqa: E402
from agentmem.manifest import Manifest  # noqa: E402
from fixtures import SUBSTANTIAL, fake_consolidate, fake_summarize, make_vault, write_jsonl, claude_lines  # noqa: E402


class OrphanTranscriptRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / ".audit")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = make_vault(self.root)
        self.man = Manifest(self.cfg.manifest_path)
        self.addCleanup(self.man.close)
        self.cwd = str(self.root / "work" / "demo-app")
        marker = Path(self.cwd).parent / ".git"
        if marker.is_dir() and not any(marker.iterdir()):
            marker.rmdir()
        marker.write_text("gitdir: nonexistent", encoding="utf-8")

    def test_killed_unregistered_claude_transcript_is_registered_after_settle_window(self):
        sid = "killed-before-stop-1234"
        transcripts = self.root / "claude-projects"
        source = transcripts / "C--synthetic-project" / f"{sid}.jsonl"
        write_jsonl(source, claude_lines(sid, self.cwd, SUBSTANTIAL, minutes_ago=30))
        empty_codex = self.root / "empty-codex"
        empty_codex.mkdir()
        with patch.dict(os.environ, {"AGENTMEM_CLAUDE_TRANSCRIPTS_ROOT": str(transcripts),
                                     "AGENTMEM_CODEX_TRANSCRIPTS_ROOT": str(empty_codex)}):
            self.assertEqual([], pipeline.process_ready(self.cfg, self.man, budget_s=30,
                                                         summarizer=fake_summarize, consolidator=fake_consolidate))
            self.assertIsNone(self.man.get("claude", sid))
            old = time.time() - (self.cfg.settle_minutes * 60 + 10)
            os.utime(source, (old, old))
            result = pipeline.process_ready(self.cfg, self.man, budget_s=30,
                                            summarizer=fake_summarize, consolidator=fake_consolidate)
        self.assertIn(("claude", sid, "captured"), result)
        self.assertEqual("captured", self.man.get("claude", sid)["status"])
        self.assertEqual(0, self.man.get("claude", sid)["end_seen"])
        self.assertEqual(1, len(self.man.query_captures()))


if __name__ == "__main__":
    unittest.main()
