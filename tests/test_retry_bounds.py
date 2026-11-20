import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentmem.manifest import Manifest, RetryPolicy, error_cause  # noqa: E402
from agentmem.pipeline import discovery_backlog, transcript_located, _opencode_session_exists  # noqa: E402
from fixtures import make_vault  # noqa: E402

PERMANENT = "FileNotFoundError: no transcript for codex:s1"
TRANSIENT = "TimeoutError: model call timed out"
MUTATED = "ValueError: source_change: earlier OpenCode messages mutated"
COMPACTED = "ValueError: source truncated or invalid step range"
OLD = time.time() - 4 * 86400  # past the 48h missing-transcript grace period
ABSENT = lambda row: False     # locator verdict: absent from every place the adapters look


class RetryBoundsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / ".audit")
        self.addCleanup(self.tmp.cleanup)
        self.cfg = make_vault(Path(self.tmp.name))
        self.man = Manifest(self.cfg.manifest_path)
        self.addCleanup(self.man.close)

    def _pending(self, sid, **fields):
        self.man.touch("codex", sid, cwd="C:/work/demo", source="C:/gone/rollout-x.jsonl")
        if fields:
            self.man.mark("codex", sid, **fields)

    def _ids(self, rows):
        return {r["session_id"] for r in rows}

    def test_error_cause_table(self):
        self.assertEqual("transcript-missing", error_cause(PERMANENT))
        self.assertEqual("source-mutated", error_cause(MUTATED))
        self.assertEqual("transcript-compacted", error_cause(COMPACTED))
        self.assertEqual("transient", error_cause(TRANSIENT))
        self.assertEqual("transient", error_cause(""))
        self.assertEqual("transient", error_cause(None))

    def test_error_cause_covers_raw_and_variant_errors(self):
        self.assertEqual("transcript-missing",
                         error_cause("FileNotFoundError: [Errno 2] No such file or directory: 'C:/x.jsonl'"))
        self.assertEqual("transcript-compacted", error_cause("ValueError: source truncated or invalid range"))
        self.assertEqual("transcript-missing", error_cause("CalledProcessError: opencode export failed"))

    def test_permanent_error_terminal_after_one_confirming_retry(self):
        self._pending("s1", last_activity=OLD)
        self.man.fail("codex", "s1", PERMANENT, exists=ABSENT)
        self.assertEqual("error", self.man.get("codex", "s1")["status"])
        self.man.fail("codex", "s1", PERMANENT, exists=ABSENT)
        row = self.man.get("codex", "s1")
        self.assertEqual("lost", row["status"])
        self.assertNotIn("s1", self._ids(self.man.ready(0)))

    def test_missing_transcript_stays_transient_while_source_exists(self):
        """A 'no transcript' error is not a cause: the transcript exists, so keep retrying."""
        self._pending("t1", last_activity=OLD)
        self.man.fail("codex", "t1", PERMANENT, exists=lambda row: True)
        self.man.fail("codex", "t1", PERMANENT, exists=lambda row: True)
        self.assertEqual("error", self.man.get("codex", "t1")["status"])

    def test_missing_transcript_young_session_keeps_retrying(self):
        """Absent everywhere but the session is inside the grace period: it may be written late."""
        self._pending("t2")  # last_activity = now
        self.man.fail("codex", "t2", PERMANENT, exists=ABSENT)
        self.man.fail("codex", "t2", PERMANENT, exists=ABSENT)
        self.assertEqual("error", self.man.get("codex", "t2")["status"])

    def test_missing_transcript_unverified_keeps_retrying(self):
        """No locator (or a locator that cannot tell) means the absence is unconfirmed: transient."""
        self._pending("t3", last_activity=OLD)
        self.man.fail("codex", "t3", PERMANENT)
        self.man.fail("codex", "t3", PERMANENT, exists=lambda row: None)
        self.assertEqual("error", self.man.get("codex", "t3")["status"])

    def test_locator_finds_claude_transcript(self):
        sid = "11111111-2222-3333-4444-555555555555"
        hit = self.cfg.claude_transcripts_root / "proj-x" / f"{sid}.jsonl"
        hit.parent.mkdir(parents=True)
        hit.write_text("{}\n", encoding="utf-8")
        row = {"agent": "claude", "session_id": sid, "source": "C:/gone/none.jsonl"}
        self.assertTrue(transcript_located(self.cfg, row))

    def test_locator_finds_codex_archived_transcript(self):
        sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        archive = Path(self.cfg.codex_transcripts_root).parent / "archived_sessions"
        archive.mkdir(parents=True)
        (archive / f"rollout-2026-11-01T00-00-00-{sid}.jsonl.zst").write_bytes(b"\x28\xb5\x2f\xfd")
        row = {"agent": "codex", "session_id": sid, "source": "C:/gone/rollout-x.jsonl"}
        self.assertTrue(transcript_located(self.cfg, row))

    def test_locator_false_when_absent_everywhere(self):
        row = {"agent": "claude", "session_id": "99999999-0000-0000-0000-000000000000",
               "source": "C:/gone/none.jsonl"}
        self.assertFalse(transcript_located(self.cfg, row))

    def test_opencode_session_exists_checks_the_store(self):
        import sqlite3
        db_path = Path(self.tmp.name) / "opencode.db"
        db = sqlite3.connect(db_path)
        db.execute("CREATE TABLE session (id TEXT PRIMARY KEY)")
        db.execute("INSERT INTO session(id) VALUES ('ses_live')")
        db.commit(); db.close()
        self.assertTrue(_opencode_session_exists("ses_live", db_path))
        self.assertFalse(_opencode_session_exists("ses_gone", db_path))
        self.assertIsNone(_opencode_session_exists("ses_live", Path(self.tmp.name) / "missing.db"))

    def test_source_change_and_compacted_are_permanent(self):
        for sid, err in (("s2a", MUTATED), ("s2b", COMPACTED)):
            self._pending(sid)
            self.man.fail("codex", sid, err)
            self.man.fail("codex", sid, err)
            self.assertEqual("lost", self.man.get("codex", sid)["status"], sid)

    def test_transient_error_retries_below_cap(self):
        self._pending("s3")
        self.man.fail("codex", "s3", TRANSIENT)
        row = self.man.get("codex", "s3")
        self.assertEqual("error", row["status"])
        self.assertEqual(1, row["attempts"])
        self.assertGreater(row["next_try"], 0)
        self.assertIn("s3", self._ids(self.man.ready(0, now=row["next_try"] + 1)))

    def test_transient_error_terminal_at_attempt_cap(self):
        self._pending("s4", attempts=self.cfg.max_attempts - 1)
        self.man.fail("codex", "s4", TRANSIENT)
        self.assertEqual("lost", self.man.get("codex", "s4")["status"])

    def test_transient_error_terminal_past_age_limit(self):
        old = time.time() - (self.cfg.error_max_age_days * 86400 + 60)
        self._pending("s5", created=old, last_activity=old)
        self.man.fail("codex", "s5", TRANSIENT)
        self.assertEqual("lost", self.man.get("codex", "s5")["status"])

    def test_reclassify_moves_existing_errors_to_terminal(self):
        self._pending("s6", attempts=30)
        self.man.mark("codex", "s6", status="error", last_error=TRANSIENT)
        self._pending("s7", attempts=2, last_activity=OLD)
        self.man.mark("codex", "s7", status="error", last_error=PERMANENT)
        self._pending("s8", attempts=1)
        self.man.mark("codex", "s8", status="error", last_error=TRANSIENT)
        n = self.man.reclassify_errors(locate=ABSENT)
        self.assertEqual(2, n)
        self.assertEqual("lost", self.man.get("codex", "s6")["status"])
        self.assertEqual("lost", self.man.get("codex", "s7")["status"])
        self.assertEqual("error", self.man.get("codex", "s8")["status"])

    def test_reclassify_keeps_unverified_missing_rows(self):
        """Migration cannot terminalize a missing-transcript row without confirming the absence."""
        self._pending("s7", attempts=2, last_activity=OLD)
        self.man.mark("codex", "s7", status="error", last_error=PERMANENT)
        n = self.man.reclassify_errors()
        self.assertEqual(0, n)
        self.assertEqual("error", self.man.get("codex", "s7")["status"])

    def test_bare_revive_leaves_terminal_rows(self):
        """`mem retry` keeps its old meaning: live errors only. Terminal rows need --session/--include-lost."""
        self._pending("r1", last_activity=OLD)
        self.man.fail("codex", "r1", PERMANENT, exists=ABSENT)
        self.man.fail("codex", "r1", PERMANENT, exists=ABSENT)
        self._pending("r2")
        self.man.fail("codex", "r2", TRANSIENT)
        self.assertEqual(1, self.man.revive())
        self.assertEqual("lost", self.man.get("codex", "r1")["status"])
        self.assertEqual("pending", self.man.get("codex", "r2")["status"])
        self.assertEqual(1, self.man.revive(include_lost=True))
        self.assertEqual("pending", self.man.get("codex", "r1")["status"])

    def test_revive_resets_capture_cursor(self):
        """A revived row restarts from the source start, not the cursor that failed."""
        self._pending("r3", last_activity=OLD)
        self.man.mark("codex", "r3", captured_until=1234, deferred_start=99)
        self.man.fail("codex", "r3", MUTATED)
        self.man.fail("codex", "r3", MUTATED)
        self.assertEqual("lost", self.man.get("codex", "r3")["status"])
        self.man.revive("r3")
        row = self.man.get("codex", "r3")
        self.assertEqual(0, row["captured_until"])
        self.assertIsNone(row["deferred_start"])

    def test_migration_uses_given_policy(self):
        """Configured bounds, not the hard-coded defaults, decide migration."""
        path = Path(self.tmp.name) / "custom.sqlite"
        m1 = Manifest(path)  # bare open: no migration
        m1.touch("codex", "m1", now=0)
        m1.mark("codex", "m1", status="error", attempts=20, last_error=TRANSIENT)
        m1.close()
        m2 = Manifest(path, migrate=RetryPolicy(max_attempts=30, locate=ABSENT))
        self.assertEqual("error", m2.get("codex", "m1")["status"])  # 20 < configured 30
        m2.close()
        m3 = Manifest(path)  # marker now set; no second migration
        self.assertEqual("error", m3.get("codex", "m1")["status"])
        m3.close()

    def test_cli_retry_session_revives_terminal(self):
        from unittest import mock
        from agentmem import cli, worker
        self._pending("s9", last_activity=OLD)
        self.man.fail("codex", "s9", PERMANENT, exists=ABSENT)
        self.man.fail("codex", "s9", PERMANENT, exists=ABSENT)
        self.assertEqual("lost", self.man.get("codex", "s9")["status"])
        with mock.patch.object(worker, "run", return_value={}):
            rc = cli.main(["--vault", str(self.cfg.vault), "retry", "--session", "s9"])
        self.assertEqual(0, rc)
        self.assertEqual("pending", self.man.get("codex", "s9")["status"])

    def test_discovery_backlog_counts_everything_waiting(self):
        """`mem stats` sees unregistered transcripts plus manifest rows, labelled by kind."""
        t0 = 1_000_000.0
        self.man.discovery_since(t0)  # persist the epoch before the transcript's mtime
        hit = self.cfg.claude_transcripts_root / "proj-x" / "99999999-0000-0000-0000-000000000001.jsonl"
        hit.parent.mkdir(parents=True)
        hit.write_text("{}\n", encoding="utf-8")
        os.utime(hit, (t0 + 60, t0 + 60))
        self._pending("w1")
        self.man.fail("codex", "w1", TRANSIENT)
        self._pending("w2")
        backlog = discovery_backlog(self.cfg, self.man, now=t0 + 60 + self.cfg.settle_minutes * 60 + 10)
        self.assertEqual(1, backlog["unregistered"])
        self.assertEqual(1, backlog["retrying"])
        self.assertEqual(1, backlog["pending"])

    def test_stats_shows_terminal_by_cause(self):
        self._pending("sa", last_activity=OLD)
        self.man.fail("codex", "sa", PERMANENT, exists=ABSENT)
        self.man.fail("codex", "sa", PERMANENT, exists=ABSENT)
        self._pending("sb")
        self.man.fail("codex", "sb", MUTATED)
        self.man.fail("codex", "sb", MUTATED)
        s = self.man.stats()
        self.assertEqual(2, s["terminal"]["total"])
        self.assertEqual(1, s["terminal"]["by_cause"]["transcript-missing"])
        self.assertEqual(1, s["terminal"]["by_cause"]["source-mutated"])


if __name__ == "__main__":
    unittest.main()
