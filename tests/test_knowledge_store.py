"""Synthetic service-level acceptance for the opt-in knowledge store."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from dataclasses import replace
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agentmem.config import Config
from agentmem.knowledge import policy as p
from agentmem.knowledge.models import (CaptureIdentity, EvidenceSpan, KnowledgeError, ReviewRequest,
                                       SourceInput, ValidationError)
from agentmem.knowledge.store import KnowledgeStore

SCRATCH = ROOT / ".audit" / "tmp"
SCRATCH.mkdir(parents=True, exist_ok=True)


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.root = Path(self.temp.name)
        self.path = self.root / "knowledge.sqlite"
        self.store = KnowledgeStore.create(self.path)
        self.pid = str(uuid.uuid4())
        self.store.register_project(self.pid, "Alpha", [str(self.root / "alpha")], privacy="model_allowed")

    def tearDown(self):
        self.store.close()
        try:
            for path in self.root.rglob("*.sqlite"):
                with closing(sqlite3.connect(path)) as db:
                    self.assertEqual(db.execute("PRAGMA integrity_check").fetchall(), [("ok",)], str(path))
                    self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [], str(path))
        finally:
            self.temp.cleanup()

    def assertCode(self, code, call):
        with self.assertRaises(KnowledgeError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)

    def snapshot(self):
        return tuple(self.store.db.iterdump())

    def source(self, session="session-a", excerpt="Use SQLite for storage.", ordinal=0, **changes):
        source = SourceInput("codex", session, 1, "transcript_span", "bytes", 0, 100, ordinal,
                             {"format_version": "v1", "provider_record_type": "message", "event_ordinal": ordinal,
                              "message_ids": [], "redactor_version": "v1", "truncated": False},
                             None, "0-Sessions/2026/10/synthetic.md", p.sha_text(excerpt),
                             "2026-10-22T08:00:00Z", None, "unknown", excerpt, "origin-a", "model_allowed", "user")
        return replace(source, **changes)

    def imported(self, session="session-a", project_id=None, excerpt="Use SQLite for storage."):
        source = self.source(session, excerpt)
        return self.store.import_capture(project_id or self.pid, CaptureIdentity("codex", session, 1),
                                         p.sha_text(session), [source])

    def payload(self, source_id, value="sqlite", *, predicate="storage_backend", statement=None):
        return {"schema_version": 1,
                "entities": [{"ref": "e1", "kind": "component", "canonical_key": "storage", "display_name": "Storage", "aliases": []}],
                "claims": [{"ref": "c1", "subject_ref": "e1", "kind": "decision", "predicate": predicate,
                            "value": value, "statement": statement or "Use " + value + ".", "rationale": "Atomic writes.",
                            "valid_from": None, "valid_to": None, "valid_precision": "unknown", "valid_end_state": "unknown",
                            "evidence": [{"source_id": source_id, "start": 0, "end": 3, "role": "supports"}]}],
                "relations": []}

    def proposal(self, imported, value="sqlite", **changes):
        payload = self.payload(imported.source_ids[0], value, **changes)
        rev = self.store.get_project(imported.project_id)["revision"]
        proposal_id = self.store.save_proposal(imported.project_id, imported.source_ids[0], payload, rev,
                                               extractor_version="test-" + str(uuid.uuid4()))
        return proposal_id, rev

    def applied(self, imported=None, value="sqlite", **changes):
        imported = imported or self.imported()
        proposal_id, rev = self.proposal(imported, value, **changes)
        return self.store.apply_proposal(imported.project_id, proposal_id, rev, proposal_id)

    def review(self, claim_id, action="accept", **changes):
        claim = self.store.get_claim(self.pid, claim_id)
        request = ReviewRequest(str(uuid.uuid4()), self.pid, action, claim_id,
                                self.store.get_project(self.pid)["revision"], claim["revision"],
                                "operator", "test-operator", "approval-1", "Explicit synthetic review.")
        return replace(request, **changes)

    def test_schema_and_default_off(self):
        tables = {r[0] for r in self.store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {"meta", "projects", "sources", "entities", "claims", "evidence", "relations",
                                  "proposals", "revisions", "operations", "jobs", "outbox", "projections",
                                  "imports", "import_sources"})
        self.assertEqual(Config(self.root).knowledge_mode, "off")
        self.assertEqual(self.store.db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("PRAGMA synchronous").fetchone()[0], 2)
        self.assertEqual(self.store.integrity_check(), "ok")

    def test_read_missing_never_creates(self):
        missing = self.root / "absent" / "knowledge.sqlite"
        self.assertCode("store_unavailable", lambda: KnowledgeStore(missing))
        self.assertFalse(missing.parent.exists())
        before = self.path.read_bytes()
        with KnowledgeStore(self.path) as reader:
            self.assertEqual(reader.get_project(self.pid)["revision"], 0)
            self.assertCode("read_only", lambda: reader.register_project(str(uuid.uuid4()), "B", [], privacy="local_only"))
        self.assertEqual(self.path.read_bytes(), before)

    def test_newer_schema_never_migrates(self):
        self.store.db.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
        before = self.path.read_bytes()
        for writable in (False, True):
            self.assertCode("unsupported_schema", lambda: KnowledgeStore(self.path, writable=writable))
        self.assertEqual(self.path.read_bytes(), before)

    def test_registration_replay_collisions_and_roots(self):
        before = self.snapshot()
        self.store.register_project(self.pid, "Alpha", [str(self.root / "alpha")], privacy="model_allowed")
        self.assertEqual(self.snapshot(), before)
        self.assertCode("project_mapping_conflict", lambda: self.store.register_project(str(uuid.uuid4()), "aLPHA", [], privacy="local_only"))
        self.assertCode("project_mapping_conflict", lambda: self.store.register_project(str(uuid.uuid4()), "Beta", [str(self.root / "alpha")], privacy="local_only"))
        self.assertEqual(self.snapshot(), before)

    def test_backup_restores_in_fresh_connection(self):
        destination = self.root / "backup.sqlite"
        result = self.store.backup(destination)
        self.assertEqual((result.schema_version, result.integrity_result, result.project_revisions[self.pid]), (1, "ok", 0))
        self.assertEqual(result.file_sha, p.hashlib.sha256(destination.read_bytes()).hexdigest())
        with KnowledgeStore(destination) as restored:
            self.assertEqual(restored.get_project(self.pid)["name"], "Alpha")
            self.assertEqual(restored.integrity_check(), "ok")
        second = self.store.backup(self.root / "second-backup.sqlite")
        self.assertNotEqual(second.backup_id, result.backup_id)

    def test_backup_refuses_same_connection_write_transaction(self):
        self.store.db.execute("BEGIN IMMEDIATE")
        try:
            self.assertCode("backup_in_transaction", lambda: self.store.backup(self.root / "unsafe.sqlite"))
            self.assertFalse((self.root / "unsafe.sqlite").exists())
        finally:
            self.store.db.execute("ROLLBACK")

    def test_git_refuses_unignored_and_tracked_paths(self):
        repo = self.root / "vault"
        repo.mkdir()
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        def git(*args):
            return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)
        git("init")
        path = repo / "knowledge.sqlite"
        self.assertCode("unignored_database", lambda: KnowledgeStore.create(path))
        self.assertFalse(path.exists())
        (repo / ".gitignore").write_text("knowledge.sqlite\n", encoding="utf-8")
        with KnowledgeStore.create(path) as fixture:
            self.assertEqual(fixture.integrity_check(), "ok")
        # A tracked artifact is not ignored, even if its filename matches an ignore rule.
        git("add", "-f", "knowledge.sqlite")
        self.assertCode("unignored_database", lambda: KnowledgeStore.create(path))
        backup = repo / "second.sqlite"
        self.assertCode("unignored_database", lambda: self.store.backup(backup))

    def test_invalid_config_and_value_boundaries(self):
        for options in ({"knowledge_mode": "yes"}, {"knowledge_run_calls": -1},
                        {"knowledge_db": "../escape.sqlite"}, {"project_privacy": {"a": "public"}}):
            with self.assertRaises(ValueError):
                Config(self.root, **options)
        for value in (1.5, float("nan"), {"key": "value"}, ["b", "a"]):
            self.assertCode("unsupported_value", lambda: p.value_json(value))
        self.assertCode("invalid_utf8", lambda: p.sha_text("\ud800"))
        self.assertCode("invalid_interval", lambda: p.temporal(None, None, "day", "unknown"))

    def test_import_replay_and_changed_body(self):
        result = self.imported()
        self.assertEqual((result.project_revision, len(result.source_ids), result.empty), (1, 1, False))
        self.assertIsNotNone(result.extraction_job_id)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
        before, changes = self.snapshot(), self.store.db.total_changes
        self.assertEqual(self.imported(), replace(result, replayed=True))
        self.assertEqual(self.store.db.total_changes, changes)
        self.assertCode("idempotency_conflict", lambda: self.imported(excerpt="Different stored bytes."))
        self.assertCode("capture_identity_conflict", lambda: self.store.import_capture(
            self.pid, CaptureIdentity("codex", "session-a", 1), p.sha_text("changed"), [self.source()]))
        self.assertEqual(self.snapshot(), before)
        source_id = p.identity([self.pid, "codex", "session-a", 1, "bytes", 0, 100, 0, p.sha_text("Use SQLite for storage.")])
        self.assertEqual(result.source_ids, (source_id,))

    def test_import_and_model_output_are_redacted_before_database_writes(self):
        email = "private.person@example.com"
        imported = self.imported("private-data", excerpt=f"Use SQLite. BSN 111222333 Contact {email}.")
        source = self.store.get_source(self.pid, imported.source_ids[0])
        self.assertNotIn("111222333", source["excerpt"])
        self.assertNotIn(email, source["excerpt"])
        payload = self.payload(imported.source_ids[0], statement=f"Contact {email} for SQLite.")
        proposal_id = self.store.save_proposal(self.pid, imported.source_ids[0], payload, 1)
        stored = self.store.db.execute("SELECT payload_json FROM proposals WHERE id=?", (proposal_id,)).fetchone()[0]
        self.assertNotIn(email, stored)
        self.assertIn("[REDACTED]", stored)

    def test_empty_and_local_only_imports(self):
        empty = self.store.import_capture(self.pid, CaptureIdentity("codex", "empty", 1), p.sha_text("empty"), [])
        self.assertTrue(empty.empty)
        self.assertEqual(empty.source_ids, ())
        self.assertIsNone(empty.extraction_job_id)
        local_pid = str(uuid.uuid4())
        self.store.register_project(local_pid, "Local", [], privacy="local_only")
        local = self.imported("local", project_id=local_pid)
        self.assertTrue(local.empty)
        self.assertEqual(len(local.source_ids), 1)
        self.assertIsNone(local.extraction_job_id)
        self.assertCode("model_disallowed", lambda: self.proposal(local))

    def test_import_orders_spans_and_checks_limits(self):
        a = self.source(ordinal=2)
        b = self.source(ordinal=1)
        result = self.store.import_capture(self.pid, CaptureIdentity("codex", "session-a", 1), p.sha_text("a"), [a, b])
        self.assertEqual([self.store.get_source(self.pid, sid)["span_ordinal"] for sid in result.source_ids], [1, 2])
        before = self.snapshot()
        invalids = [replace(a, start=-1), replace(a, end=-1), replace(a, content_sha="0" * 64),
                    replace(a, note_path="../outside.md"), replace(a, excerpt="\ud800"),
                    replace(a, occurred_at="2026-10-22", occurred_precision="instant")]
        for invalid in invalids:
            with self.assertRaises(KnowledgeError):
                self.store.import_capture(self.pid, CaptureIdentity("codex", "session-a", 1), p.sha_text("a"), [invalid])
        oversized = self.source("large", "é" * 3001)
        self.assertCode("excerpt_limit", lambda: self.store.import_capture(
            self.pid, CaptureIdentity("codex", "large", 1), p.sha_text("large"), [oversized]))
        packet = [self.source("packet", "x" * 6000, i) for i in range(6)]
        self.assertCode("packet_limit", lambda: self.store.import_capture(
            self.pid, CaptureIdentity("codex", "packet", 1), p.sha_text("packet"), packet))
        self.assertEqual(self.snapshot(), before)

    def test_import_cross_project_and_source_integrity(self):
        result = self.imported()
        other = str(uuid.uuid4())
        self.store.register_project(other, "Other", [], privacy="model_allowed")
        self.assertCode("source_scope", lambda: self.store.get_source(other, result.source_ids[0]))
        self.assertCode("capture_identity_conflict", lambda: self.imported(project_id=other))
        self.store.db.execute("UPDATE sources SET excerpt='corrupted'")
        self.assertCode("source_hash_mismatch", lambda: self.store.get_source(self.pid, result.source_ids[0]))

    def test_import_rolls_back_at_every_write(self):
        count = 0
        def counter():
            nonlocal count
            count += 1
        self.store.fault = counter
        self.imported("count")
        self.store.fault = None
        for stop in range(1, count + 1):
            before = self.snapshot()
            remaining = stop
            def fail():
                nonlocal remaining
                remaining -= 1
                if remaining == 0:
                    raise RuntimeError("injected crash")
            self.store.fault = fail
            with self.assertRaises(RuntimeError):
                self.imported("failure-" + str(stop))
            self.store.fault = None
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(self.store.integrity_check(), "ok")

    def test_model_claim_is_reported_and_replays_zero_writes(self):
        imported = self.imported()
        proposal_id, rev = self.proposal(imported)
        result = self.store.apply_proposal(self.pid, proposal_id, rev, proposal_id)
        self.assertEqual(result.project_revision, 2)
        self.assertEqual(len(result.created_claim_ids), 1)
        claim = self.store.get_claim(self.pid, result.created_claim_ids[0])
        self.assertEqual((claim["status"], claim["evidence_level"], claim["revision"], claim["user_stated"]),
                         ("reported", "reported", 1, True))
        before, changes = self.snapshot(), self.store.db.total_changes
        self.assertEqual(self.store.apply_proposal(self.pid, proposal_id, rev, proposal_id), replace(result, replayed=True))
        self.assertEqual(self.store.db.total_changes, changes)
        self.assertCode("idempotency_conflict", lambda: self.store.apply_proposal(self.pid, proposal_id, rev + 1, proposal_id))
        self.assertEqual(self.snapshot(), before)

    def test_duplicate_origin_never_elevates_authority(self):
        first = self.applied()
        second = self.applied(self.imported("session-b"))
        self.assertEqual(second.created_claim_ids, ())
        self.assertEqual(second.changed_claim_ids, first.created_claim_ids)
        claim = self.store.get_claim(self.pid, first.created_claim_ids[0])
        self.assertEqual((claim["status"], claim["evidence_level"]), ("reported", "reported"))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 2)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM claims").fetchone()[0], 1)

    def test_model_contradicts_relation_stores_edge_without_demoting_claims(self):
        a = self.applied(predicate="storage_backend").created_claim_ids[0]
        self.store.review_claim(self.review(a))
        self.assertEqual(self.store.get_claim(self.pid, a)["status"], "accepted")
        imp = self.imported("contradict-source", excerpt="Postgres is better.")
        payload = self.payload(imp.source_ids[0], "postgres", predicate="other", statement="Postgres claim.")
        rev_a = self.store.get_claim(self.pid, a)["revision"]
        payload["relations"] = [{"from": {"ref": "c1"}, "kind": "contradicts",
                                  "to": {"claim_id": a, "expected_revision": rev_a}, "rationale": "model says so"}]
        rev = self.store.get_project(self.pid)["revision"]
        proposal_id = self.store.save_proposal(self.pid, imp.source_ids[0], payload, rev)
        result = self.store.apply_proposal(self.pid, proposal_id, rev, proposal_id)
        self.assertEqual(self.store.get_claim(self.pid, a)["status"], "accepted")
        rel = self.store.db.execute("SELECT * FROM relations WHERE kind='contradicts'").fetchall()
        self.assertTrue(any(r["from_id"] == result.created_claim_ids[0] and r["to_id"] == a for r in rel))

    def test_retract_requires_operator_approval_and_quarantine_allows_system(self):
        a = self.applied().created_claim_ids[0]
        self.store.review_claim(self.review(a))
        before = self.snapshot()
        self.assertCode("approval_required", lambda: self.store.review_claim(
            self.review(a, "retract", actor_kind="agent", approval_ref=None)))
        self.assertEqual(self.snapshot(), before)
        b = self.applied(self.imported("b"), "postgres", predicate="other").created_claim_ids[0]
        self.store.review_claim(self.review(b))
        before = self.snapshot()
        self.assertCode("approval_required", lambda: self.store.review_claim(
            self.review(b, "quarantine", actor_kind="agent", approval_ref=None)))
        self.assertEqual(self.snapshot(), before)
        self.store.review_claim(self.review(b, "quarantine", actor_kind="system", approval_ref=None))
        self.assertEqual(self.store.get_claim(self.pid, b)["status"], "quarantined")
        c = self.applied(self.imported("c"), "redis", predicate="other").created_claim_ids[0]
        self.store.review_claim(self.review(c))
        self.store.review_claim(self.review(c, "quarantine", actor_kind="operator", approval_ref="q-1"))
        self.assertEqual(self.store.get_claim(self.pid, c)["status"], "quarantined")
        self.store.review_claim(self.review(a, "retract", actor_kind="operator", approval_ref="r-1"))
        self.assertEqual(self.store.get_claim(self.pid, a)["status"], "retracted")

    def test_supersede_boundary_preserves_content_identity(self):
        imported = self.imported()
        payload = self.payload(imported.source_ids[0])
        payload["claims"][0].update(valid_from="2026-10-01", valid_precision="day", valid_end_state="open")
        prop = self.store.save_proposal(self.pid, imported.source_ids[0], payload, 1)
        old = self.store.apply_proposal(self.pid, prop, 1, prop).created_claim_ids[0]
        old_sha = self.store.get_claim(self.pid, old)["content_sha"]
        new = self.applied(self.imported("succ"), "postgres").created_claim_ids[0]
        s = self.store.get_claim(self.pid, new)
        self.store.review_claim(self.review(old, "supersede", successor_claim_id=new,
                                            expected_successor_revision=s["revision"], effective_boundary="2026-10-22"))
        old_claim = self.store.get_claim(self.pid, old)
        self.assertEqual(old_claim["content_sha"], old_sha)
        self.assertEqual((old_claim["valid_from"], old_claim["valid_to"], old_claim["valid_end_state"]),
                         ("2026-10-01", "2026-10-22", "closed"))
        imp3 = self.imported("again")
        p3 = self.payload(imp3.source_ids[0])
        p3["claims"][0].update(valid_from="2026-10-01", valid_precision="day", valid_end_state="open")
        rev = self.store.get_project(self.pid)["revision"]
        pr = self.store.save_proposal(self.pid, imp3.source_ids[0], p3, rev)
        result = self.store.apply_proposal(self.pid, pr, rev, pr)
        self.assertIn(old, result.changed_claim_ids)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM claims WHERE id=?", (old,)).fetchone()[0], 1)

    def test_accept_conflict_member_returns_unresolved_conflict_and_writes_nothing(self):
        a = self.applied().created_claim_ids[0]
        b = self.applied(self.imported("b", excerpt="Use Postgres for storage."), "postgres").created_claim_ids[0]
        a_rev = self.store.get_claim(self.pid, a)["revision"]
        self.assertEqual(self.store.get_claim(self.pid, a)["status"], "disputed")
        before = self.snapshot()
        rev_before = self.store.get_project(self.pid)["revision"]
        changes = self.store.db.total_changes
        self.assertCode("unresolved_conflict", lambda: self.store.review_claim(self.review(a)))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.store.db.total_changes, changes)
        self.assertEqual(self.store.get_project(self.pid)["revision"], rev_before)
        self.assertEqual(self.store.get_claim(self.pid, a)["revision"], a_rev)
        self.assertCode("unresolved_conflict", lambda: self.store.review_claim(self.review(b)))

    def test_accept_requires_explicit_operator_and_evidence(self):
        result = self.applied()
        claim_id = result.created_claim_ids[0]
        request = self.review(claim_id)
        before = self.snapshot()
        for invalid in (replace(request, actor_kind="agent"), replace(request, approval_ref=None), replace(request, reason="")):
            with self.assertRaises(KnowledgeError):
                self.store.review_claim(invalid)
        self.assertEqual(self.snapshot(), before)
        accepted = self.store.review_claim(request)
        claim = self.store.get_claim(self.pid, claim_id)
        self.assertEqual((claim["status"], claim["evidence_level"], claim["revision"]), ("accepted", "reported", 2))
        self.assertIsNotNone(claim["reviewed_at"])
        changes = self.store.db.total_changes
        self.assertEqual(self.store.review_claim(request), replace(accepted, replayed=True))
        self.assertEqual(self.store.db.total_changes, changes)
        self.assertCode("idempotency_conflict", lambda: self.store.review_claim(replace(request, reason="Another reason.")))

    def test_stale_claim_or_project_revision_writes_nothing(self):
        claim_id = self.applied().created_claim_ids[0]
        request = self.review(claim_id)
        for invalid in (replace(request, expected_project_revision=0), replace(request, expected_claim_revision=99)):
            before, changes = self.snapshot(), self.store.db.total_changes
            self.assertCode("revision_conflict", lambda: self.store.review_claim(invalid))
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(self.store.db.total_changes, changes)

    def test_conflict_accept_and_supersede(self):
        old_id = self.applied().created_claim_ids[0]
        self.store.review_claim(self.review(old_id))
        newer = self.applied(self.imported("new", excerpt="Use Postgres for storage."), "postgres")
        new_id = newer.created_claim_ids[0]
        self.assertEqual(set(newer.conflict_ids), {old_id, new_id})
        self.assertEqual(self.store.get_claim(self.pid, old_id)["status"], "disputed")
        before = self.snapshot()
        self.assertCode("unresolved_conflict", lambda: self.store.review_claim(self.review(new_id)))
        self.assertEqual(self.snapshot(), before)
        old = self.store.get_claim(self.pid, old_id)
        new = self.store.get_claim(self.pid, new_id)
        request = self.review(old_id, "supersede", successor_claim_id=new_id, expected_successor_revision=new["revision"])
        result = self.store.review_claim(request)
        self.assertEqual(result.conflict_ids, ())
        self.assertEqual(self.store.get_claim(self.pid, old_id)["status"], "superseded")
        self.assertEqual(self.store.get_claim(self.pid, new_id)["status"], "accepted")
        history = self.store.db.execute("SELECT snapshot_json FROM revisions WHERE claim_id=? ORDER BY claim_revision", (old_id,)).fetchall()
        self.assertEqual([json.loads(row[0])["status"] for row in history], ["reported", "accepted", "disputed", "superseded"])

    def test_restore_requires_fresh_evidence_and_retains_history(self):
        result = self.applied()
        claim_id = result.created_claim_ids[0]
        self.store.review_claim(self.review(claim_id, "retract"))
        old_source = self.store.db.execute("SELECT source_id FROM evidence WHERE claim_id=?", (claim_id,)).fetchone()[0]
        request = self.review(claim_id, "restore", target_status="reported", evidence=(EvidenceSpan(old_source, 0, 3),))
        before = self.snapshot()
        self.assertCode("fresh_evidence_required", lambda: self.store.review_claim(request))
        self.assertEqual(self.snapshot(), before)
        imported = self.imported("fresh")
        request = self.review(claim_id, "restore", target_status="reported", evidence=(EvidenceSpan(imported.source_ids[0], 0, 3),))
        restored = self.store.review_claim(request)
        self.assertEqual(self.store.get_claim(self.pid, claim_id)["status"], "reported")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM revisions WHERE claim_id=?", (claim_id,)).fetchone()[0], 3)

    def test_proposal_unknown_keys_and_bad_spans_fail_atomically(self):
        imported = self.imported()
        for location in ("top", "claim", "entity", "evidence"):
            payload = self.payload(imported.source_ids[0])
            target = {"top": payload, "claim": payload["claims"][0], "entity": payload["entities"][0],
                      "evidence": payload["claims"][0]["evidence"][0]}[location]
            target["status"] = "accepted"
            before = self.snapshot()
            with self.assertRaises(ValidationError):
                self.store.save_proposal(self.pid, imported.source_ids[0], payload, 1)
            self.assertEqual(self.snapshot(), before)
        payload = self.payload(imported.source_ids[0])
        payload["claims"][0]["evidence"][0]["end"] = 9999
        self.assertCode("evidence_range", lambda: self.store.save_proposal(self.pid, imported.source_ids[0], payload, 1))
        other = self.imported("other")
        payload = self.payload(other.source_ids[0])
        self.assertCode("evidence_scope", lambda: self.store.save_proposal(self.pid, imported.source_ids[0], payload, 2))

    def test_proposal_stale_and_cross_project_endpoints(self):
        imported = self.imported()
        proposal_id, rev = self.proposal(imported)
        self.imported("advanced")
        before, changes = self.snapshot(), self.store.db.total_changes
        self.assertCode("revision_conflict", lambda: self.store.apply_proposal(self.pid, proposal_id, rev, proposal_id))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.store.db.total_changes, changes)
        other = str(uuid.uuid4())
        self.store.register_project(other, "Beta", [], privacy="model_allowed")
        foreign = self.applied(self.imported("foreign", project_id=other)).created_claim_ids[0]
        payload = self.payload(imported.source_ids[0])
        payload["relations"] = [{"from": {"ref": "c1"}, "kind": "derived_from",
                                  "to": {"claim_id": foreign, "expected_revision": 1}, "rationale": "Cross scope."}]
        rev = self.store.get_project(self.pid)["revision"]
        proposal_id = self.store.save_proposal(self.pid, imported.source_ids[0], payload, rev)
        before = self.snapshot()
        self.assertCode("claim_scope", lambda: self.store.apply_proposal(self.pid, proposal_id, rev, proposal_id))
        self.assertEqual(self.snapshot(), before)

    def test_cycles_reject_entire_proposal(self):
        imported = self.imported()
        payload = self.payload(imported.source_ids[0])
        other = dict(payload["claims"][0], ref="c2", predicate="other", statement="Different statement.")
        payload["claims"].append(other)
        payload["relations"] = [{"from": {"ref": "c1"}, "kind": "derived_from", "to": {"ref": "c2"}, "rationale": "First edge."},
                                 {"from": {"ref": "c2"}, "kind": "depends_on", "to": {"ref": "c1"}, "rationale": "Cycle."}]
        proposal_id = self.store.save_proposal(self.pid, imported.source_ids[0], payload, 1)
        before = self.snapshot()
        self.assertCode("relation_cycle", lambda: self.store.apply_proposal(self.pid, proposal_id, 1, proposal_id))
        self.assertEqual(self.snapshot(), before)

    def test_apply_and_review_rollback_after_each_write(self):
        imported = self.imported()
        proposal_id, rev = self.proposal(imported)
        count = 0
        def counter():
            nonlocal count
            count += 1
        self.store.fault = counter
        applied = self.store.apply_proposal(self.pid, proposal_id, rev, proposal_id)
        self.store.fault = None
        total = count
        for stop in range(1, total + 1):
            proposal_id, rev = self.proposal(imported, "value-" + str(stop), predicate="nonexclusive")
            before = self.snapshot()
            remaining = stop
            def fail():
                nonlocal remaining
                remaining -= 1
                if remaining == 0:
                    raise RuntimeError("injected")
            self.store.fault = fail
            with self.assertRaises(RuntimeError):
                self.store.apply_proposal(self.pid, proposal_id, rev, proposal_id)
            self.store.fault = None
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(self.store.integrity_check(), "ok")
        claim_id = applied.created_claim_ids[0]
        count = 0
        self.store.fault = counter
        self.store.review_claim(self.review(claim_id))
        self.store.fault = None
        for stop in range(1, count + 1):
            before = self.snapshot()
            remaining = stop
            def fail2():
                nonlocal remaining
                remaining -= 1
                if remaining == 0:
                    raise RuntimeError("injected")
            self.store.fault = fail2
            with self.assertRaises(RuntimeError):
                self.store.review_claim(self.review(claim_id, "quarantine"))
            self.store.fault = None
            self.assertEqual(self.snapshot(), before)

        def rollback_each_write(prepare, act):
            writes = 0
            def writer_counter():
                nonlocal writes
                writes += 1
            prepare()
            self.store.fault = writer_counter
            act()
            self.store.fault = None
            total = writes
            for stop in range(1, total + 1):
                remaining = stop
                def writer_fail():
                    nonlocal remaining
                    remaining -= 1
                    if remaining == 0:
                        raise RuntimeError("injected")
                prepare()
                before = self.snapshot()
                self.store.fault = writer_fail
                with self.assertRaises(RuntimeError):
                    act()
                self.store.fault = None
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.store.integrity_check(), "ok")

        seed_imp = self.imported("conflict-seed")
        seed_prop, seed_rev = self.proposal(seed_imp, "postgres", predicate="storage_backend")
        self.store.apply_proposal(self.pid, seed_prop, seed_rev, seed_prop)
        state = {}
        def prepare_conflict():
            imp = self.imported("conflict-" + str(len(state)))
            prop, rev = self.proposal(imp, "postgres", predicate="storage_backend")
            state["proposal_id"] = prop
            state["rev"] = rev
        def act_conflict():
            self.store.apply_proposal(self.pid, state["proposal_id"], state["rev"], state["proposal_id"])
        rollback_each_write(prepare_conflict, act_conflict)

        target_imp = self.imported("edge-target")
        target_prop, target_rev = self.proposal(target_imp, "target", predicate="nonexclusive")
        target_result = self.store.apply_proposal(self.pid, target_prop, target_rev, target_prop)
        target_id = target_result.created_claim_ids[0]
        self.store._write("UPDATE claims SET status='disputed' WHERE id=?", (target_id,))
        state.clear()
        def prepare_edge():
            imp = self.imported("edge-" + str(len(state)))
            payload = self.payload(imp.source_ids[0], "edge", predicate="other", statement="Edge claim.")
            rev_target = self.store.get_claim(self.pid, target_id)["revision"]
            payload["relations"] = [{"from": {"ref": "c1"}, "kind": "contradicts",
                                      "to": {"claim_id": target_id, "expected_revision": rev_target},
                                      "rationale": "edge"}]
            rev = self.store.get_project(self.pid)["revision"]
            prop = self.store.save_proposal(self.pid, imp.source_ids[0], payload, rev)
            state["proposal_id"] = prop
            state["rev"] = rev
        def act_edge():
            self.store.apply_proposal(self.pid, state["proposal_id"], state["rev"], state["proposal_id"])
        rollback_each_write(prepare_edge, act_edge)

        base_imp = self.imported("base-supersede")
        base_payload = self.payload(base_imp.source_ids[0], value="base", predicate="nonexclusive", statement="Base claim.")
        base_payload["claims"][0].update(valid_from="2026-10-01", valid_precision="day", valid_end_state="open")
        base_rev = self.store.get_project(self.pid)["revision"]
        base_prop = self.store.save_proposal(self.pid, base_imp.source_ids[0], base_payload, base_rev)
        base_id = self.store.apply_proposal(self.pid, base_prop, base_rev, base_prop).created_claim_ids[0]
        self.store.review_claim(self.review(base_id))
        succ_imp = self.imported("succ")
        succ_payload = self.payload(succ_imp.source_ids[0], value="postgres", predicate="nonexclusive", statement="Successor claim.")
        succ_rev = self.store.get_project(self.pid)["revision"]
        succ_prop = self.store.save_proposal(self.pid, succ_imp.source_ids[0], succ_payload, succ_rev)
        new_id = self.store.apply_proposal(self.pid, succ_prop, succ_rev, succ_prop).created_claim_ids[0]
        state.clear()
        def prepare_supersede():
            self.store._write("UPDATE claims SET status='accepted',reviewed_at=NULL WHERE id=?", (base_id,))
            self.store._write("DELETE FROM relations WHERE from_id=? AND kind='supersedes' AND to_id=?", (new_id, base_id))
            new = self.store.get_claim(self.pid, new_id)
            req = self.review(base_id, "supersede", successor_claim_id=new_id,
                              expected_successor_revision=new["revision"], effective_boundary="2026-10-22")
            state["request"] = req
        def act_supersede():
            self.store.review_claim(state["request"])
        rollback_each_write(prepare_supersede, act_supersede)

        conflict_a = self.applied(self.imported("conflict-a"), "redis").created_claim_ids[0]
        self.applied(self.imported("comp"), "postgres")
        before = self.snapshot()
        writes = 0
        def conflict_counter():
            nonlocal writes
            writes += 1
        self.store.fault = conflict_counter
        self.assertCode("unresolved_conflict", lambda: self.store.review_claim(self.review(conflict_a)))
        self.store.fault = None
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(writes, 0)

    def test_busy_exhaustion_is_typed_and_writes_nothing(self):
        with KnowledgeStore(self.path, writable=True, timeout=0.01) as other:
            self.store.db.execute("BEGIN IMMEDIATE")
            before = tuple(other.db.iterdump())
            try:
                self.assertCode("store_busy", lambda: other.import_capture(
                    self.pid, CaptureIdentity("codex", "busy", 1), p.sha_text("busy"), []))
            finally:
                self.store.db.execute("ROLLBACK")
            self.assertEqual(tuple(other.db.iterdump()), before)

    def test_orphan_foreign_keys_fail(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("INSERT INTO import_sources VALUES ('missing','missing',0)")

    def test_parallel_processes_exactly_one_revision_wins(self):
        claim_id = self.applied().created_claim_ids[0]
        request = self.review(claim_id)
        script = self.root / "compete.py"
        script.write_text(
            "import sys,json\nfrom pathlib import Path\nsys.path.insert(0,sys.argv[1])\n"
            "from agentmem.knowledge.store import KnowledgeStore\n"
            "from agentmem.knowledge.models import ReviewRequest,KnowledgeError\n"
            "with KnowledgeStore(sys.argv[2],writable=True,timeout=5) as store:\n"
            " try:\n  result=store.review_claim(ReviewRequest(**json.loads(sys.argv[3])))\n  print('committed')\n"
            " except KnowledgeError as exc:\n  print(exc.code)\n", encoding="utf-8")
        from dataclasses import asdict
        processes = [subprocess.Popen([sys.executable, "-B", "-I", str(script), str(ROOT), str(self.path),
                                       json.dumps(asdict(replace(request, operation_id=str(uuid.uuid4()))))],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        outputs = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, stderr)
            outputs.append(stdout.strip())
        self.assertCountEqual(outputs, ["committed", "revision_conflict"])
        self.assertEqual(self.store.get_project(self.pid)["revision"], request.expected_project_revision + 1)
        self.assertEqual(self.store.get_claim(self.pid, claim_id)["revision"], request.expected_claim_revision + 1)

    def test_parallel_imports_commit_once_and_replay_once(self):
        script = self.root / "import_compete.py"
        script.write_text(
            "import sys\nsys.path.insert(0,sys.argv[1])\n"
            "from agentmem.knowledge.store import KnowledgeStore\n"
            "from agentmem.knowledge.models import CaptureIdentity\n"
            "with KnowledgeStore(sys.argv[2],writable=True,timeout=5) as store:\n"
            " result=store.import_capture(sys.argv[3],CaptureIdentity('codex','parallel-import',1),'0'*64,[])\n"
            " print('replayed' if result.replayed else 'committed')\n", encoding="utf-8")
        processes = [subprocess.Popen([sys.executable, "-B", "-I", str(script), str(ROOT), str(self.path), self.pid],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        outputs = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, stderr)
            outputs.append(stdout.strip())
        self.assertCountEqual(outputs, ["committed", "replayed"])
        self.assertEqual(self.store.get_project(self.pid)["revision"], 1)

    def test_abrupt_process_exit_at_each_write_recovers_old_revision(self):
        from dataclasses import asdict
        claim_id = self.applied().created_claim_ids[0]
        request = self.review(claim_id)
        before = self.snapshot()
        script = self.root / "crash.py"
        script.write_text(
            "import sys,json,os\nsys.path.insert(0,sys.argv[1])\n"
            "from agentmem.knowledge.store import KnowledgeStore\n"
            "from agentmem.knowledge.models import ReviewRequest\n"
            "remaining=int(sys.argv[4])\n"
            "def crash():\n global remaining\n remaining-=1\n if remaining==0: os._exit(75)\n"
            "with KnowledgeStore(sys.argv[2],writable=True,fault=crash) as store:\n"
            " store.review_claim(ReviewRequest(**json.loads(sys.argv[3])))\n", encoding="utf-8")
        crashes = 0
        for stop in range(1, 30):
            result = subprocess.run([sys.executable, "-B", "-I", str(script), str(ROOT), str(self.path),
                                     json.dumps(asdict(request)), str(stop)], capture_output=True, text=True, timeout=15)
            if result.returncode == 0:
                break
            self.assertEqual(result.returncode, 75, result.stderr)
            crashes += 1
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(self.store.integrity_check(), "ok")
        else:
            self.fail("Review never reached a complete commit.")
        self.assertGreaterEqual(crashes, 6)
        self.assertEqual(self.store.get_claim(self.pid, claim_id)["status"], "accepted")
        self.assertEqual(self.store.get_project(self.pid)["revision"], request.expected_project_revision + 1)

    def assertAtomic(self, operation):
        before = self.snapshot()
        for stop in range(1, 100):
            remaining = stop
            def fail():
                nonlocal remaining
                remaining -= 1
                if remaining == 0:
                    raise RuntimeError("injected")
            self.store.fault = fail
            try:
                result = operation()
            except RuntimeError:
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.store.integrity_check(), "ok")
            else:
                self.assertGreater(stop, 1)
                return result
            finally:
                self.store.fault = None
        self.fail("Operation never committed.")

    def test_supersede_and_restore_are_atomic_with_remaining_competitor(self):
        old_id = self.applied().created_claim_ids[0]
        new_id = self.applied(self.imported("second"), "postgres").created_claim_ids[0]
        third_id = self.applied(self.imported("third"), "redis").created_claim_ids[0]
        successor = self.store.get_claim(self.pid, new_id)
        request = self.review(old_id, "supersede", successor_claim_id=new_id,
                              expected_successor_revision=successor["revision"])
        result = self.assertAtomic(lambda: self.store.review_claim(request))
        self.assertEqual(set(result.conflict_ids), {new_id, third_id})
        self.assertEqual(self.store.get_claim(self.pid, new_id)["status"], "disputed")
        self.assertEqual(self.store.get_claim(self.pid, old_id)["status"], "superseded")
        self.store.review_claim(self.review(new_id, "quarantine"))
        fresh = self.imported("fresh-for-restore")
        request = self.review(new_id, "restore", target_status="accepted", evidence=(EvidenceSpan(fresh.source_ids[0], 0, 3),))
        result = self.assertAtomic(lambda: self.store.review_claim(request))
        self.assertEqual(set(result.conflict_ids), {new_id, third_id})
        self.assertEqual(self.store.get_claim(self.pid, new_id)["status"], "disputed")

    def test_temporal_fractional_seconds_and_nonoverlapping_intervals(self):
        p.temporal("2026-10-22T00:00:00Z", "2026-10-22T00:00:00.1Z", "instant", "closed")
        p.temporal("2026-10-22T00:00:00.0000001Z", "2026-10-22T00:00:00.0000002+00:00", "instant", "closed")
        self.assertCode("invalid_interval", lambda: p.temporal(
            "2026-10-22T00:00:00.10Z", "2026-10-22T00:00:00.1+00:00", "instant", "closed"))
        self.assertCode("invalid_interval", lambda: p.temporal(
            "2026-10-22T00:00:00.1Z", "2026-10-22T00:00:00Z", "instant", "closed"))
        imported = self.imported()
        payload = self.payload(imported.source_ids[0])
        payload["claims"][0].update(valid_from="2026-10-01", valid_to="2026-10-10", valid_precision="day", valid_end_state="closed")
        proposal_id = self.store.save_proposal(self.pid, imported.source_ids[0], payload, 1)
        old_id = self.store.apply_proposal(self.pid, proposal_id, 1, proposal_id).created_claim_ids[0]
        second = self.imported("time-second")
        payload = self.payload(second.source_ids[0], "postgres")
        payload["claims"][0].update(valid_from="2026-10-10", valid_to=None, valid_precision="day", valid_end_state="open")
        rev = self.store.get_project(self.pid)["revision"]
        proposal_id = self.store.save_proposal(self.pid, second.source_ids[0], payload, rev)
        result = self.store.apply_proposal(self.pid, proposal_id, rev, proposal_id)
        self.assertEqual(result.conflict_ids, ())
        self.assertEqual(self.store.get_claim(self.pid, old_id)["status"], "reported")

    def test_supersede_explicit_boundary_changes_only_predecessor_interval(self):
        imported = self.imported()
        payload = self.payload(imported.source_ids[0])
        payload["claims"][0].update(valid_from="2026-10-01", valid_precision="day", valid_end_state="open")
        proposal_id = self.store.save_proposal(self.pid, imported.source_ids[0], payload, 1)
        old_id = self.store.apply_proposal(self.pid, proposal_id, 1, proposal_id).created_claim_ids[0]
        old_sha = self.store.get_claim(self.pid, old_id)["content_sha"]
        new_id = self.applied(self.imported("time-successor"), "postgres").created_claim_ids[0]
        successor = self.store.get_claim(self.pid, new_id)
        self.store.review_claim(self.review(old_id, "supersede", successor_claim_id=new_id,
                                            expected_successor_revision=successor["revision"], effective_boundary="2026-10-22"))
        old = self.store.get_claim(self.pid, old_id)
        self.assertEqual((old["valid_from"], old["valid_to"], old["valid_end_state"]), ("2026-10-01", "2026-10-22", "closed"))
        self.assertEqual(old["content_sha"], old_sha)
        self.assertIsNone(self.store.get_claim(self.pid, new_id)["valid_to"])

    def test_scope_identity_excluded_and_archived_guards(self):
        alpha_id = self.applied().created_claim_ids[0]
        other = str(uuid.uuid4())
        self.store.register_project(other, "Beta", [], privacy="model_allowed")
        beta_id = self.applied(self.imported("beta-session", project_id=other)).created_claim_ids[0]
        alpha = self.store.get_claim(self.pid, alpha_id)
        beta = self.store.get_claim(other, beta_id)
        self.assertNotEqual(alpha["subject_id"], beta["subject_id"])
        self.assertNotEqual(alpha_id, beta_id)
        request = self.review(alpha_id)
        self.assertCode("claim_scope", lambda: self.store.review_claim(replace(request, project_id=other,
                                                                               expected_project_revision=2)))
        excluded = str(uuid.uuid4())
        self.store.register_project(excluded, "Excluded", [], privacy="excluded")
        before = self.snapshot()
        self.assertCode("project_ineligible", lambda: self.imported("excluded", project_id=excluded))
        self.assertEqual(self.snapshot(), before)
        self.store.db.execute("UPDATE projects SET archived=1 WHERE id=?", (self.pid,))
        archived = self.snapshot()
        self.assertCode("project_ineligible", lambda: self.store.review_claim(request))
        self.assertEqual(self.snapshot(), archived)

    def test_source_spans_use_unicode_codepoints_and_quote_hash(self):
        imported = self.imported(excerpt="é💡 SQLite")
        payload = self.payload(imported.source_ids[0])
        payload["claims"][0]["evidence"][0].update(start=1, end=2)
        proposal_id = self.store.save_proposal(self.pid, imported.source_ids[0], payload, 1)
        self.store.apply_proposal(self.pid, proposal_id, 1, proposal_id)
        quote = self.store.db.execute("SELECT quote_sha FROM evidence").fetchone()[0]
        self.assertEqual(quote, p.sha_text("💡"))

    def test_existing_unversioned_store_is_unsupported_without_writes(self):
        path = self.root / "old.sqlite"
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE legacy (id TEXT)")
            db.commit()
        before = path.read_bytes()
        self.assertCode("unsupported_schema", lambda: KnowledgeStore(path))
        self.assertEqual(path.read_bytes(), before)

    def test_backup_during_wal_write_is_a_complete_committed_snapshot(self):
        self.applied()
        self.store.db.execute("PRAGMA journal_mode=WAL")
        other = sqlite3.connect(self.path, isolation_level=None)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute("UPDATE projects SET revision=revision+1 WHERE id=?", (self.pid,))
            result = self.store.backup(self.root / "live-backup.sqlite")
            self.assertEqual(result.project_revisions[self.pid], 2)
            other.execute("ROLLBACK")
        finally:
            other.close()
        with KnowledgeStore(self.root / "live-backup.sqlite") as restored:
            self.assertEqual(restored.get_project(self.pid)["revision"], 2)
            self.assertEqual(restored.integrity_check(), "ok")

    def test_accept_without_support_fails_without_partial_rows(self):
        claim_id = self.applied().created_claim_ids[0]
        self.store.db.execute("DELETE FROM evidence WHERE claim_id=?", (claim_id,))
        request = self.review(claim_id)
        before = self.snapshot()
        self.assertCode("missing_support", lambda: self.store.review_claim(request))
        self.assertEqual(self.snapshot(), before)

    def test_review_snapshot_retains_reason_actor_and_approval(self):
        claim_id = self.applied().created_claim_ids[0]
        request = self.review(claim_id)
        self.store.review_claim(request)
        row = self.store.db.execute("SELECT snapshot_json FROM revisions WHERE claim_id=? AND claim_revision=2", (claim_id,)).fetchone()
        review = json.loads(row[0])["review"]
        self.assertEqual((review["reason"], review["actor_id"], review["approval_ref"]),
                         (request.reason, request.actor_id, request.approval_ref))

    def test_stale_existing_endpoint_and_changed_source_are_checked_before_writes(self):
        imported = self.imported()
        claim_id = self.applied(imported).created_claim_ids[0]
        self.store.review_claim(self.review(claim_id))
        payload = self.payload(imported.source_ids[0], predicate="nonexclusive", statement="Dependent claim.")
        payload["relations"] = [{"from": {"ref": "c1"}, "kind": "derived_from",
                                  "to": {"claim_id": claim_id, "expected_revision": 1}, "rationale": "Old revision."}]
        rev = self.store.get_project(self.pid)["revision"]
        proposal_id = self.store.save_proposal(self.pid, imported.source_ids[0], payload, rev)
        before, changes = self.snapshot(), self.store.db.total_changes
        self.assertCode("revision_conflict", lambda: self.store.apply_proposal(self.pid, proposal_id, rev, proposal_id))
        self.assertEqual(self.store.db.total_changes, changes)
        self.assertEqual(self.snapshot(), before)
        proposal_id, rev = self.proposal(imported, "another")
        self.store.db.execute("UPDATE sources SET excerpt='wrong bytes' WHERE id=?", (imported.source_ids[0],))
        before, changes = self.snapshot(), self.store.db.total_changes
        self.assertCode("source_hash_mismatch", lambda: self.store.apply_proposal(self.pid, proposal_id, rev, proposal_id))
        self.assertEqual(self.store.db.total_changes, changes)
        self.assertEqual(self.snapshot(), before)

    def test_graph_traversal_limit_rejects_atomically(self):
        imported = self.imported()
        template = self.applied(imported).created_claim_ids[0]
        ids = [str(uuid.uuid4()) for _ in range(10001)]
        self.store.db.execute("BEGIN IMMEDIATE")
        try:
            self.store.db.executemany(
                "INSERT INTO claims SELECT ?,project_id,subject_id,kind,predicate,value_json,statement,rationale,status,"
                "evidence_level,valid_from,valid_to,valid_precision,valid_end_state,recorded_at,reviewed_at,review_due_at,"
                "revision,content_sha FROM claims WHERE id=?", [(claim_id, template) for claim_id in ids])
            self.store.db.executemany("INSERT INTO relations VALUES (?,?,'derived_from',?,'Synthetic chain.')",
                                      [(self.pid, left, right) for left, right in zip(ids, ids[1:])])
            self.store.db.execute("COMMIT")
        except BaseException:
            self.store.db.execute("ROLLBACK")
            raise
        payload = self.payload(imported.source_ids[0], predicate="other", statement="Bounded traversal.")
        payload["relations"] = [{"from": {"ref": "c1"}, "kind": "depends_on",
                                  "to": {"claim_id": ids[0], "expected_revision": 1}, "rationale": "Traversal probe."}]
        rev = self.store.get_project(self.pid)["revision"]
        proposal_id = self.store.save_proposal(self.pid, imported.source_ids[0], payload, rev)
        before = self.snapshot()
        self.assertCode("graph_limit", lambda: self.store.apply_proposal(self.pid, proposal_id, rev, proposal_id))
        self.assertEqual(self.snapshot(), before)

    def test_packet_metadata_and_raw_model_output_are_bounded(self):
        packet = [self.source("many-empty", "", i) for i in range(100)]
        before = self.snapshot()
        self.assertCode("packet_limit", lambda: self.store.import_capture(
            self.pid, CaptureIdentity("codex", "many-empty", 1), p.sha_text("many-empty"), packet))
        self.assertEqual(self.snapshot(), before)
        oversized = " " * (32 * 1024) + '{"schema_version":1,"entities":[],"claims":[],"relations":[]}'
        self.assertCode("proposal_limit", lambda: p.validate_proposal(oversized))
        self.assertCode("invalid_utf8", lambda: p.validate_proposal(b'\xff'))


if __name__ == "__main__":
    unittest.main()
