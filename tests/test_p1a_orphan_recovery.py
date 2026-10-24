import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentmem import adapters, pipeline  # noqa: E402
from agentmem.config import Config  # noqa: E402
from agentmem.manifest import Manifest  # noqa: E402
from fixtures import (SUBSTANTIAL, fake_consolidate, fake_summarize, make_vault, write_jsonl,
                      claude_lines, codex_lines, ts)  # noqa: E402


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
        sid = "10000000-0000-4000-8000-000000000005"
        transcripts = self.root / "claude-projects"
        source = transcripts / "C--synthetic-project" / f"{sid}.jsonl"
        empty_codex = self.root / "empty-codex"
        empty_codex.mkdir()
        self.cfg.claude_transcripts_root = transcripts
        self.cfg.codex_transcripts_root = empty_codex
        horizon = time.time() - 86400
        pipeline._discover_orphan_transcripts(self.cfg, self.man, now=horizon)
        write_jsonl(source, claude_lines(sid, self.cwd, SUBSTANTIAL, minutes_ago=5))
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

    def test_discovery_horizon_ignores_historical_transcript_then_accepts_growth_after_horizon(self):
        sid = "10000000-0000-4000-8000-000000000001"
        source = self.cfg.claude_transcripts_root / "C--synthetic-project" / f"{sid}.jsonl"
        write_jsonl(source, claude_lines(sid, self.cwd, SUBSTANTIAL))
        now = 1_800_000_000
        old = now - self.cfg.settle_minutes * 60 - 10
        os.utime(source, (old, old))

        self.assertEqual(0, pipeline._discover_orphan_transcripts(self.cfg, self.man, now=now))
        self.assertEqual(float(now), self.man.discovery_since(now=now + 10))
        self.assertIsNone(self.man.get("claude", sid))

        modified = now + 1
        os.utime(source, (modified, modified))
        self.assertEqual(1, pipeline._discover_orphan_transcripts(self.cfg, self.man, now=now + 4000))
        self.assertIsNotNone(self.man.get("claude", sid))

    def test_discovery_scope_excludes_claude_subagent_transcript(self):
        sid = "10000000-0000-4000-8000-000000000002"
        source = self.cfg.claude_transcripts_root / "C--synthetic-project" / "subagents" / f"{sid}.jsonl"
        write_jsonl(source, claude_lines(sid, self.cwd, SUBSTANTIAL))
        now = 1_800_000_000
        old = now - self.cfg.settle_minutes * 60 - 10
        os.utime(source, (old, old))
        pipeline._discover_orphan_transcripts(self.cfg, self.man, now=now)

        self.assertIsNone(self.man.get("claude", sid))

    def test_discovery_scope_accepts_codex_rollouts_only(self):
        sid = "30000000-0000-4000-8000-000000000001"
        now = 1_800_000_000
        pipeline._discover_orphan_transcripts(self.cfg, self.man, now=now)
        rollout = self.cfg.codex_transcripts_root / "2026" / "10" / f"rollout-2026-10-24-{sid}.jsonl"
        unrelated = self.cfg.codex_transcripts_root / "2026" / "10" / f"session-{sid}.jsonl"
        write_jsonl(rollout, codex_lines(sid, self.cwd, SUBSTANTIAL))
        write_jsonl(unrelated, codex_lines("30000000-0000-4000-8000-000000000002", self.cwd, SUBSTANTIAL))
        modified = now + 1
        os.utime(rollout, (modified, modified))
        os.utime(unrelated, (modified, modified))

        self.assertEqual(1, pipeline._discover_orphan_transcripts(self.cfg, self.man, now=now + 4000))
        self.assertIsNotNone(self.man.get("codex", sid))
        self.assertIsNone(self.man.get("codex", "30000000-0000-4000-8000-000000000002"))

    def test_discovery_skips_transcript_from_summarizer_neutral_cwd(self):
        sid = "10000000-0000-4000-8000-000000000003"
        cwd = str(Path(tempfile.gettempdir()) / "agentmem-llm-test-cwd")
        source = self.cfg.claude_transcripts_root / "C--synthetic-project" / f"{sid}.jsonl"
        write_jsonl(source, claude_lines(sid, cwd, SUBSTANTIAL))
        now = 1_800_000_000
        old = now - self.cfg.settle_minutes * 60 - 10
        os.utime(source, (old, old))
        pipeline._discover_orphan_transcripts(self.cfg, self.man, now=now)

        self.assertIsNone(self.man.get("claude", sid))

    def test_discovery_reopens_existing_capture_when_transcript_grows(self):
        sid = "10000000-0000-4000-8000-000000000004"
        now = 1_800_000_000
        pipeline._discover_orphan_transcripts(self.cfg, self.man, now=now)
        source = self.cfg.claude_transcripts_root / "C--synthetic-project" / f"{sid}.jsonl"
        write_jsonl(source, claude_lines(sid, self.cwd, SUBSTANTIAL))
        modified = now + 1
        os.utime(source, (modified, modified))
        transcript = adapters.read("claude", sid, str(source))
        self.man.touch("claude", sid, cwd=self.cwd, source=str(source), now=modified)
        self.man.mark("claude", sid, status="captured", captured_until=transcript.end, parts=1)

        with source.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "user", "sessionId": sid, "cwd": self.cwd,
                                 "timestamp": ts(), "message": {"role": "user", "content": "added growth"}}) + "\n")
        os.utime(source, (modified + 1, modified + 1))

        self.assertEqual(1, pipeline._discover_orphan_transcripts(self.cfg, self.man, now=now + 4000))
        self.assertEqual("pending", self.man.get("claude", sid)["status"])

    def test_discovery_caps_new_registrations_at_five_oldest_first(self):
        self.cfg.discovery_max_per_drain = 5
        now = 1_800_000_000
        pipeline._discover_orphan_transcripts(self.cfg, self.man, now=now)
        rows = []
        for index in range(8):
            sid = f"20000000-0000-4000-8000-{index + 1:012d}"
            source = self.cfg.claude_transcripts_root / "C--synthetic-project" / f"{sid}.jsonl"
            write_jsonl(source, claude_lines(sid, self.cwd, SUBSTANTIAL))
            modified = now + index + 1
            os.utime(source, (modified, modified))
            rows.append((sid, source))

        self.assertEqual(5, pipeline._discover_orphan_transcripts(self.cfg, self.man, now=now + 4000))
        registered = [sid for sid, _ in rows if self.man.get("claude", sid)]
        self.assertEqual([sid for sid, _ in rows[:5]], registered)
        self.assertEqual(5, len(registered))

    def test_test_guard_rejects_scanning_real_agent_home(self):
        self.assertEqual(self.cfg.claude_transcripts_root, self.root / "transcripts" / "claude")
        self.assertEqual(self.cfg.codex_transcripts_root, self.root / "transcripts" / "codex")
        default_cfg = Config(self.cfg.vault)
        self.assertEqual(default_cfg.claude_transcripts_root, Path.home() / ".claude" / "projects")
        self.assertEqual(default_cfg.codex_transcripts_root, Path.home() / ".codex" / "sessions")
        with self.assertRaisesRegex(AssertionError, "real agent transcript root"):
            (Path.home() / ".claude" / "projects").rglob("*.jsonl")


if __name__ == "__main__":
    unittest.main()
