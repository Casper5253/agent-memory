"""Session id restore: evidence sources, ambiguity, byte-exact apply, hash refusal, backups. Synthetic only."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import restore_session_ids as rs

SCRATCH = ROOT / ".audit" / "tmp"
UUID_A = "11111111-2222-4333-8444-aaaaaaaaaaaa"
UUID_B = "11111111-2222-4333-8444-bbbbbbbbbbbb"
UUID_C = "11111111-2222-4333-8444-cccccccccccc"
UUID_D1 = "11111111-2222-4333-8444-1111dddddddd"
UUID_D2 = "22222222-2222-4333-8444-2222dddddddd"


def note_bytes(agent, name_date, body, eol=b"\n", part=1):
    lines = [b"---", b"type: session", b"agent: " + agent.encode(), b"session_id: [REDACTED]",
             b'project: "Demo App"', b"date: " + name_date.encode(), b"part: " + str(part).encode(),
             b"immutable: true", b"---", b"", b"# Synthetic note", b"", body.encode()]
    return eol.join(lines) + eol


class RestoreFixture(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.vault = self.tmp / "vault"
        (self.vault / "_meta").mkdir(parents=True)
        (self.vault / "0-Sessions" / "2026" / "10").mkdir(parents=True)
        self.manifest = sqlite3.connect(self.vault / "_meta" / "manifest.sqlite")
        self.manifest.execute("CREATE TABLE captures (agent TEXT, session_id TEXT, part INTEGER, note_path TEXT)")
        self.manifest.commit()
        self.roots = {"claude": self.tmp / "claude-projects", "codex": self.tmp / "codex-sessions"}
        self.opencode = []
        self.out = self.tmp / "restore"

    def close(self):
        self.manifest.close()

    def add_note(self, name, data, capture=None):
        rel = "0-Sessions/2026/10/" + name
        (self.vault / rel).write_bytes(data)
        if capture:
            self.manifest.execute("INSERT INTO captures VALUES (?,?,?,?)", (capture[0], capture[1], capture[2], rel))
            self.manifest.commit()
        return rel

    def plan(self):
        self.addCleanup(self.close)
        return rs.build_plan(self.vault, self.out, roots=self.roots, opencode_sessions=lambda: self.opencode)

    def restored(self, plan):
        return {entry["note"].rsplit("/", 1)[-1]: entry for entry in plan["restored"]}

    def masked(self, plan):
        return {item["note"].rsplit("/", 1)[-1]: item for item in plan["still_masked"]}


class EvidenceSources(RestoreFixture):
    def test_capture_state_restores_the_masked_id(self):
        self.add_note("2026-10-01-1000-claude-demo-aaaaaaaa.md", note_bytes("claude", "2026-10-01", "Body."),
                      capture=("claude", UUID_A, 1))
        plan = self.plan()
        entry = self.restored(plan)["2026-10-01-1000-claude-demo-aaaaaaaa.md"]
        self.assertEqual((entry["session_id"], entry["evidence"]), (UUID_A, "capture-state"))

    def test_transcript_path_in_the_body_restores_the_masked_id(self):
        body = f"- Transcript: `C:/transcripts/{UUID_B}.jsonl` (range 1-40)"
        self.add_note("2026-10-01-1000-claude-demo-bbbbbbbb.md", note_bytes("claude", "2026-10-01", body))
        plan = self.plan()
        entry = self.restored(plan)["2026-10-01-1000-claude-demo-bbbbbbbb.md"]
        self.assertEqual((entry["session_id"], entry["evidence"]), (UUID_B, "transcript-path"))

    def test_unique_transcript_file_on_disk_restores_the_masked_id(self):
        projects = self.roots["claude"] / "demo-project"
        projects.mkdir(parents=True)
        transcript = projects / f"{UUID_C}.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        stamp = datetime(2026, 10, 1, 12, tzinfo=timezone.utc).timestamp()
        os.utime(transcript, (stamp, stamp))
        self.add_note("2026-10-01-1000-claude-demo-cccccccc.md", note_bytes("claude", "2026-10-01", "No ids here."))
        plan = self.plan()
        entry = self.restored(plan)["2026-10-01-1000-claude-demo-cccccccc.md"]
        self.assertEqual((entry["session_id"], entry["evidence"]), (UUID_C, "transcript-file"))

    def test_opencode_session_list_restores_the_masked_id(self):
        created = int(datetime(2026, 10, 1, 9, tzinfo=timezone.utc).timestamp() * 1000)
        self.opencode = [dict(id="ses_demoabcdeffffffff", created=created)]
        self.add_note("2026-10-01-1000-opencode-demo-ffffffff.md",
                      note_bytes("opencode", "2026-10-01", "No ids here."))
        plan = self.plan()
        entry = self.restored(plan)["2026-10-01-1000-opencode-demo-ffffffff.md"]
        self.assertEqual((entry["session_id"], entry["evidence"]), ("ses_demoabcdeffffffff", "transcript-file"))

    def test_two_transcript_files_for_one_suffix_stay_masked(self):
        projects = self.roots["claude"] / "demo-project"
        projects.mkdir(parents=True)
        for uuid in (UUID_D1, UUID_D2):
            (projects / f"{uuid}.jsonl").write_text("{}\n", encoding="utf-8")
            stamp = datetime(2026, 10, 1, 12, tzinfo=timezone.utc).timestamp()
            os.utime(projects / f"{uuid}.jsonl", (stamp, stamp))
        self.add_note("2026-10-01-1000-claude-demo-dddddddd.md", note_bytes("claude", "2026-10-01", "Two files."))
        plan = self.plan()
        self.assertEqual(self.masked(plan)["2026-10-01-1000-claude-demo-dddddddd.md"]["reason"], "ambiguous")

    def test_no_evidence_stays_masked_and_is_listed(self):
        self.add_note("2026-10-01-1000-codex-demo-eeeeeeee.md", note_bytes("codex", "2026-10-01", "Nothing."))
        plan = self.plan()
        self.assertEqual(plan["restored"], [])
        self.assertEqual(self.masked(plan)["2026-10-01-1000-codex-demo-eeeeeeee.md"]["reason"], "no_evidence")


class ByteExactApply(RestoreFixture):
    def test_apply_changes_only_the_session_id_line_and_keeps_crlf(self):
        original = note_bytes("claude", "2026-10-01", "Body with CRLF.", eol=b"\r\n")
        rel = self.add_note("2026-10-01-1000-claude-demo-aaaaaaaa.md", original, capture=("claude", UUID_A, 1))
        self.plan()
        result = rs.apply_plan(self.vault, self.out / "plan.json")
        self.assertEqual(len(result["applied"]), 1)
        new = (self.vault / rel).read_bytes()
        before, after = original.split(b"\r\n"), new.split(b"\r\n")
        self.assertEqual(len(before), len(after))
        changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
        self.assertEqual(changed, [3])
        self.assertEqual(after[3], f'session_id: "{UUID_A}"'.encode())
        self.assertTrue(new.endswith(b"\r\n"))

    def test_hash_changed_since_plan_is_refused_and_left_alone(self):
        rel = self.add_note("2026-10-01-1000-claude-demo-aaaaaaaa.md", note_bytes("claude", "2026-10-01", "Body."),
                            capture=("claude", UUID_A, 1))
        self.plan()
        edited = (self.vault / rel).read_bytes() + b"Edited after planning.\n"
        (self.vault / rel).write_bytes(edited)
        result = rs.apply_plan(self.vault, self.out / "plan.json")
        self.assertEqual(result["applied"], [])
        self.assertEqual(result["refused"], [dict(note=rel, reason="hash_changed_since_plan")])
        self.assertEqual((self.vault / rel).read_bytes(), edited)

    def test_backup_of_each_touched_note_is_kept_outside_the_vault(self):
        original = note_bytes("claude", "2026-10-01", "Body.")
        rel = self.add_note("2026-10-01-1000-claude-demo-aaaaaaaa.md", original, capture=("claude", UUID_A, 1))
        self.plan()
        rs.apply_plan(self.vault, self.out / "plan.json")
        backup = self.out / "backups" / rel
        self.assertEqual(backup.read_bytes(), original)
        self.assertFalse(rs.inside(backup, self.vault))

    def test_plan_and_backups_must_stay_outside_the_vault(self):
        self.add_note("2026-10-01-1000-claude-demo-aaaaaaaa.md", note_bytes("claude", "2026-10-01", "Body."),
                      capture=("claude", UUID_A, 1))
        self.addCleanup(self.close)
        with self.assertRaises(rs.SetupError):
            rs.build_plan(self.vault, self.vault / "plan", roots=self.roots, opencode_sessions=lambda: [])


if __name__ == "__main__":
    unittest.main()
