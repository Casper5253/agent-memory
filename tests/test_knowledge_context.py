"""Synthetic context contract checks against the real store and CLI."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agentmem.knowledge import policy as p
from agentmem.knowledge.models import CaptureIdentity, ReviewRequest, SourceInput
from agentmem.knowledge.retrieval import (ContextRequest, QmdClient, build_context, count_payload,
                                        fuse_ranks, lexical_ranks)
from agentmem.knowledge.store import KnowledgeStore


class ScriptedExtraction(unittest.TestCase):
    def test_scripted_proposals_use_only_source_content_and_cannot_grant_authority(self):
        from tools.knowledge_scripted import ScriptedExtractor
        from agentmem.knowledge.proposals import serialize_model_input
        sid = "a" * 64
        text = "Use SQLite for synthetic-component-01 storage. Reason: atomic cursor updates."
        data = dict(sources=[dict(source_id=sid, excerpt=text, speaker="user")], existing_claims=[])
        extractor = ScriptedExtractor({sid: dict(occurred_at="2026-10-01T00:00:00Z")})
        proposal = json.loads(extractor(serialize_model_input(data)))
        validated = p.validate_proposal(proposal)
        self.assertEqual(validated.claims[0].statement, "Use SQLite for synthetic-component-01 storage.")
        self.assertEqual(validated.claims[0].rationale, "atomic cursor updates")
        self.assertEqual(validated.claims[0].evidence[0].source_id, sid)
        self.assertNotIn("status", proposal["claims"][0])
        self.assertNotIn("evidence_level", proposal["claims"][0])
        self.assertEqual(json.loads(extractor(serialize_model_input(data))), proposal)
        data["sources"][0]["excerpt"] = "synthetic-component-01 current release is tag v1 at revision " + "a" * 40 + "."
        repo_claim = json.loads(extractor(serialize_model_input(data)))["claims"][0]
        self.assertEqual(repo_claim["predicate"], "repo_state")


class ContextCase(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / ".audit" / "tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temp.cleanup)
        self.vault = Path(temp.name)
        self.path = self.vault / "_meta" / "knowledge.sqlite"
        self.store = KnowledgeStore.create(self.path, vault=self.vault)
        self.addCleanup(self.store.close)
        self.pid = str(uuid.uuid4())
        self.project_root = self.vault / "work" / "alpha"
        self.project_root.mkdir(parents=True)
        (self.project_root.parent / ".git").mkdir()
        self.store.register_project(self.pid, "Alpha", [str(self.project_root)], privacy="model_allowed")
        self.request = ContextRequest("storage", project=self.pid)

    def claim(self, statement="Use SQLite storage.", rationale="Atomic cursor writes.", subject="storage",
              predicate="storage_backend", value="sqlite", speaker="user", session=None,
              valid_from="2026-10-01T00:00:00Z", valid_to=None, repo_head=None):
        session = session or str(uuid.uuid4())
        excerpt = statement + " " + rationale
        source = SourceInput("codex", session, 1, "transcript_span", "bytes", 0, 100, 0,
            {"format_version": "1", "provider_record_type": "message", "event_ordinal": 0,
             "message_ids": [], "redactor_version": "v1", "truncated": False},
            p.sha_text(excerpt), "0-Sessions/synthetic.md", p.sha_text(excerpt),
            "2026-10-02T00:00:00Z", None, "unknown", excerpt, session, "model_allowed", speaker, repo_head)
        imported = self.store.import_capture(self.pid, CaptureIdentity("codex", session, 1), p.sha_text(session), [source])
        payload = dict(schema_version=1,
            entities=[dict(ref="e", kind="component", canonical_key=subject, display_name=subject, aliases=[])],
            claims=[dict(ref="c", subject_ref="e", kind="decision", predicate=predicate, value=value,
                statement=statement, rationale=rationale, valid_from=valid_from, valid_to=valid_to,
                valid_precision="instant" if valid_from else "unknown",
                valid_end_state="closed" if valid_to else "open" if valid_from else "unknown",
                evidence=[dict(source_id=imported.source_ids[0], start=0, end=len(excerpt), role="supports")])], relations=[])
        revision = self.store.get_project(self.pid)["revision"]
        proposal = self.store.save_proposal(self.pid, imported.source_ids[0], p.canonical(payload), revision)
        result = self.store.apply_proposal(self.pid, proposal, revision, proposal)
        cid = (result.created_claim_ids or result.changed_claim_ids)[0]
        return cid

    def context(self, **kwargs):
        return build_context(self.store, replace(self.request, **kwargs)).to_dict()

    def review(self, cid, action="accept", successor=None):
        claim = self.store.db.execute("SELECT revision FROM claims WHERE id=?", (cid,)).fetchone()
        successor_rev = self.store.db.execute("SELECT revision FROM claims WHERE id=?", (successor,)).fetchone() if successor else None
        request = ReviewRequest(str(uuid.uuid4()), self.pid, action, cid,
            self.store.get_project(self.pid)["revision"], claim[0], "operator", "synthetic", "test:operator",
            "Synthetic approval.", successor_claim_id=successor,
            expected_successor_revision=successor_rev[0] if successor else None,
            effective_boundary="2026-10-05T00:00:00Z" if successor else None)
        self.store.review_claim(request)

    def test_decision_rationale_labels_citation_and_no_writes(self):
        cid = self.claim()
        before = self.store.db.total_changes
        with patch("agentmem.llm.summarize", side_effect=AssertionError("model called")):
            result = self.context()
        item = result["items"][0]
        self.assertEqual(item["claim_ids"], [cid])
        self.assertEqual((item["statement"], item["rationale"], item["status"], item["user_stated"]),
                         ("Use SQLite storage.", "Atomic cursor writes.", "reported", True))
        citation = item["citations"][0]
        self.assertEqual(citation["quote_sha"], p.sha_text(citation["quote"]))
        self.assertLessEqual(len(citation["quote"]), 240)
        self.assertFalse(Path(citation["note_path"]).is_absolute())
        self.assertEqual(self.store.db.total_changes, before)
        self.assertFalse(self.store.db.in_transaction)

    def test_conflicts_never_pick_a_winner(self):
        old = self.claim(statement="Use JSON storage.", value="json")
        new = self.claim()
        result = self.context()
        bundle = result["items"][0]
        self.assertEqual(bundle["status"], "unresolved")
        self.assertEqual(set(bundle["claim_ids"]), {old, new})
        self.assertEqual([c["status"] for c in bundle["alternatives"]], ["disputed", "disputed"])

    def test_reviewed_succession_current_history_and_as_of(self):
        old = self.claim(statement="Use JSON storage.", value="json")
        new = self.claim(valid_from="2026-10-05T00:00:00Z")
        self.review(old, "supersede", new)
        self.assertEqual(self.context()["items"][0]["claim_ids"], [new])
        historical = self.context(mode="history")
        self.assertEqual({c["claim_ids"][0] for c in historical["items"]}, {old, new})
        predecessor = next(c for c in historical["items"] if c["claim_ids"] == [old])
        self.assertEqual(predecessor["section"], "history")
        self.assertEqual(self.context(as_of="2026-10-03T00:00:00Z")["items"][0]["claim_ids"], [old])
        self.assertEqual(self.context(as_of="2026-10-06T00:00:00Z")["items"][0]["claim_ids"], [new])

    def test_unreviewed_as_of_keeps_disputed_and_excludes_unknown(self):
        old = self.claim(statement="Use JSON storage.", value="json")
        self.claim(valid_from="2026-10-05T00:00:00Z")
        self.claim(statement="Unknown storage timing.", value="unknown", valid_from=None)
        result = self.context(as_of="2026-10-03T00:00:00Z")
        self.assertEqual(result["items"][0]["claim_ids"], [old])
        self.assertEqual(result["items"][0]["alternatives"][0]["status"], "disputed")
        self.assertIn("unknown_time_excluded", result["warnings"])

    def test_project_isolation_and_scope_resolution(self):
        self.claim()
        other = str(uuid.uuid4())
        self.store.register_project(other, "Foreign", [str(self.vault / "foreign")], privacy="model_allowed")
        self.assertEqual(self.context(project=other)["items"], [])
        self.assertEqual(self.context(project=None, cwd=str(self.project_root))["project_id"], self.pid)
        self.assertEqual(self.context(project=None, cwd=str(self.vault))["errors"][0]["code"], "project_not_found")
        self.store.db.execute("UPDATE projects SET roots_json=? WHERE id=?", (p.canonical([str(self.project_root)]), other))
        self.assertEqual(self.context(project=None, cwd=str(self.project_root))["errors"][0]["code"], "ambiguous_project")

    def test_excluded_archived_and_local_only(self):
        self.claim()
        self.store.db.execute("UPDATE projects SET privacy='local_only' WHERE id=?", (self.pid,))
        self.assertTrue(self.context()["items"])
        self.store.db.execute("UPDATE projects SET archived=1 WHERE id=?", (self.pid,))
        self.assertEqual(self.context()["errors"][0]["code"], "archived_requires_history")
        self.assertTrue(self.context(mode="history")["items"])
        self.store.db.execute("UPDATE projects SET privacy='excluded' WHERE id=?", (self.pid,))
        self.assertEqual(self.context(mode="history")["status"], "privacy_violation")

    def test_missing_and_corrupt_evidence(self):
        self.claim()
        self.store.db.execute("UPDATE sources SET excerpt='tampered'")
        result = self.context()
        self.assertEqual(result["items"], [])
        self.assertEqual(result["omitted"]["invalid_evidence"], 1)

    def test_unregistered_verified_artifact_is_ineligible_until_wp06(self):
        self.claim()
        self.store.db.execute("UPDATE claims SET evidence_level='verified'")
        result = self.context()
        self.assertEqual(result["items"], [])
        self.assertIn("verified_artifact_unavailable_wp06", result["warnings"])

    def test_partial_local_scan_reports_its_bound(self):
        cid = self.claim()
        columns = [row[1] for row in self.store.db.execute("PRAGMA table_info(claims)")]
        original = dict(self.store.db.execute("SELECT * FROM claims WHERE id=?", (cid,)).fetchone())
        sql = "INSERT INTO claims VALUES (" + ",".join("?" for _ in columns) + ")"
        self.store.db.execute("BEGIN")
        for n in range(5000):
            row = {**original, "id": str(uuid.uuid5(uuid.NAMESPACE_URL, "context-bound-" + str(n)))}
            self.store.db.execute(sql, [row[key] for key in columns])
        self.store.db.execute("COMMIT")
        result = self.context()
        self.assertTrue(result["partial"])
        self.assertLessEqual(result["omitted"]["invalid_evidence"], 5000)

    def test_supported_injected_tokenizer_counts_the_entire_envelope(self):
        self.claim()
        class Counter:
            id = "synthetic-pinned"
            version = "1"
            def count(self, text):
                return len(text.encode("utf-8"))
        request = replace(self.request, tokenizer_id=Counter.id, budget=2400)
        result = build_context(self.store, request, tokenizer=Counter()).to_dict()
        self.assertEqual(result["budget_unit"], "tokens:synthetic-pinned:1")
        self.assertEqual(result["used"], len(p.canonical(result).encode("utf-8")))
        self.assertLessEqual(result["used"], 2400)

    def test_expansion_is_one_hop_and_at_most_ten_neighbors(self):
        seed = self.claim(statement="Needle storage.", subject="needle", predicate="statement")
        neighbors = [self.claim(statement="Neighbor number " + str(n) + ".", subject="neighbor" + str(n),
                                predicate="statement", value=str(n)) for n in range(12)]
        second = self.claim(statement="Second hop.", subject="second", predicate="statement")
        for neighbor in neighbors:
            self.store.db.execute("INSERT INTO relations VALUES (?,?,?,?,?)", (self.pid, seed, "depends_on", neighbor, "reason"))
        self.store.db.execute("INSERT INTO relations VALUES (?,?,?,?,?)", (self.pid, neighbors[0], "depends_on", second, "reason"))
        result = self.context(query="needle", budget=8000)
        ids = {cid for item in result["items"] for cid in item["claim_ids"]}
        self.assertIn(seed, ids)
        self.assertNotIn(second, ids)
        self.assertEqual(len(ids), 11)

    def test_query_and_budget_errors_are_small(self):
        for kwargs, code in ((dict(query=""), "empty_or_oversized_query"), (dict(budget=1023), "invalid_budget"),
                             (dict(budget=8001), "invalid_budget"), (dict(tokenizer_id="unknown"), "unsupported_tokenizer"),
                             (dict(as_of="not-a-time"), "invalid_as_of")):
            with self.subTest(kwargs=kwargs):
                result = self.context(**kwargs)
                self.assertEqual(result["errors"][0]["code"], code)
                self.assertLess(len(p.canonical(result).encode()), 256)
        self.assertEqual(self.context(query="unmatched")["items"], [])

    def test_full_envelope_is_counted_and_bundles_are_atomic(self):
        for n in range(8):
            self.claim(statement="storage " + str(n) + " " + "x" * 450,
                       rationale="r" * 900, subject="storage" + str(n), value=str(n))
        result = self.context(budget=1024)
        self.assertEqual(result["used"], count_payload(result))
        self.assertLessEqual(result["used"], 1024)
        self.assertGreater(result["omitted"]["budget"], 0)
        for item in result["items"]:
            self.assertEqual(len(item["rationale"]), 900)
            self.assertTrue(item["citations"])

    def test_huge_conflict_omits_all_alternatives_honestly(self):
        for n in range(12):
            self.claim(statement="storage " + str(n) + " " + "x" * 450, rationale="r" * 900, value=str(n))
        result = self.context(budget=1024)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["omitted"]["budget"], 12)
        self.assertIn("unresolved_alternatives_omitted", result["warnings"])
        self.assertLessEqual(result["used"], 1024)

    def test_soft_allocation_keeps_room_for_reported_bundles(self):
        for n in range(6):
            cid = self.claim(statement="storage accepted " + str(n) + " " + "x" * 80,
                rationale="r" * 350, subject="accepted" + str(n), predicate="statement", value=str(n))
            self.review(cid)
        reported = self.claim(statement="storage user report " + "x" * 80,
            rationale="r" * 350, subject="reported", predicate="statement")
        result = self.context()
        self.assertIn(reported, {cid for item in result["items"] for cid in item["claim_ids"]})
        self.assertEqual(result["items"][-1]["status"], "reported")
        self.assertLessEqual(result["used"], 2400)

    def test_unicode_identifiers_and_fixed_fusion(self):
        cid = self.claim(subject="a_b-c/d.e", statement="λ storage.")
        self.assertEqual(self.context(query="a b c d e λ")["items"][0]["claim_ids"], [cid])
        self.assertEqual(fuse_ranks(["b", "a"], ["a", "b"]), ["a", "b"])

    def test_repo_moved_cannot_keep_current_freshness_even_when_accepted(self):
        cid = self.claim(predicate="repo_state", repo_head="a" * 40)
        self.review(cid)
        with patch("agentmem.knowledge.retrieval.repo_head", return_value="b" * 40):
            item = self.context()["items"][0]
        self.assertEqual((item["status"], item["freshness"]), ("accepted", "repo_moved"))
        self.assertIn("live_verification_required", item["freshness_warning"])
        self.assertIn("a" * 40, item["freshness_warning"])

    def test_qmd_failure_degrades_without_returning_snippets(self):
        self.claim()
        class Client:
            def search(self, *args, **kwargs):
                raise ValueError("PRIVATE_CANARY malformed JSON")
        result = build_context(self.store, self.request, Client()).to_dict()
        self.assertTrue(result["degraded"])
        self.assertTrue(result["items"])
        self.assertNotIn("PRIVATE_CANARY", p.canonical(result))

    def test_qmd_total_deadline_and_transaction_closed(self):
        self.claim()
        ticks = iter([0, 0, 1, 1.6])
        calls = []
        case = self
        class Client:
            def search(self, *args, **kwargs):
                case.assertFalse(case.store.db.in_transaction)
                calls.append(kwargs["timeout"])
                return []
        result = build_context(self.store, replace(self.request, semantic=True), Client(), clock=lambda: next(ticks)).to_dict()
        self.assertEqual(calls, [1.5])
        self.assertTrue(result["degraded"])

    def test_qmd_wrapper_uses_subprocess_timeout(self):
        with patch("agentmem.search._qmd", side_effect=subprocess.TimeoutExpired("qmd", .02)) as run:
            with self.assertRaises(subprocess.TimeoutExpired):
                QmdClient("isolated").search("storage", timeout=.02)
        self.assertEqual(run.call_args.kwargs, dict(timeout=.02, index="isolated"))

    def test_revision_changes_retry_once_then_busy(self):
        self.claim()
        calls = []
        case = self
        class Client:
            def search(self, *args, **kwargs):
                calls.append(1)
                case.store.db.execute("UPDATE projects SET revision=revision+1 WHERE id=?", (case.pid,))
                return []
        result = build_context(self.store, self.request, Client()).to_dict()
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["status"], "busy")

    def test_revision_retry_does_not_reset_total_backend_deadline(self):
        self.claim()
        calls = []
        ticks = iter([0, 0, 1.4, 1.6])
        case = self
        class Client:
            def search(self, *args, **kwargs):
                calls.append(kwargs["timeout"])
                case.store.db.execute("UPDATE projects SET revision=revision+1 WHERE id=?", (case.pid,))
                return []
        result = build_context(self.store, self.request, Client(), clock=lambda: next(ticks)).to_dict()
        self.assertEqual(calls, [1.5])
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["degraded"])

    def test_qmd_pages_require_project_revision_hash_and_safe_path(self):
        cid = self.claim(statement="Unrelated fact.", subject="unrelated")
        project = self.store.get_project(self.pid)
        path = "1-Projects/Alpha/knowledge/facts.md"
        disk = self.vault / path
        disk.parent.mkdir(parents=True)
        text = f"---\nproject_id: {self.pid}\nproject_revision: {project['revision']}\n---\n{cid}\n"
        disk.write_text(text, encoding="utf-8")
        self.store.db.execute("INSERT INTO projections VALUES (?,?,?,?,?,'clean')",
            (self.pid, path, project["revision"], project["revision"], p.sha_text(text)))
        class Client:
            def search(self, *args, **kwargs):
                return [{"file": "qmd://vault-projects/Alpha/knowledge/facts.md", "snippet": "CANARY"}]
        result = build_context(self.store, replace(self.request, query="different"), Client()).to_dict()
        self.assertEqual(result["items"][0]["claim_ids"], [cid])
        self.assertNotIn("CANARY", p.canonical(result))
        disk.write_text(text + "edited", encoding="utf-8")
        self.assertEqual(build_context(self.store, replace(self.request, query="different"), Client()).items, [])
        for raw in ("../escape.md", "//server/share.md", "qmd://foreign/Alpha/knowledge/facts.md", "1-Projects/Alpha/knowledge/../../escape.md"):
            class Unsafe:
                def search(self, *args, **kwargs):
                    return [{"file": raw, "snippet": "CANARY"}]
            self.assertEqual(build_context(self.store, replace(self.request, query="different"), Unsafe()).items, [])

    def test_fresh_cli_process_reads_supported_capture_without_mutation(self):
        cid = self.claim()
        runtime = self.vault / "runtime"
        runtime.mkdir()
        (runtime / "config.json").write_text(json.dumps(dict(knowledge_mode="shadow", qmd_index="wp04-test",
            project_privacy={"Alpha": "model_allowed"})), encoding="utf-8")
        env = dict(os.environ, AGENTMEM_HOME=str(runtime), XDG_CACHE_HOME=str(self.vault / "cache"),
            XDG_CONFIG_HOME=str(self.vault / "config"), INDEX_PATH=str(self.vault / "cache" / "qmd" / "index.sqlite"))
        executable = "import sys;sys.path.insert(0,sys.argv.pop(1));from agentmem.cli import main;sys.exit(main())"
        command = [sys.executable, "-B", "-I", "-c", executable, str(ROOT), "--vault", str(self.vault)]
        before = self.path.read_bytes()
        result = subprocess.run(command + ["context", "storage", "--project", self.pid, "--json"],
            capture_output=True, env=env, cwd=self.project_root, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["items"][0]["claim_ids"], [cid])
        self.assertEqual(self.path.read_bytes(), before)
        invalid = subprocess.run(command + ["context", "storage", "--budget", "12", "--project", self.pid, "--json"],
            capture_output=True, env=env, cwd=self.project_root, timeout=10)
        self.assertEqual(invalid.returncode, 2)
        self.assertEqual(json.loads(invalid.stdout)["errors"][0]["code"], "invalid_budget")
        self.store.close()
        missing = self.vault / "missing-vault"
        (missing / "_meta").mkdir(parents=True)
        unavailable = subprocess.run([sys.executable, "-B", "-I", "-c", executable, str(ROOT), "--vault", str(missing),
            "context", "storage", "--project", self.pid, "--json"], capture_output=True, env=env, timeout=10)
        self.assertEqual(unavailable.returncode, 4)
        self.assertEqual(json.loads(unavailable.stdout)["status"], "unavailable")
        self.assertFalse((missing / "_meta" / "knowledge.sqlite").exists())
