"""Synthetic tests for WP-03 proposal pipeline and fixture validator path."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentmem.knowledge import policy as p
from agentmem.knowledge import proposals as kprop
from agentmem.knowledge.models import CaptureIdentity, KnowledgeError, SourceInput, ValidationError
from agentmem.knowledge.policy import sha_text
from agentmem.knowledge.store import KnowledgeStore

SCRATCH = ROOT / ".audit" / "tmp"
SCRATCH.mkdir(parents=True, exist_ok=True)

FIXTURE_DIR = ROOT / "tests" / "fixtures" / "proposals"
SOURCE_ID_PLACEHOLDER = "12345678-1234-5678-1234-567812345678"
OTHER_SOURCE_ID_PLACEHOLDER = "87654321-4321-8765-4321-876543218765"


def _source_input(session="s1", excerpt="The synthetic project uses SQLite for durable storage.", ordinal=0, privacy="model_allowed", **changes):
    return SourceInput(
        "codex", session, 1, "transcript_span", "bytes", ordinal * 100, ordinal * 100 + 50, ordinal,
        {"format_version": "v1", "provider_record_type": "message", "event_ordinal": ordinal,
         "message_ids": [], "redactor_version": "agentmem-v1", "truncated": False},
        sha_text(excerpt), "0-Sessions/2026/10/synthetic.md", sha_text(excerpt),
        "2026-10-22T08:00:00Z", None, "unknown", excerpt, "origin-a",
        privacy=privacy, speaker="user", repo_head=None,
        **changes,
    )


class ProposalCase(unittest.TestCase):
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
            for sqlite in self.root.rglob("*.sqlite"):
                if not sqlite.is_file():
                    continue
                with closing(sqlite3.connect(sqlite)) as db:
                    self.assertEqual(db.execute("PRAGMA integrity_check").fetchall(), [("ok",)], str(sqlite))
                    self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [], str(sqlite))
        finally:
            self.temp.cleanup()

    def import_source(self, excerpt="The synthetic project uses SQLite for durable storage.", session="s1", project_id=None, **changes):
        project_id = project_id or self.pid
        source = _source_input(session=session, excerpt=excerpt, **changes)
        result = self.store.import_capture(project_id, CaptureIdentity("codex", session, 1), sha_text(session), [source])
        return result.source_ids[0]

    def bind_fixture(self, text: str, source_id: str, other_source_id: str | None = None) -> str:
        text = text.replace(SOURCE_ID_PLACEHOLDER, source_id)
        if other_source_id:
            text = text.replace(OTHER_SOURCE_ID_PLACEHOLDER, other_source_id)
        return text

    def assertCode(self, code, call):
        with self.assertRaises(KnowledgeError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)


class FixtureValidatorPath(ProposalCase):
    """Every proposal fixture bound to a real imported source and run through the store validator."""

    def _error_mapping(self, error_code: str) -> tuple:
        mapping = {
            "unknown_key": (ValidationError,),
            "source_not_in_input": (KnowledgeError, "evidence_scope"),
            "evidence_out_of_range": (ValidationError, "evidence_range"),
            "invalid_offsets": (ValidationError,),
            "missing_supports": (ValidationError, "missing_support"),
            "forbidden_relation": (ValidationError,),
            "invalid_ref": (ValidationError, "invalid_ref"),
            "unknown_subject": (ValidationError, "unknown_ref"),
            "invalid_temporal": (ValidationError, "invalid_interval"),
            "closed_interval_missing_end": (ValidationError, "invalid_interval"),
            "float_value": (ValidationError, "unsupported_value"),
            "payload_too_large": (ValidationError, "proposal_limit"),
            "invalid_json": (ValidationError, "invalid_json"),
        }
        return mapping.get(error_code, (Exception,))

    def test_all_proposal_fixtures(self):
        index = json.loads((FIXTURE_DIR / "cases.json").read_text(encoding="utf-8"))
        source_id = self.import_source()
        other_id = self.import_source(session="s2", excerpt="Other source excerpt for cross reference.", ordinal=1)
        failures = []
        for case in index:
            with self.subTest(file=case["file"], expect=case["expect"]):
                raw = (FIXTURE_DIR / case["file"]).read_text(encoding="utf-8")
                raw = self.bind_fixture(raw, source_id, other_id)
                rev = self.store.get_project(self.pid)["revision"]
                if case["expect"] == "valid":
                    try:
                        proposal_id = self.store.save_proposal(self.pid, source_id, raw, rev,
                                                                 extractor_version=case["file"])
                    except Exception as exc:
                        failures.append((case["file"], type(exc).__name__, str(exc)))
                        continue
                    row = self.store.db.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
                    self.assertIsNotNone(row, case["file"])
                else:
                    expected = self._error_mapping(case["error_code"])
                    try:
                        self.store.save_proposal(self.pid, source_id, raw, rev)
                        failures.append((case["file"], "no-error", None))
                    except expected[0] as exc:
                        if len(expected) > 1:
                            self.assertEqual(exc.code, expected[1], case["file"])
                    except Exception as exc:
                        failures.append((case["file"], type(exc).__name__, str(exc)))
        if failures:
            self.fail("fixture failures: " + json.dumps(failures, indent=2))


class ModelInputAndPrivacy(ProposalCase):
    def test_serialized_input_contains_prompt_sources_existing_claims_and_output_schema(self):
        source_id = self.import_source()
        with KnowledgeStore(self.path) as reader:
            inp = kprop.build_proposal_input(reader, source_id)
        serialized = kprop.serialize_model_input(inp)
        self.assertIn("You extract durable project knowledge", serialized)
        self.assertIn("The synthetic project uses SQLite for durable storage.", serialized)
        self.assertIn("OUTPUT SCHEMA", serialized)
        self.assertIn('"schema_version": 1', serialized)
        self.assertIn("entities", serialized)
        self.assertIn("claims", serialized)
        self.assertIn("relations", serialized)

    def test_excluded_and_local_only_text_never_reaches_model_callable(self):
        canary = "CANARY_EXCLUDED_LOCAL_ONLY_99"
        for privacy in ("excluded", "local_only"):
            with self.subTest(privacy=privacy):
                pid = str(uuid.uuid4())
                self.store.register_project(pid, privacy.capitalize(), [str(self.root / privacy)], privacy="model_allowed")
                source_id = self.import_source(session=privacy, project_id=pid,
                                               excerpt="Text with " + canary,
                                               privacy="model_allowed" if privacy == "excluded" else privacy)
                # Flip to excluded after import so the source exists but the policy blocks the model call.
                if privacy == "excluded":
                    self.store.db.execute("UPDATE projects SET privacy='excluded' WHERE id=?", (pid,))
                called = []
                def fake(prompt: str) -> str:
                    called.append(prompt)
                    return '{"schema_version":1,"entities":[],"claims":[],"relations":[]}'
                with self.assertRaises(KnowledgeError) as caught:
                    kprop.extract_and_apply(str(self.path), source_id, fake)
                self.assertIn(caught.exception.code, {"model_disallowed", "project_ineligible"})
                self.assertEqual(called, [])


class ExtractAndApply(ProposalCase):
    def _fake(self, payload: dict):
        def model(prompt: str) -> str:
            return json.dumps(payload)
        return model

    def test_model_claim_is_reported_not_accepted(self):
        source_id = self.import_source()
        payload = {
            "schema_version": 1,
            "entities": [{"ref": "e1", "kind": "component", "canonical_key": "storage", "display_name": "Storage", "aliases": []}],
            "claims": [{"ref": "c1", "subject_ref": "e1", "kind": "decision", "predicate": "storage_backend",
                        "value": "sqlite", "statement": "Use SQLite.", "rationale": "Atomic.",
                        "valid_from": None, "valid_to": None, "valid_precision": "unknown", "valid_end_state": "unknown",
                        "evidence": [{"source_id": source_id, "start": 0, "end": 3, "role": "supports"}]}],
            "relations": [],
        }
        result = kprop.extract_and_apply(str(self.path), source_id, self._fake(payload))
        self.assertEqual(len(result.created_claim_ids), 1)
        claim = self.store.get_claim(self.pid, result.created_claim_ids[0])
        self.assertEqual((claim["status"], claim["evidence_level"]), ("reported", "reported"))

    def test_oversized_model_output_is_rejected(self):
        source_id = self.import_source()
        def huge(prompt: str) -> str:
            return '{"schema_version":1,"entities":[],"claims":[{"ref":"c1","subject_ref":"e1","kind":"fact","predicate":"x","value":"' + "x" * 40000 + '","statement":"x","rationale":"x","valid_from":null,"valid_to":null,"valid_precision":"unknown","valid_end_state":"unknown","evidence":[{"source_id":"' + source_id + '","start":0,"end":1,"role":"supports"}]}],"relations":[]}'
        with self.assertRaises(ValidationError) as caught:
            kprop.extract_and_apply(str(self.path), source_id, huge, max_output_bytes=32768)
        self.assertEqual(caught.exception.code, "proposal_limit")

    def test_apply_is_idempotent(self):
        source_id = self.import_source()
        payload = {
            "schema_version": 1,
            "entities": [{"ref": "e1", "kind": "component", "canonical_key": "storage", "display_name": "Storage", "aliases": []}],
            "claims": [{"ref": "c1", "subject_ref": "e1", "kind": "decision", "predicate": "storage_backend",
                        "value": "sqlite", "statement": "Use SQLite.", "rationale": "Atomic.",
                        "valid_from": None, "valid_to": None, "valid_precision": "unknown", "valid_end_state": "unknown",
                        "evidence": [{"source_id": source_id, "start": 0, "end": 3, "role": "supports"}]}],
            "relations": [],
        }
        first = kprop.extract_and_apply(str(self.path), source_id, self._fake(payload))
        proposal = self.store.db.execute("SELECT base_revision FROM proposals WHERE id=?", (first.operation_id,)).fetchone()
        second = self.store.apply_proposal(self.pid, first.operation_id, proposal["base_revision"], first.operation_id)
        self.assertTrue(second.replayed)
        self.assertEqual(self.store.get_project(self.pid)["revision"], first.project_revision)

    def test_changed_source_hash_rejects_apply(self):
        source_id = self.import_source()
        payload = {
            "schema_version": 1,
            "entities": [{"ref": "e1", "kind": "component", "canonical_key": "storage", "display_name": "Storage", "aliases": []}],
            "claims": [{"ref": "c1", "subject_ref": "e1", "kind": "decision", "predicate": "storage_backend",
                        "value": "sqlite", "statement": "Use SQLite.", "rationale": "Atomic.",
                        "valid_from": None, "valid_to": None, "valid_precision": "unknown", "valid_end_state": "unknown",
                        "evidence": [{"source_id": source_id, "start": 0, "end": 3, "role": "supports"}]}],
            "relations": [],
        }
        proposal_id = self.store.save_proposal(self.pid, source_id, payload, self.store.get_project(self.pid)["revision"])
        self.store.db.execute("UPDATE sources SET excerpt='corrupted' WHERE id=?", (source_id,))
        self.assertCode("source_hash_mismatch", lambda: self.store.apply_proposal(
            self.pid, proposal_id, self.store.get_project(self.pid)["revision"], proposal_id))


class AuthorityInjection(ProposalCase):
    def test_injected_accepted_status_is_rejected(self):
        source_id = self.import_source()
        raw = (FIXTURE_DIR / "valid_authority_injection.json").read_text(encoding="utf-8")
        raw = self.bind_fixture(raw, source_id)
        # save_proposal strips authority keys during validation.
        rev = self.store.get_project(self.pid)["revision"]
        proposal_id = self.store.save_proposal(self.pid, source_id, raw, rev)
        result = self.store.apply_proposal(self.pid, proposal_id, rev, proposal_id)
        claim = self.store.get_claim(self.pid, result.created_claim_ids[0])
        self.assertEqual(claim["status"], "reported")


class LifecycleRegressions(ProposalCase):
    def output(self, sid):
        return self.bind_fixture((FIXTURE_DIR / "valid_minimal.json").read_text(encoding="utf-8"), sid)

    def test_impossible_input_budget_never_calls_model(self):
        sid = self.import_source()
        called = []
        with self.assertRaises(KnowledgeError) as caught:
            kprop.extract_and_apply(str(self.path), sid, lambda prompt: called.append(prompt) or self.output(sid), max_input_units=1)
        self.assertEqual(caught.exception.code, "input_limit")
        self.assertEqual(called, [])

    def test_exact_serialized_input_is_trimmed_and_evidence_is_bounded(self):
        sid = self.import_source(excerpt="x" * 6000)
        with KnowledgeStore(self.path) as store:
            inp = kprop.build_proposal_input(store, sid, max_input_units=1800)
        self.assertLessEqual(len(kprop.serialize_model_input(inp).encode()), 5400)
        self.assertTrue(inp["sources"][0]["truncated"])
        # The full persisted source must not make evidence outside the supplied prefix eligible.
        body = json.loads(self.output(sid))
        body["claims"][0]["evidence"][0].update(start=5500, end=5503)
        self.assertCode("evidence_range", lambda: kprop.extract_and_apply(
            str(self.path), sid, lambda prompt: json.dumps(body), max_input_units=1800))

    def test_changed_job_state_and_expired_lease_cannot_commit(self):
        for field, value in (("state", "quarantined"), ("lease_until", "2000-01-01T00:00:00Z"), ("lease_token", "replacement")):
            with self.subTest(field=field):
                sid = self.import_source(session=field)
                def late(prompt):
                    with KnowledgeStore(self.path, writable=True) as other:
                        other.db.execute(f"UPDATE jobs SET {field}=? WHERE source_id=?", (value, sid))
                    return self.output(sid)
                result = kprop.process_pending_extract_jobs(str(self.path), late)
                self.assertFalse(any(r["status"] == "applied" for r in result))
                self.assertEqual(self.store.db.execute("SELECT count(*) FROM claims").fetchone()[0], 0)

    def test_timeout_retries_are_counted_backed_off_and_capped(self):
        sid = self.import_source()
        called = []
        def timeout(prompt):
            with KnowledgeStore(self.path) as reader:
                row = reader.db.execute("SELECT state,attempts,lease_token FROM jobs WHERE source_id=?", (sid,)).fetchone()
            self.assertEqual(row["state"], "running")
            self.assertTrue(row["lease_token"])
            called.append(row["attempts"])
            raise TimeoutError("offline")
        for attempt in range(3):
            result = kprop.process_pending_extract_jobs(str(self.path), timeout)
            self.assertEqual(result[0]["error_code"], "model_timeout")
            row = self.store.db.execute("SELECT * FROM jobs WHERE source_id=?", (sid,)).fetchone()
            if attempt < 2:
                self.assertEqual(row["state"], "pending")
                self.assertIsNotNone(row["next_try_at"])
                self.assertEqual(kprop.process_pending_extract_jobs(str(self.path), timeout), [])
                self.store.db.execute("UPDATE jobs SET next_try_at=NULL WHERE source_id=?", (sid,))
        self.assertEqual(kprop.process_pending_extract_jobs(str(self.path), timeout), [])
        self.assertEqual(called, [1, 2, 3])
        self.assertEqual(row["state"], "quarantined")


if __name__ == "__main__":
    unittest.main()
