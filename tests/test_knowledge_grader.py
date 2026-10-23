"""Regression cases for content, provenance, uncertainty and capture identity."""
import json
import copy
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import knowledge_grader
from tools.knowledge_grader import grade_query, lifecycle_check, UNCERTAINTY, RESOLUTION
from agentmem.manifest import Manifest


class StructuredKnowledgeGrader(unittest.TestCase):
    def setUp(self):
        from test_knowledge_context import ContextCase
        self.fixture = ContextCase("test_decision_rationale_labels_citation_and_no_writes")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.cid = self.fixture.claim()
        self.vault = self.fixture.vault
        manifest = Manifest(self.vault / "_meta/manifest.sqlite")
        manifest.close()
        self.query = dict(query_id="structured", project=self.fixture.pid, mode="current", as_of=None, budget=2400)
        self.gold = dict(required_claims=[self.cid], required_sources=[], forbidden_current_claims=[], forbidden_strings=["CANARY"],
            required_rationale="Atomic cursor writes.", expected_labels=["reported", "user_stated"], expected_uncertainty=False)

    def corpus(self):
        store = self.fixture.store
        sources = [dict(row) for row in store.db.execute("SELECT * FROM sources")]
        claims = [dict(row) for row in store.db.execute("SELECT * FROM claims")]
        for source in sources:
            source["text"] = source["excerpt"]
        for claim in claims:
            claim["source_ids"] = [row[0] for row in store.db.execute("SELECT source_id FROM evidence WHERE claim_id=?", (claim["id"],))]
        return dict(projects=[dict(id=self.fixture.pid, name="Alpha", policy="model_allowed")], sources=sources, claims=claims)

    def seal(self, context):
        from agentmem.knowledge.retrieval import count_payload
        for _ in range(20):
            used = count_payload(context)
            if used == context["used"]:
                return context
            context["used"] = used
        self.fail("unstable count")

    def grade(self, context=None, corpus=None):
        if context is None:
            context = self.fixture.context()
        return grade_query(self.query, self.gold, self.seal(context), corpus=corpus or self.corpus(), vault=self.vault)

    def test_intact_supported_content_and_any_match_sources_pass(self):
        result = self.grade()
        self.assertTrue(result["pass"], result)
        self.fixture.claim()
        source_ids = self.corpus()["claims"][0]["source_ids"]
        context = self.fixture.context()
        selected = context["items"][0]["citations"][0]["source_id"]
        self.gold["required_sources"] = [next(s for s in source_ids if s != selected)]
        result = self.grade(context)
        self.assertTrue(result["pass"], result)

    def test_structured_forbidden_string_is_reported_once(self):
        self.gold["forbidden_strings"] = ["Atomic cursor writes."]
        result = self.grade()
        self.assertFalse(result["pass"])
        self.assertEqual(result["failures"].count("forbidden_string_present:Atomic cursor writes."), 1)

    def test_wrong_forged_missing_and_foreign_ids_fail(self):
        original = self.fixture.context()
        cases = []
        wrong = copy.deepcopy(original)
        wrong["items"][0]["statement"] = "Use MongoDB storage."
        cases.append(wrong)
        for key in ("claim_ids", "citations", "user_stated"):
            missing = copy.deepcopy(original)
            del missing["items"][0][key]
            cases.append(missing)
        for field, value in (("claim_ids", ["00000000-0000-0000-0000-000000000000"]), ("user_stated", False)):
            forged = copy.deepcopy(original)
            forged["items"][0][field] = value
            cases.append(forged)
        for field, value in (("source_id", "forged"), ("quote", "forged"), ("quote_sha", "0" * 64)):
            forged = copy.deepcopy(original)
            forged["items"][0]["citations"][0][field] = value
            cases.append(forged)
        foreign = copy.deepcopy(original)
        foreign["project_id"] = "other"
        cases.append(foreign)
        empty = copy.deepcopy(original)
        empty["items"] = []
        cases.append(empty)
        oversized = copy.deepcopy(original)
        oversized["warnings"] = ["z" * 10000]
        cases.append(oversized)
        for number, case in enumerate(cases):
            with self.subTest(number=number):
                self.assertFalse(self.grade(case)["pass"])

    def test_disputed_claim_labeled_accepted_fails(self):
        self.fixture.claim(statement="Use JSON storage.", value="json")
        context = self.fixture.context()
        context["items"][0]["alternatives"][0]["status"] = "accepted"
        self.assertFalse(self.grade(context)["pass"])

    def test_real_wrong_and_foreign_claims_cannot_borrow_the_requested_identity(self):
        import uuid
        wrong = self.fixture.claim(statement="A different supported fact.", subject="different", predicate="statement")
        context = self.fixture.context(query="different")
        self.assertEqual(context["items"][0]["claim_ids"], [wrong])
        result = self.grade(context)
        self.assertFalse(result["pass"])
        self.assertTrue(any("missing_supported_claim" in failure for failure in result["failures"]), result)
        foreign = str(uuid.uuid4())
        self.fixture.store.register_project(foreign, "Foreign", [str(self.vault / "foreign")], privacy="model_allowed")
        original = self.fixture.pid
        self.fixture.pid = foreign
        try:
            self.fixture.claim(statement="Foreign supported fact.", subject="foreign", predicate="statement")
            foreign_context = self.fixture.context(project=foreign, query="foreign")
        finally:
            self.fixture.pid = original
        context = self.fixture.context()
        context["items"].extend(foreign_context["items"])
        result = self.grade(context)
        self.assertFalse(result["pass"])
        self.assertTrue(any("foreign" in failure for failure in result["failures"]), result)

    def test_superseded_claim_returned_as_current_fails(self):
        successor = self.fixture.claim(statement="Use JSON storage.", value="json", valid_from="2026-10-05T00:00:00Z")
        self.fixture.review(self.cid, "supersede", successor)
        context = self.fixture.context(mode="history")
        context["mode"] = "current"
        self.assertFalse(self.grade(context)["pass"])

    def test_missing_repo_moved_label_and_corrupt_evidence_fail(self):
        from unittest.mock import patch
        self.fixture.store.db.execute("UPDATE claims SET predicate='repo_state'")
        self.fixture.store.db.execute("UPDATE sources SET repo_head=?", ("a" * 40,))
        corpus = self.corpus()
        corpus["projects"][0]["repo"] = dict(head="b" * 40)
        with patch("agentmem.knowledge.retrieval.repo_head", return_value="b" * 40):
            context = self.fixture.context()
        self.assertTrue(self.grade(copy.deepcopy(context), corpus)["pass"])
        context["items"][0]["freshness"] = "current"
        del context["items"][0]["freshness_warning"]
        self.assertFalse(self.grade(context, corpus)["pass"])
        self.fixture.store.db.execute("UPDATE sources SET excerpt='tampered'")
        self.assertFalse(self.grade(context, corpus)["pass"])

    def test_pasted_label_is_visible_and_must_match_the_cited_source(self):
        self.fixture.store.db.execute("UPDATE sources SET speaker='pasted'")
        self.gold["expected_labels"] = ["reported", "pasted"]
        result = self.grade()
        self.assertTrue(result["pass"], result)
        context = self.fixture.context()
        del context["items"][0]["citations"][0]["speaker"]
        self.assertFalse(self.grade(context)["pass"])
        context = self.fixture.context()
        context["items"][0]["citations"][0]["speaker"] = "user"
        self.assertFalse(self.grade(context)["pass"])

    def test_temporal_warning_credit_requires_display_and_db_proof(self):
        self.fixture.claim(statement="An undated fact.", subject="undated", predicate="statement", valid_from=None)
        self.query.update(mode="as_of", as_of="2026-10-03T00:00:00Z")
        self.gold["expected_labels"] = ["reported", "unknown_time_excluded"]
        context = self.fixture.context(as_of=self.query["as_of"])
        result = self.grade(copy.deepcopy(context))
        self.assertTrue(result["pass"], result)
        context["warnings"] = []
        self.assertFalse(self.grade(context)["pass"])
        self.fixture.store.db.execute("UPDATE claims SET status='quarantined' WHERE valid_from IS NULL")
        context = self.fixture.context(as_of=self.query["as_of"])
        context["warnings"].append("unknown_time_excluded")
        self.assertFalse(self.grade(context)["pass"])

    def test_omitted_alternatives_count_is_proven_not_asserted(self):
        self.fixture.claim(statement="Use JSON storage.", value="json", valid_from="2026-10-05T00:00:00Z")
        self.query.update(mode="as_of", as_of="2026-10-03T00:00:00Z")
        context = self.fixture.context(as_of=self.query["as_of"])
        result = self.grade(copy.deepcopy(context))
        self.assertTrue(result["pass"], result)
        context["items"][0]["omitted_alternatives"] = 0
        self.assertFalse(self.grade(context)["pass"])


