"""Synthetic tests for WP-03 source import and capture bridge."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import uuid
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentmem.adapters import Event, Transcript
from agentmem.config import Config
from agentmem.knowledge import sources as ksources
from agentmem.knowledge.models import CaptureIdentity
from agentmem.knowledge.policy import sha_text
from agentmem.knowledge.store import KnowledgeStore

SCRATCH = ROOT / ".audit" / "tmp"
SCRATCH.mkdir(parents=True, exist_ok=True)


def _source_packet(ordinal=0, excerpt="Use SQLite.", **changes):
    return {
        "agent": "codex", "session_id": "s1", "part": 1,
        "kind": "transcript_span", "cursor_kind": "bytes",
        "start": ordinal * 100, "end": ordinal * 100 + 50,
        "span_ordinal": ordinal,
        "locator_json": {"format_version": "v1", "provider_record_type": "message",
                         "event_ordinal": ordinal, "message_ids": [],
                         "redactor_version": "agentmem-v1", "truncated": False},
        "original_range_sha": sha_text(excerpt),
        "note_path": "0-Sessions/2026/10/s1.md",
        "content_sha": sha_text(excerpt),
        "recorded_at": "2026-10-22T08:00:00Z",
        "occurred_at": "2026-10-22T08:00:00Z",
        "occurred_precision": "instant",
        "excerpt": excerpt,
        "origin_group": "capture-sha",
        "privacy": "model_allowed",
        "speaker": "user",
        "repo_head": "abcdef1234567890abcdef1234567890abcdef12",
        **changes,
    }


def _receipt(sources=None, project="Alpha", privacy="model_allowed", **changes):
    text = "---\ntype: session\nagent: codex\nsession_id: \"s1\"\nproject: \"Alpha\"\ndate: 2026-10-22\npart: 1\nsummarizer: deterministic\nfiles_modified: 0\nimmutable: true\nrepo_head: null\ntags:\n  - session\n  - agent/codex\n  - project/alpha\n---\n\n# Session\n\nUse SQLite.\n"
    receipt = {
        "version": 1, "agent": "codex", "session_id": "s1", "part": 1,
        "start": 0, "end": 100, "project": project,
        "summary": {"title": "Session", "summary": "summary"},
        "digest_sha": sha_text(text), "note_path": "0-Sessions/2026/10/s1.md",
        "text": text, "content_sha": sha_text(text),
        "when": "2026-10-22T08:00:00Z", "summarizer": "deterministic", "cost": 0,
        "prefix_sha": "", "evidence": "transcript", "related_projects": [],
        "source_warnings": [], "sources": sources or [],
    }
    receipt.update(changes)
    return receipt


class SourceCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        (self.vault / "_meta").mkdir(parents=True)
        (self.vault / "0-Sessions").mkdir(parents=True)
        (self.vault / "1-Projects").mkdir(parents=True)
        (self.vault / "_meta" / "projects.json").write_text(json.dumps({"projects": []}))
        self.db_path = self.vault / "_meta" / "knowledge.sqlite"
        self.store = KnowledgeStore.create(self.db_path)
        self.pid = str(uuid.uuid4())
        self.store.register_project(self.pid, "Alpha", [str(self.root / "alpha")], privacy="model_allowed")

    def tearDown(self):
        self.store.close()
        try:
            for path in self.root.rglob("*.sqlite"):
                if not path.is_file():
                    continue
                with closing(sqlite3.connect(path)) as db:
                    self.assertEqual(db.execute("PRAGMA integrity_check").fetchall(), [("ok",)], str(path))
                    self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [], str(path))
        finally:
            self.temp.cleanup()

    def cfg(self, **kwargs):
        defaults = {"knowledge_mode": "shadow",
                    "knowledge_project_ids": {"Alpha": self.pid},
                    "project_privacy": {"Alpha": "model_allowed"}}
        defaults.update(kwargs)
        return Config(self.vault, **defaults)


class BuildReceiptPackets(SourceCase):
    def test_selects_user_and_final_assistant_text(self):
        t = Transcript(agent="codex", session_id="s1", source="src.jsonl")
        t.events = [
            Event("user", "text", "First prompt."),
            Event("assistant", "text", "First answer.", final=True),
            Event("user", "text", "Second prompt."),
            Event("assistant", "text", "Second answer.", final=True),
            Event("assistant", "command", "git status"),
        ]
        packets = ksources.build_receipt_packets(t)
        self.assertEqual(len(packets), 4)
        self.assertEqual([p["speaker"] for p in packets], ["user", "assistant", "user", "assistant"])

    def test_speaker_classifies_pasted_heuristics(self):
        t = Transcript(agent="codex", session_id="s1", source="src.jsonl")
        t.events = [
            Event("user", "text", "```\nsecret paste\n```"),
            Event("user", "text", "> quoted block"),
            Event("user", "text", "x" * 1600),
            Event("assistant", "text", "Ack.", final=True),
        ]
        packets = ksources.build_receipt_packets(t)
        self.assertEqual([p["speaker"] for p in packets[:3]], ["pasted", "pasted", "pasted"])
        self.assertEqual(packets[-1]["speaker"], "assistant")

    def test_packet_cap_marks_truncated(self):
        t = Transcript(agent="codex", session_id="s1", source="src.jsonl")
        # 10 prompts each near the excerpt limit forces capping.
        for i in range(10):
            t.events.append(Event("user", "text", f"Prompt {i}. " + "word " * 800))
            t.events.append(Event("assistant", "text", f"Answer {i}. " + "word " * 800, final=True))
        packets = ksources.build_receipt_packets(t)
        total = sum(len(p["excerpt"].encode("utf-8")) for p in packets)
        self.assertLessEqual(total, ksources._PACKET_LIMIT)
        if len(packets) < 20:
            self.assertTrue(packets[-1]["locator_json"]["truncated"])

    def test_repo_head_applied_to_all_packets(self):
        head = "1234" * 10
        t = Transcript(agent="codex", session_id="s1", source="src.jsonl")
        t.events = [Event("user", "text", "Hello.")]
        packets = ksources.build_receipt_packets(t, repo_head=head)
        self.assertEqual(packets[0]["repo_head"], head)

    def test_no_raw_transcript_copy_only_redacted_hash(self):
        t = Transcript(agent="codex", session_id="s1", source="src.jsonl")
        t.events = [Event("user", "text", "The password: Password123 is the secret.")]
        packets = ksources.build_receipt_packets(t)
        self.assertNotIn("Password123", packets[0]["excerpt"])
        self.assertEqual(packets[0]["original_range_sha"], sha_text("The password: Password123 is the secret."))


class ImportFromReceipt(SourceCase):
    def test_import_creates_sources_and_extraction_job(self):
        receipt = _receipt(sources=[_source_packet(0), _source_packet(1)])
        cfg = self.cfg()
        outcome = ksources.import_capture_for_receipt(cfg, self.store, receipt)
        self.assertEqual(outcome.status, "imported")
        result = outcome.import_result
        self.assertEqual(len(result.source_ids), 2)
        self.assertIsNotNone(result.extraction_job_id)
        self.assertFalse(result.empty)
        row = self.store.db.execute("SELECT * FROM jobs WHERE id=?", (result.extraction_job_id,)).fetchone()
        self.assertEqual(row["kind"], "extract")

    def test_import_local_only_zero_model_calls(self):
        local_pid = str(uuid.uuid4())
        self.store.register_project(local_pid, "Local", [str(self.root / "local")], privacy="local_only")
        receipt = _receipt(sources=[_source_packet(0)], project="Local")
        cfg = self.cfg(knowledge_project_ids={"Alpha": self.pid, "Local": local_pid},
                       project_privacy={"Alpha": "model_allowed", "Local": "local_only"})
        outcome = ksources.import_capture_for_receipt(cfg, self.store, receipt)
        self.assertEqual(outcome.status, "imported")
        self.assertTrue(outcome.import_result.empty)
        self.assertIsNone(outcome.import_result.extraction_job_id)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)

    def test_import_excluded_is_content_free_skip(self):
        receipt = _receipt(sources=[_source_packet(0)], project="Alpha")
        cfg = self.cfg(project_privacy={"Alpha": "excluded"})
        outcome = ksources.import_capture_for_receipt(cfg, self.store, receipt)
        self.assertEqual(outcome.status, "skipped")
        self.assertEqual(outcome.reason, "project_excluded")
        count = self.store.db.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        self.assertEqual(count, 0)

    def test_legacy_note_source_when_transcript_missing(self):
        text = "---\ntype: session\nagent: codex\nsession_id: \"s1\"\nproject: \"Alpha\"\ndate: 2026-10-22\npart: 1\nsummarizer: legacy\nimmutable: true\n---\n\nLegacy decision: use SQLite.\n"
        receipt = _receipt(sources=[], text=text, content_sha=sha_text(text),
                           digest_sha=sha_text(text), summarizer="legacy", evidence="legacy-note")
        cfg = self.cfg()
        outcome = ksources.import_capture_for_receipt(cfg, self.store, receipt)
        self.assertEqual(outcome.status, "imported")
        source = self.store.get_source(self.pid, outcome.import_result.source_ids[0])
        self.assertEqual(source["kind"], "legacy_note")
        self.assertEqual(source["speaker"], "unknown")

    def test_import_replay_and_changed_fingerprint(self):
        receipt = _receipt(sources=[_source_packet(0)])
        cfg = self.cfg()
        first = ksources.import_capture_for_receipt(cfg, self.store, receipt)
        second = ksources.import_capture_for_receipt(cfg, self.store, receipt)
        self.assertTrue(second.import_result.replayed)
        changed = _receipt(sources=[_source_packet(0)], digest_sha="0" * 64)
        from agentmem.knowledge.models import KnowledgeError
        with self.assertRaises(KnowledgeError) as caught:
            ksources.import_capture_for_receipt(cfg, self.store, changed)
        self.assertEqual(caught.exception.code, "capture_identity_conflict")

    def test_post_capture_seam_skips_when_mode_off(self):
        receipt = _receipt(sources=[_source_packet(0)])
        cfg = self.cfg(knowledge_mode="off", knowledge_db="_meta/other.sqlite")
        outcome = ksources.maybe_import_after_capture(cfg, receipt)
        self.assertEqual(outcome.status, "skipped")
        self.assertFalse((self.vault / "_meta" / "other.sqlite").exists())

    def test_capture_succeeds_when_store_unavailable(self):
        bad_path = self.vault / "_meta" / "bad.sqlite"
        bad_path.mkdir(parents=True, exist_ok=True)
        receipt = _receipt(sources=[_source_packet(0)])
        cfg = self.cfg(knowledge_mode="active", knowledge_db="_meta/bad.sqlite")
        outcome = ksources.maybe_import_after_capture(cfg, receipt)
        self.assertEqual(outcome.status, "error")

    def test_unimported_capture_scan_imports_legacy_note(self):
        from agentmem.manifest import Manifest
        man = Manifest(self.vault / "_meta" / "manifest.sqlite")
        try:
            text = "---\ntype: session\nagent: codex\nsession_id: \"s1\"\nproject: \"Alpha\"\ndate: 2026-10-22\npart: 1\nsummarizer: legacy\nimmutable: true\n---\n\nLegacy.\n"
            note_path = "0-Sessions/2026/10/s1.md"
            os.makedirs(self.vault / "0-Sessions" / "2026" / "10", exist_ok=True)
            (self.vault / note_path).write_text(text, encoding="utf-8")
            digest = sha_text(text)
            man.record_capture(agent="codex", session_id="s1", part=1, note_path=note_path,
                               project="Alpha", title="Legacy", date="2026-10-22",
                               start=0, end=10, summarizer="legacy", cost=0, sha=digest,
                               consolidated=True, summary_json="{}")
            cfg = self.cfg(knowledge_mode="shadow")
            outcomes = ksources.scan_unimported_captures(cfg, man)
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(outcomes[0].status, "imported")
        finally:
            man.close()


class CaptureRegressions(SourceCase):
    def prepare(self, **changes):
        sys.path.insert(0, str(ROOT / "tests"))
        import fixtures as fx
        self.fx = fx
        self.capture_cfg = fx.make_vault(self.root, knowledge_mode="shadow",
            knowledge_project_ids={"Demo App": self.pid}, project_privacy={"Demo App": "model_allowed"}, **changes)
        self.transcript = self.root / "capture.jsonl"
        self.cwd = str(self.root / "work" / "demo-app")
        fx.write_jsonl(self.transcript, fx.claude_lines("capture", self.cwd, fx.SUBSTANTIAL))
        return self.capture_cfg

    def capture(self, cfg, summary=None, merge=None):
        from agentmem import pipeline
        from agentmem.manifest import Manifest
        with closing(Manifest(cfg.manifest_path)) as man:
            man.touch("claude", "capture", cwd=self.cwd, source=str(self.transcript), end=True)
            with patch("agentmem.projects.repo_head", return_value=None):
                result = pipeline.finalize(cfg, man, man.get("claude", "capture"),
                    summary or self.fx.fake_summarize, merge or self.fx.fake_consolidate)
            return result, [dict(row) for row in man.query_captures()]

    def test_private_capture_remember_and_deferred_make_zero_model_calls(self):
        from agentmem import pipeline, remember
        from agentmem.manifest import Manifest
        cfg = self.prepare()
        cfg.project_privacy["Demo App"] = "local_only"
        calls = []
        def forbidden(*args, **kwargs):
            calls.append(args)
            return (self.fx.fake_consolidate if len(args) > 1 else self.fx.fake_summarize)(*args, **kwargs)
        self.assertEqual(self.capture(cfg, forbidden, forbidden)[0], "captured")
        remember.remember_item(cfg, "LOCAL_ONLY_CANARY", cwd=self.cwd, consolidator=forbidden)
        with closing(Manifest(cfg.manifest_path)) as man:
            pipeline.consolidate_pending(cfg, man, summarizer=forbidden, consolidator=forbidden)
        self.assertEqual(calls, [])

    def test_excluded_capture_and_remember_store_only_skip(self):
        from agentmem import remember
        cfg = self.prepare()
        cfg.project_privacy["Demo App"] = "excluded"
        calls = []
        def forbidden(*args, **kwargs):
            calls.append(args)
            return (self.fx.fake_consolidate if len(args) > 1 else self.fx.fake_summarize)(*args, **kwargs)
        outcome, rows = self.capture(cfg, forbidden, forbidden)
        self.assertEqual(outcome, "excluded")
        self.assertEqual(rows, [])
        result = remember.remember_item(cfg, "EXCLUDED_CANARY", cwd=self.cwd, consolidator=forbidden)
        self.assertEqual(result["status"], "excluded")
        self.assertEqual(calls, [])
        self.assertEqual(list(cfg.sessions_dir.rglob("*.md")), [])
        self.assertEqual(list((cfg.meta / "capture-receipts").glob("*.json")), [])

    def test_real_capture_and_resumed_part_import_independently(self):
        cfg = self.prepare()
        self.assertEqual(self.capture(cfg)[0], "captured")
        self.fx.write_jsonl(self.transcript, self.fx.claude_lines("capture", self.cwd, self.fx.SWITCH), append=True)
        self.assertEqual(self.capture(cfg)[0], "captured")
        rows = self.store.db.execute("SELECT part,length(capture_sha) FROM imports ORDER BY part").fetchall()
        self.assertEqual([tuple(row) for row in rows], [(1, 64), (2, 64)])

    def test_mode_off_has_no_packet_dependency_or_new_receipt_fields(self):
        cfg = self.prepare()
        cfg.knowledge_mode = "off"
        with patch.object(ksources, "build_receipt_packets", side_effect=RuntimeError("unavailable")):
            self.assertEqual(self.capture(cfg)[0], "captured")
        receipt = json.loads(next((cfg.meta / "capture-receipts").glob("*.json")).read_text(encoding="utf-8"))
        self.assertNotIn("sources", receipt)
        self.assertNotIn("source_warnings", receipt)

    def test_scan_rejects_bad_receipt_hash_and_manifest_project_mismatch(self):
        from agentmem import pipeline
        from agentmem.manifest import Manifest
        cfg = self.prepare()
        with patch.object(ksources, "_store_for_cfg", side_effect=OSError("offline")):
            self.capture(cfg)
        path = next((cfg.meta / "capture-receipts").glob("*.json"))
        receipt = json.loads(path.read_text(encoding="utf-8"))
        beta = str(uuid.uuid4())
        cfg.knowledge_project_ids["Beta"] = beta
        cfg.project_privacy["Beta"] = "model_allowed"
        receipt["project"] = "Beta"
        with closing(Manifest(cfg.manifest_path)) as man:
            for valid_hash in (False, True):
                if valid_hash:
                    receipt["receipt_sha"] = pipeline._receipt_hash(receipt)
                path.write_text(json.dumps(receipt), encoding="utf-8")
                ksources.scan_unimported_captures(cfg, man, self.store)
                self.assertEqual(self.store.db.execute("SELECT count(*) FROM imports").fetchone()[0], 0)

    def test_packet_hashes_located_raw_bytes_and_full_packet_budget(self):
        from agentmem import adapters
        from agentmem.knowledge.policy import canonical
        cfg = self.prepare()
        t = adapters.read_claude(str(self.transcript))
        packets = ksources.build_receipt_packets(t)
        raw = self.transcript.read_bytes()
        for packet in packets:
            import hashlib
            self.assertEqual(packet["original_range_sha"], hashlib.sha256(raw[packet["start"]:packet["end"]]).hexdigest())
    def test_full_serialized_packet_budget_and_original_order(self):
        from agentmem.knowledge.policy import canonical
        t = Transcript("codex", "s1", "missing-synthetic.jsonl")
        t.events = [Event("user", "text", str(i) + "x" * 5999, ordinal=i) for i in range(10)]
        packets = ksources.build_receipt_packets(t)
        self.assertLessEqual(len(canonical(packets).encode()), 32768)
        self.assertEqual([p["locator_json"]["event_ordinal"] for p in packets],
                         sorted(p["locator_json"]["event_ordinal"] for p in packets))

    def test_interior_fences_and_quotes_are_pasted(self):
        for text in ("Here is data:\n```\nPASTED_CANARY\n```", "Here is a quote:\n> PASTED_CANARY"):
            self.assertEqual(ksources._speaker(Event("user", "text", text)), "pasted")


if __name__ == "__main__":
    unittest.main()
