import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentmem import pipeline  # noqa: E402
from agentmem.manifest import Manifest  # noqa: E402
from fixtures import SUBSTANTIAL, SWITCH, fake_consolidate, fake_summarize, make_vault, write_jsonl, claude_lines  # noqa: E402


class LogicalSessionNoteTests(unittest.TestCase):
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

    def test_continuation_is_appended_once_with_range_hash_and_atomic_retry(self):
        sid = "logical-session-1234"
        path = self.root / f"{sid}.jsonl"
        write_jsonl(path, claude_lines(sid, self.cwd, SUBSTANTIAL))
        self.man.touch("claude", sid, source=str(path), cwd=self.cwd, end=True)
        pipeline.finalize(self.cfg, self.man, self.man.get("claude", sid), fake_summarize, fake_consolidate)
        first = self.man.capture("claude", sid, 1)
        first_path = self.cfg.vault / first["note_path"]
        initial = first_path.read_text(encoding="utf-8")
        write_jsonl(path, claude_lines(sid, self.cwd, SWITCH, 5), append=True)
        self.man.touch("claude", sid, source=str(path), cwd=self.cwd, end=True)
        with patch.object(self.man, "record_capture", side_effect=RuntimeError("crash after note")):
            with self.assertRaisesRegex(RuntimeError, "crash after note"):
                pipeline.finalize(self.cfg, self.man, self.man.get("claude", sid), fake_summarize, fake_consolidate)
        interrupted_path = max(self.cfg.sessions_dir.rglob("*.md"), key=lambda candidate: candidate.stat().st_mtime_ns)
        interrupted = interrupted_path.read_text(encoding="utf-8")
        self.assertNotEqual(initial, interrupted)
        self.assertIn("## Continued", interrupted)
        pipeline.finalize(self.cfg, self.man, self.man.get("claude", sid), fake_summarize, fake_consolidate)
        final = first_path.read_text(encoding="utf-8")
        self.assertEqual(1, final.count("## Continued"))
        self.assertEqual(1, len(list(self.cfg.sessions_dir.rglob("*.md"))))
        self.assertIn("Transcript range", final)
        self.assertIn("SHA-256", final)
        self.assertEqual(2, len(self.man.query_captures()))

    def test_initial_note_omits_empty_sections(self):
        sid = "empty-headings-1234"
        path = self.root / f"{sid}.jsonl"
        write_jsonl(path, claude_lines(sid, self.cwd, SUBSTANTIAL))
        self.man.touch("claude", sid, source=str(path), cwd=self.cwd, end=True)
        summary = {"title": "Only summary", "summary": "Evidence recorded.", "what_happened": [],
                   "decisions": [], "facts": [], "open_questions": [], "next_steps": []}
        pipeline.finalize(self.cfg, self.man, self.man.get("claude", sid), lambda *a, **k: summary.copy(), fake_consolidate)
        text = (self.cfg.vault / self.man.capture("claude", sid, 1)["note_path"]).read_text(encoding="utf-8")
        for heading in ("## What happened", "## Decisions and rationale", "## Confirmed facts",
                        "## Open questions and next steps"):
            self.assertNotIn(heading, text)


if __name__ == "__main__":
    unittest.main()