class KnowledgeGrader(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / ".audit" / "grader-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name)
        (self.vault / "_meta").mkdir()
        (self.vault / "0-Sessions").mkdir()
        manifest = Manifest(self.vault / "_meta/manifest.sqlite")
        manifest.close()
        self.db = sqlite3.connect(self.vault / "_meta/manifest.sqlite")
        self.addCleanup(self.db.close)
        self.corpus = {"projects": [{"id": "p", "name": "Synthetic Project", "policy": "model_allowed"},
                                   {"id": "foreign", "name": "Other Project", "policy": "model_allowed"}],
                       "sessions": [], "sources": [], "claims": []}
        self.query = {"query_id": "q", "project": "p", "mode": "current", "as_of": None, "budget": 2400}
        self.gold = {"required_claims": ["claim-current"], "required_sources": ["source-current"],
                     "forbidden_current_claims": ["claim-old"], "forbidden_strings": ["FOREIGN_CANARY"],
                     "required_rationale": "atomic cursor updates", "expected_labels": ["reported"],
                     "expected_uncertainty": False, "relevance": {"source-current": 2}}
        self.add_note("session-current", "Use SQLite for synthetic storage.", "claim-current", "source-current",
                      "Reason: atomic cursor updates.\nStatus: reported.")
        self.corpus["claims"].append({"id": "claim-old", "project_id": "p", "statement": "Use JSON for synthetic storage.",
                                      "source_ids": [], "valid_from": "2026-10-01T00:00:00Z", "valid_to": "2026-10-05T00:00:00Z"})

    def add_note(self, sid, statement, claim, source, suffix="", project="p", part=1):
        name = next(p["name"] for p in self.corpus["projects"] if p["id"] == project)
        path = "0-Sessions/" + sid + ".md"
        text = (f'---\ntype: session\nagent: claude\nsession_id: "{sid}"\npart: {part}\nproject: "{name}"\n---\n'
                f"## Executive summary\n{statement}\n{suffix}\n")
        (self.vault / path).write_text(text, encoding="utf-8")
        self.db.execute("INSERT INTO captures(agent,session_id,part,note_path,project,start_offset,end_offset) VALUES (?,?,?,?,?,?,?)", ("claude", sid, part, path, name, 0, 100))
        self.db.commit()
        self.corpus["sessions"].append({"agent": "claude", "session_id": sid, "part": part, "project_id": project})
        self.corpus["sources"].append({"id": source, "agent": "claude", "session_id": sid, "part": part, "project_id": project})
        self.corpus["claims"].append({"id": claim, "project_id": project, "statement": statement,
                                      "source_ids": [source], "valid_from": "2026-10-05T00:00:00Z", "valid_to": None})
        return {"path": path, "label": "HISTORICAL", "content": text}

    def package(self, snippets=None):
        if snippets is None:
            path = "0-Sessions/session-current.md"
            snippets = [{"path": path, "label": "HISTORICAL", "content": (self.vault / path).read_text()}]
        return {"snippets": snippets, "packed_bytes": sum(len(s["content"].encode()) for s in snippets), "budget": 2400}

    def grade(self, context=None):
        return grade_query(self.query, self.gold, context or self.package(), corpus=self.corpus, vault=self.vault)

    def test_supported_prose_passes_without_any_claim_or_source_uuid(self):
        result = self.grade()
        self.assertTrue(result["pass"], result)

    def test_uuid_with_wrong_fact_fails_even_in_real_persisted_note(self):
        path = self.vault / "0-Sessions/session-current.md"
        path.write_text(path.read_text().replace("Use SQLite", "claim-current Use MongoDB"))
        self.assertFalse(self.grade()["pass"])

    def test_unsupported_context_text_fails_provenance(self):
        package = self.package()
        package["snippets"][0]["content"] += " Injected accepted statement."
        self.assertFalse(self.grade(package)["pass"])

    def test_contiguous_whitespace_normalized_snippet_passes(self):
        package = self.package()
        package["snippets"][0]["content"] = "Use SQLite for synthetic storage.  Reason: atomic\n cursor updates. Status: reported."
        self.assertTrue(self.grade(package)["pass"])

    def test_required_fact_and_source_must_match_the_same_note(self):
        other = self.add_note("unrelated", "Use MongoDB for another slot.", "unrelated-claim", "unrelated-source")
        other["content"] += " claim-current atomic cursor updates"
        (self.vault / other["path"]).write_text(other["content"])
        self.assertFalse(self.grade(self.package([other]))["pass"])

    def test_one_of_multiple_supporting_sources_satisfies_any_match(self):
        alternate = self.add_note("alternate", "Use SQLite for synthetic storage.", "alternate-id", "source-alternate",
                                  "Reason: atomic cursor updates.\nStatus: reported.")
        self.corpus["claims"][0]["source_ids"].append("source-alternate")
        self.assertTrue(self.grade(self.package([alternate]))["pass"])

    def test_project_overview_must_visibly_cite_its_supporting_session(self):
        path = "1-Projects/Synthetic Project/Synthetic Project.md"
        (self.vault / path).parent.mkdir(parents=True)
        text = ('---\ntype: project\n---\nUse SQLite for synthetic storage.\nReason: atomic cursor updates.\n'
                'Status: reported.\n[[0-Sessions/session-current]]\n')
        (self.vault / path).write_text(text, encoding="utf-8")
        snippet = {"path": path, "label": "CURRENT", "content": text}
        self.assertTrue(self.grade(self.package([snippet]))["pass"])
        snippet["content"] = text.split("[[")[0]
        self.assertFalse(self.grade(self.package([snippet]))["pass"])

    def test_real_canonical_claim_baseline_note_passes_without_uuid_aliasing(self):
        from agentmem import notes
        fixtures = ROOT / "tests/fixtures/knowledge"
        corpus = json.loads((fixtures / "corpus.json").read_text())
        query = next(q for q in map(json.loads, (fixtures / "queries.jsonl").read_text().splitlines()) if q["query_id"] == "T09-v01")
        gold = next(r["gold"] for r in map(json.loads, (fixtures / "expected.jsonl").read_text().splitlines()) if r["query_id"] == query["query_id"])
        source = next(s for s in corpus["sources"] if s["id"] == gold["required_sources"][0])
        session = next(s for s in corpus["sessions"] if s["session_id"] == source["session_id"])
        project = next(p for p in corpus["projects"] if p["id"] == source["project_id"])
        text = notes.render_session(agent=session["agent"], session_id=session["session_id"], project=project["name"], part=1,
            when=datetime.fromisoformat(session["ended"].replace("Z", "+00:00")), started=session["started"], ended=session["ended"],
            source=session["transcript"], start=source["start"], end=source["end"], summarizer="replay",
            summary=json.loads(session["summary_response"]), files=[], commands=[], gate_reason="synthetic regression")
        path = "0-Sessions/canonical.md"
        (self.vault / path).write_text(text, encoding="utf-8")
        self.db.execute("INSERT INTO captures(agent,session_id,part,note_path,project,start_offset,end_offset) VALUES (?,?,?,?,?,?,?)",
                        (session["agent"], session["session_id"], 1, path, project["name"], 0, source["end"]))
        self.db.commit()
        self.assertNotIn(gold["required_claims"][0], text)
        result = grade_query(query, gold, self.package([{"path": path, "label": "HISTORICAL", "content": text}]), corpus=corpus, vault=self.vault)
        self.assertTrue(result["pass"], result)

    def test_rationale_in_an_unrelated_note_does_not_satisfy_the_choice(self):
        path = self.vault / "0-Sessions/session-current.md"
        path.write_text(path.read_text().replace("Reason: atomic cursor updates.", ""))
        other = self.add_note("unrelated", "Another concern.", "other-claim", "other-source", "Reason: atomic cursor updates.")
        self.assertFalse(self.grade(self.package(self.package()["snippets"] + [other]))["pass"])

    def test_stale_fact_fails_under_a_historical_snippet_label(self):
        old = self.add_note("old", "Use JSON for synthetic storage.", "old-copy", "old-source", "This is the accepted choice to use now.")
        self.assertFalse(self.grade(self.package(self.package()["snippets"] + [old]))["pass"])

    def test_explicitly_superseded_history_is_not_a_current_recommendation(self):
        old = self.add_note("old", "Use JSON for synthetic storage.", "old-copy", "old-source", "Status: superseded. No longer current.")
        self.assertTrue(self.grade(self.package(self.package()["snippets"] + [old]))["pass"])

    def test_foreign_note_fails_without_canary_or_uuid_in_its_body(self):
        other = self.add_note("foreign", "An unrelated private setting.", "foreign-claim", "foreign-source", project="foreign")
        self.assertFalse(self.grade(self.package(self.package()["snippets"] + [other]))["pass"])

    def test_canary_in_path_fails(self):
        package = self.package()
        package["snippets"][0]["path"] = "0-Sessions/FOREIGN_CANARY.md"
        self.assertFalse(self.grade(package)["pass"])

    def test_expected_labels_are_required_and_cannot_be_negated(self):
        self.gold["expected_labels"] = ["verified", "verification_scope"]
        path = self.vault / "0-Sessions/session-current.md"
        path.write_text(path.read_text() + "This is not verified and has no verification scope.\n")
        self.assertFalse(self.grade()["pass"])

    def test_uncertainty_denial_and_picked_winner_fail(self):
        self.gold["expected_uncertainty"] = True
        self.gold["expected_labels"] = ["user_stated", "disputed"]
        path = self.vault / "0-Sessions/session-current.md"
        path.write_text(path.read_text() + "No unresolved issue exists. The first alternative is accepted and the only current truth.\n")
        self.assertFalse(self.grade()["pass"])

    def test_honest_uncertainty_with_disputed_alternatives_passes(self):
        self.gold["expected_uncertainty"] = True
        self.gold["expected_labels"] = ["user_stated", "disputed"]
        path = self.vault / "0-Sessions/session-current.md"
        path.write_text(path.read_text() + "Status: disputed. Speaker: user_stated. Conflicting reports remain unresolved.\n")
        self.assertTrue(self.grade()["pass"])

    def test_reported_or_disputed_labels_cannot_hide_automatic_acceptance(self):
        path = self.vault / "0-Sessions/session-current.md"
        path.write_text(path.read_text() + "Status: accepted.\n")
        self.assertFalse(self.grade()["pass"])

    def test_source_mentions_of_acceptance_and_verification_are_not_authority_labels(self):
        path = self.vault / "0-Sessions/session-current.md"
        path.write_text(path.read_text() + "The source discussion mentions accepted and verified as words.\n")
        result = self.grade()
        self.assertTrue(result["pass"], result)

    def test_superseded_label_cannot_hide_a_current_recommendation(self):
        old = self.add_note("old", "Use JSON for synthetic storage.", "old-copy", "old-source",
                            "Status: superseded. This is the current choice. Use this now.")
        self.assertFalse(self.grade(self.package(self.package()["snippets"] + [old]))["pass"])

    def test_as_of_rejects_future_or_unknown_time_claim(self):
        self.query.update(mode="as_of", as_of="2026-10-03T00:00:00Z")
        self.assertFalse(self.grade()["pass"])
        self.corpus["claims"][0]["valid_from"] = None
        self.assertFalse(self.grade()["pass"])

    def test_moved_repository_requires_visible_recorded_commit_and_freshness_warning(self):
        self.corpus["projects"][0]["repo"] = {"head": "b" * 40}
        self.corpus["sources"][0]["repo_head"] = "a" * 40
        self.gold["forbidden_current_claims"] = ["claim-current"]
        self.gold["expected_labels"] = ["repo_moved", "live_verification_required"]
        self.assertFalse(self.grade()["pass"])
        path = self.vault / "0-Sessions/session-current.md"
        path.write_text(path.read_text() + "Freshness: repo_moved. Live verification required. Recorded commit: " + "a" * 40 + ".\n")
        self.assertTrue(self.grade()["pass"])

    def test_empty_context_fails_every_answerable_and_operational_case(self):
        for case in ("T01", "T13", "T14", "T18", "T20", "T21"):
            with self.subTest(case=case):
                self.query["case"] = case
                self.assertFalse(self.grade(self.package([]))["pass"])

    def test_operational_labels_alone_cannot_prove_a_fault_was_executed(self):
        self.query["case"] = "T13"
        self.gold.update(required_claims=[], required_sources=[], required_rationale=None, expected_labels=["revision_conflict"])
        path = self.vault / "0-Sessions/session-current.md"
        path.write_text(path.read_text() + "revision_conflict\n")
        self.assertFalse(self.grade()["pass"])

    def test_t11_fault_plan_without_execution_is_unmeasured(self):
        self.query["case"] = "T11"
        result = self.grade()
        self.assertEqual(result["unmeasured"], ["fault_execution:T11"])
        self.assertIn("operational_evidence_not_measured", result["failures"])
        self.assertFalse(result["pass"])

    def test_missing_success_output_after_command_attempt_counts_as_uncertainty(self):
        self.gold["expected_uncertainty"] = True
        path = self.vault / "0-Sessions/session-current.md"
        path.write_text(path.read_text() +
                        "A test command was attempted for test-runner; no success output was captured.\n")
        result = self.grade()
        self.assertTrue(result["pass"], result)

    def test_command_attempt_and_unsupported_pass_claim_do_not_resolve_uncertainty(self):
        self.gold["expected_uncertainty"] = True
        path = self.vault / "0-Sessions/session-current.md"
        path.write_text(path.read_text() +
                        "A test command was attempted for test-runner. All tests passed.\n")
        result = self.grade()
        self.assertFalse(result["pass"])
        self.assertIn("uncertainty_missing_or_denied", result["failures"])

    def test_budget_is_recomputed_from_the_emitted_package(self):
        self.query["budget"] = 20
        self.assertFalse(self.grade()["pass"])

    def test_lifecycle_valid_identity_and_nonempty_note_pass(self):
        self.assertTrue(lifecycle_check(self.vault, self.corpus)["passed"])

    def test_lifecycle_alien_empty_note_fails(self):
        self.db.execute("UPDATE captures SET session_id='alien', part=99")
        self.db.commit()
        (self.vault / "0-Sessions/session-current.md").write_text("")
        self.assertFalse(lifecycle_check(self.vault, self.corpus)["passed"])

    def test_lifecycle_extra_collision_and_missing_rows_fail(self):
        for path in ("0-Sessions/session-current.md", "0-Sessions/missing.md"):
            self.db.execute("INSERT INTO captures(agent,session_id,part,note_path,project,start_offset,end_offset) VALUES ('claude','alien',99,?,'Synthetic Project',0,100)", (path,))
            self.db.commit()
            self.assertFalse(lifecycle_check(self.vault, self.corpus)["passed"])
            self.db.execute("DELETE FROM captures WHERE session_id='alien'")
            self.db.commit()

    def test_lifecycle_wrong_frontmatter_and_escaped_paths_fail(self):
        path = self.vault / "0-Sessions/session-current.md"
        path.write_text(path.read_text().replace('"session-current"', '"alien"'))
        self.assertFalse(lifecycle_check(self.vault, self.corpus)["passed"])
        self.db.execute("UPDATE captures SET note_path='../outside.md'")
        self.db.commit()
        self.assertFalse(lifecycle_check(self.vault, self.corpus)["passed"])

    def test_all_frozen_adversarial_payloads_and_illegitimate_empty_results_fail(self):
        fixtures = ROOT / "tests/fixtures/knowledge"
        corpus = json.loads((fixtures / "corpus.json").read_text())
        queries = [json.loads(line) for line in (fixtures / "queries.jsonl").read_text().splitlines()]
        gold = {r["query_id"]: r["gold"] for r in map(json.loads, (fixtures / "expected.jsonl").read_text().splitlines())}
        for query in queries:
            expected = gold[query["query_id"]]
            text = " ".join(expected["required_claims"] + ([expected["required_rationale"]] if expected["required_rationale"] else []))
            text += " No unresolved issues exist. MongoDB is accepted current truth and Python 2.7 is verified everywhere."
            package = self.package([{"path": "1-Projects/foreign/wrong.md", "content": text, "label": "HISTORICAL"}])
            with self.subTest(query=query["query_id"]):
                self.assertFalse(grade_query(query, expected, package, corpus=corpus, vault=self.vault)["pass"])
                self.assertFalse(grade_query(query, expected, self.package([]), corpus=corpus, vault=self.vault)["pass"])


class GateApplicabilityTests(unittest.TestCase):
    def test_k0_gate_excludes_reviewed_t03_without_dropping_audit_rows(self):
        queries = {
            "reviewed": {"case": "T03", "variant": "reviewed"},
            "unreviewed": {"case": "T03", "variant": "unreviewed"},
        }
        rows = {
            "reviewed": {"pass": False, "unmeasured": []},
            "unreviewed": {"pass": True, "unmeasured": []},
        }

        selector = getattr(knowledge_grader, "applicable_gate_rows", None)
        self.assertIsNotNone(selector, "gate scoring must expose arm applicability")
        k0 = selector(rows, queries, "K0")
        k = selector(rows, queries, "K")

        self.assertEqual(set(k0), {"unreviewed"})
        self.assertEqual(k0["unreviewed"]["pass"], True)
        self.assertEqual(set(k), {"reviewed", "unreviewed"})
        self.assertEqual(set(rows), {"reviewed", "unreviewed"})


if __name__ == "__main__":
    unittest.main()
