"""Conservative synthetic retrieval grading from persisted, scoped evidence.

This grades literal supported retrieval, not general paraphrase entailment or
downstream answer quality. Missing structured fields fail visibly. Operational
fault cases remain unmeasured here; prose cannot prove a mutation was executed.
"""
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath


OPERATIONAL_CASES = frozenset({"T11", "T12", "T13", "T14", "T16", "T17", "T18", "T19", "T20", "T24"})
LABELS = {
    "reported": r"\b(?:reported|historical record|according to|source says)\b",
    "user_stated": r"\b(?:user stated|stated by the user|user explicitly chose|user directly stated)\b",
    "disputed": r"\b(?:disputed|conflicting reports|unresolved alternatives)\b",
    "verification_scope": r"\b(?:verification scope|scope is|scope:)\b",
    "unknown_time_excluded": r"\b(?:unknown time excluded|unknown time.*excluded|undated.*excluded)\b",
    "repo_moved": r"\b(?:repo moved|repository moved|repository head changed)\b",
    "live_verification_required": r"\b(?:live verification required|requires live verification|needs live verification)\b",
    "missing_evidence": r"\b(?:missing evidence|no evidence|not recorded|unrecorded|no .{0,60} recorded)\b",
    "command_attempt": r"\b(?:command attempt|command was attempted|command attempted)\b",
    "current_access_rejected": r"\b(?:current access rejected|current context rejected|current access denied)\b",
}
UNCERTAINTY = re.compile(r"\b(?:unresolved|uncertain|disputed|conflicting reports|disagree|no evidence|not recorded|unrecorded|missing evidence|no .{0,60} recorded|no success output (?:was )?(?:captured|recorded)|success output (?:was )?(?:not|never) captured)\b")
RESOLUTION = re.compile(r"\b(?:no unresolved|not uncertain|no uncertainty|no contradiction|no disagreement|only current truth|only correct choice|resolved|accepted current|sole current|the only alternative|with certainty|winner|all tests passed|tests? (?:passed|succeeded)|test run succeeded)\b")
CURRENT_ASSERTION = r"\b(?:accepted.{0,35}(?:current|use now)|current (?:decision|choice|truth)|use .{0,60} now|now use)\b"
QUERY_STOPWORDS = frozenset("a an and are as at be been by can could did do does for from had has have how i in is it its may me of on or our should that the their then these this those to was we were what when where which while who why will with would you your".split())
K0_UNREVIEWED_CASES = frozenset({"T02", "T03", "T04"})


def gate_query_applicable(arm, query):
    return not (arm == "K0" and query.get("case") in K0_UNREVIEWED_CASES
                and query.get("variant") != "unreviewed")


def applicable_gate_rows(rows, queries, arm):
    return {qid: row for qid, row in rows.items()
            if qid in queries and gate_query_applicable(arm, queries[qid]) and not row.get("unmeasured")}


def formulate_query(question):
    """Frozen v1 rule for every arm: lowercase, fixed stopwords, first four words."""
    words = re.findall(r"\b\w+(?:[-']\w+)*\b", question.lower())
    return " ".join([word for word in words if word not in QUERY_STOPWORDS][:4])


def _space(text):
    return " ".join(text.split())


def _has_explicit_label(text, field, value):
    return bool(re.search(r"(?:^|\n)\s*" + re.escape(field) + r"(?:\s+level)?\s*:\s*"
                          + re.escape(value) + r"\b", text, re.IGNORECASE))


def _plain(text):
    return re.sub(r"[^\w]+", " ", text.casefold().replace("_", " ")).strip()


def _positive(text, pattern):
    for line in re.split(r"[.!?\n]", text.casefold().replace("_", " ")):
        match = re.search(pattern, line)
        if match and not re.search(r"\b(?:not|never|no|without|none|neither)\s+(?:\w+\s+){0,3}$", line[:match.start()]):
            return True
    return False


def _supports(text, statement):
    needle = _plain(statement)
    if not needle:
        return False
    haystack = " " + _plain(text) + " "
    for match in re.finditer(re.escape(" " + needle + " "), haystack):
        prefix, suffix = haystack[:match.start()], haystack[match.end():]
        if re.search(r"\b(?:not|never|avoid|reject|no longer|don t|instead of)\s+(?:\w+\s+){0,2}$", prefix):
            continue
        if re.search(r"^\s*(?:is false|was rejected|did not happen)\b", suffix):
            continue
        return True
    return False


def _header(text):
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return {}
    chunks = re.split(r"\r?\n---\r?\n", text, maxsplit=1)
    if len(chunks) != 2:
        return {}
    fields = {}
    for line in chunks[0].splitlines()[1:]:
        match = re.fullmatch(r"([a-z_]+):\s*(.*?)\s*", line)
        if not match:
            continue
        key, value = match.groups()
        try:
            fields[key] = json.loads(value)
        except (ValueError, TypeError):
            fields[key] = value
    return fields


def _contained(vault, value):
    if not isinstance(value, str) or not value or ":" in value:
        raise ValueError("invalid_note_path")
    relative = PurePosixPath(value.replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("escaped_note_path")
    resolved = (vault / Path(*relative.parts)).resolve()
    if not resolved.is_relative_to(vault) or resolved.suffix != ".md":
        raise ValueError("escaped_note_path")
    return resolved


def _rows(vault):
    database = vault / "_meta" / "manifest.sqlite"
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute("SELECT * FROM captures")]
    finally:
        connection.close()


def _identity(row):
    return row.get("agent"), row.get("session_id"), row.get("part", 1)


def _note_identity_matches(fields, row):
    if fields.get("type") != "session":
        return False
    if fields.get("session_id") == "[REDACTED]":
        return fields.get("agent") == row.get("agent") and fields.get("part") == row.get("part")
    return _identity(fields) == _identity(row)


@dataclass(frozen=True)
class Citation:
    path: str
    text: str
    source_ids: frozenset
    status: str = ""
    evidence_level: str = ""


class EvidenceIndex:
    def __init__(self, corpus, vault):
        self.vault = Path(vault).resolve()
        self.projects = {p["id"]: p for p in corpus.get("projects", [])}
        self.claims = {c["id"]: c for c in corpus.get("claims", [])}
        self.sources = {s["id"]: s for s in corpus.get("sources", [])}
        self.paths = {}
        for row in _rows(self.vault):
            path = _contained(self.vault, row.get("note_path"))
            self.paths.setdefault(path, []).append(row)

    def _session_sources(self, path, project):
        rows = self.paths.get(path, [])
        if len(rows) != 1:
            raise ValueError("missing_or_colliding_capture")
        row = rows[0]
        note = path.read_text(encoding="utf-8")
        fields = _header(note)
        if not _note_identity_matches(fields, row):
            raise ValueError("capture_note_identity_mismatch")
        if fields.get("project") != project["name"] or row.get("project") != project["name"]:
            raise ValueError("foreign_project_note")
        return {source_id for source_id, source in self.sources.items()
                if _identity(source) == _identity(row) and source.get("project_id") == project["id"]}

    def cite(self, snippet, project_id):
        project = self.projects.get(project_id)
        if not project or project.get("policy") == "excluded":
            raise ValueError("unknown_or_excluded_project")
        path = _contained(self.vault, snippet.get("path"))
        visible = snippet.get("content")
        if not isinstance(visible, str) or not visible.strip():
            raise ValueError("empty_snippet")
        persisted = path.read_text(encoding="utf-8")
        if _space(visible) not in _space(persisted):
            raise ValueError("snippet_not_in_persisted_note")
        if path in self.paths:
            source_ids = self._session_sources(path, project)
        else:
            expected_path = self.vault / "1-Projects" / project["name"] / (project["name"] + ".md")
            if path != expected_path.resolve() or _header(persisted).get("type") != "project":
                raise ValueError("unmapped_or_foreign_note")
            source_ids = set()
            for target in re.findall(r"\[\[([^\]|#]+)(?:[^\]]*)\]\]", visible):
                linked = _contained(self.vault, target if target.endswith(".md") else target + ".md")
                source_ids.update(self._session_sources(linked, project))
        return Citation(str(path.relative_to(self.vault)), visible, frozenset(source_ids))

    def moved_heads(self, citation, claim, project_id):
        live = self.projects[project_id].get("repo", {}).get("head")
        return {self.sources[source].get("repo_head") for source in citation.source_ids.intersection(claim.get("source_ids", []))
                if live and self.sources[source].get("repo_head") and self.sources[source]["repo_head"] != live}


def _freshness_warning(citation, heads):
    return (bool(heads) and all(head in citation.text for head in heads)
            and _positive(citation.text, LABELS["repo_moved"])
            and _positive(citation.text, LABELS["live_verification_required"]))


def grade_query(query, gold, context, *, corpus, vault):
    if isinstance(context, dict) and "items" in context:
        result = _grade_structured(query, gold, context, corpus=corpus, vault=vault)
    else:
        result = _grade_query(query, gold, context, corpus=corpus, vault=vault)
    if query.get("case") in OPERATIONAL_CASES or (query.get("case") == "T21" and query.get("mode") == "current"):
        result["unmeasured"] = ["fault_execution:" + query["case"]]
        if "operational_evidence_not_measured" not in result["failures"]:
            result["failures"].append("operational_evidence_not_measured")
        result["pass"] = False
    return result


def _grade_query(query, gold, context, *, corpus, vault, proven_citations=None):
    """Score visible prose against authored facts and trusted note identities.

    A claim can be supported by any of its independently registered source spans.
    No source or claim identifier in the prose itself earns retrieval credit.
    """
    failures = []
    outcome = {"query_id": query["query_id"], "pass": False, "failures": failures,
               "snippet_count": 0, "packed_bytes": 0, "unmeasured": []}
    try:
        encoded = json.dumps(context, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
        outcome["packed_bytes"] = len(encoded)
        snippets = context.get("snippets", [])
        if not isinstance(snippets, list):
            raise ValueError("invalid_snippets")
        outcome["snippet_count"] = len(snippets)
        budget = query.get("budget", 2400)
        if isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0:
            raise ValueError("invalid_budget")
        unit = context.get("budget_unit", "tokens_est_3b")
        if unit not in ("bytes", "tokens_est_3b"):
            raise ValueError("unmeasured_tokenizer")
        used = len(encoded) if unit == "bytes" else math.ceil(len(encoded) / 3)
        outcome.update(budget_unit=unit, used=used)
        if used > budget:
            failures.append("budget_exceeded")
        if not snippets:
            failures.append("empty_context")
        for forbidden in gold.get("forbidden_strings", []):
            if forbidden in encoded.decode("utf-8"):
                failures.append("forbidden_string_present:" + forbidden)
        if context.get("project_id", query["project"]) != query["project"]:
            failures.append("foreign_context_project")
        index = EvidenceIndex(corpus, vault)
        citations = list(proven_citations or [])
        for snippet in ([] if proven_citations is not None else snippets):
            try:
                if snippet.get("project_id", query["project"]) != query["project"]:
                    raise ValueError("foreign_snippet_project")
                citations.append(index.cite(snippet, query["project"]))
            except (ValueError, OSError) as error:
                failures.append("invalid_citation:" + str(error))
        visible = "\n".join(c.text for c in citations)
        supports = {}
        for claim_id in gold.get("required_claims", []):
            claim = index.claims.get(claim_id)
            if not claim or claim.get("project_id") != query["project"]:
                failures.append("unmapped_required_claim:" + claim_id)
                continue
            matches = [c for c in citations if c.source_ids.intersection(claim.get("source_ids", []))
                       and _supports(c.text, claim["statement"])]
            supports[claim_id] = matches
            if not matches:
                failures.append("missing_supported_claim:" + claim_id)
        retrieved_sources = {source for citation in citations for source in citation.source_ids}
        for source_id in gold.get("required_sources", []):
            alternatives = {source_id}
            for claim_id in gold.get("required_claims", []):
                source_ids = set(index.claims.get(claim_id, {}).get("source_ids", []))
                if source_id in source_ids:
                    alternatives.update(source_ids)
            if not alternatives.intersection(retrieved_sources):
                failures.append("missing_required_source:" + source_id)
        supporting_texts = {c.text for matches in supports.values() for c in matches}
        if gold.get("required_rationale") and not any(_supports(text, gold["required_rationale"]) for text in supporting_texts):
            failures.append("missing_supported_rationale")
        label_text = "\n".join(supporting_texts) if supports else visible
        for label in gold.get("expected_labels", []):
            pattern = LABELS.get(label, r"\b" + re.escape(label.replace("_", " ")) + r"\b")
            if not _positive(label_text, pattern):
                failures.append("missing_label:" + label)
        accepted = {a["claim_id"] for a in corpus.get("acceptances", [])}
        for citation in citations:
            matching_claims = [claim for claim in index.claims.values()
                               if citation.source_ids.intersection(claim.get("source_ids", []))
                               and _supports(citation.text, claim["statement"])]
            for claim in matching_claims:
                moved = index.moved_heads(citation, claim, query["project"])
                if moved and not _freshness_warning(citation, moved):
                    failures.append("unqualified_moved_repository_fact:" + claim["id"])
            acceptance_asserted = citation.status == "accepted" or _has_explicit_label(citation.text, "status", "accepted")
            verification_asserted = citation.evidence_level == "verified" or _has_explicit_label(citation.text, "evidence", "verified")
            if acceptance_asserted and not any(
                    claim["id"] in accepted or claim.get("status") in ("accepted", "superseded") for claim in matching_claims):
                failures.append("unsupported_acceptance")
            if verification_asserted and not any(
                    claim.get("evidence_level") == "verified" for claim in matching_claims):
                failures.append("unsupported_verification")
            if query.get("case") == "T15" and (not matching_claims or any(
                    claim.get("rationale") and not _supports(citation.text, claim["rationale"]) for claim in matching_claims)):
                failures.append("incomplete_budget_bundle")
        normalized = visible.casefold().replace("_", " ")
        if gold.get("expected_uncertainty"):
            if not _positive(normalized, UNCERTAINTY.pattern) or _positive(normalized, RESOLUTION.pattern):
                failures.append("uncertainty_missing_or_denied")
        for claim_id in gold.get("forbidden_current_claims", []):
            claim = index.claims.get(claim_id)
            if not claim:
                failures.append("unmapped_forbidden_claim:" + claim_id)
                continue
            for citation in citations:
                if not _supports(citation.text, claim["statement"]):
                    continue
                historical = _positive(citation.text, r"\b(?:superseded|no longer current)\b")
                historical = historical or _freshness_warning(citation, index.moved_heads(citation, claim, query["project"]))
                disputed = gold.get("expected_uncertainty") and _positive(citation.text, LABELS["disputed"])
                asserts_current = _positive(citation.text, CURRENT_ASSERTION)
                if asserts_current or (not historical and not (disputed and not _positive(normalized, RESOLUTION.pattern))):
                    failures.append("forbidden_current_claim:" + claim_id)
        if query.get("mode") == "as_of":
            instant = datetime.fromisoformat(query["as_of"].replace("Z", "+00:00"))
            for claim_id, matches in supports.items():
                if not matches:
                    continue
                claim = index.claims[claim_id]
                start, end = claim.get("valid_from"), claim.get("valid_to")
                if not start or datetime.fromisoformat(start.replace("Z", "+00:00")) > instant:
                    failures.append("ineligible_as_of_claim:" + claim_id)
                elif end and datetime.fromisoformat(end.replace("Z", "+00:00")) <= instant:
                    failures.append("expired_as_of_claim:" + claim_id)
        if query.get("case") in OPERATIONAL_CASES or (query.get("case") == "T21" and query.get("mode") == "current"):
            outcome["unmeasured"].append("fault_execution:" + query["case"])
            failures.append("operational_evidence_not_measured")
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as error:
        failures.append("invalid_grading_setup:" + str(error))
    outcome["pass"] = not failures
    return outcome


def _structured_citations(query, context, corpus, vault):
    from agentmem.knowledge import policy as policy
    from agentmem.knowledge.store import KnowledgeStore
    if context.get("status") != "ok" or context.get("project_id") != query["project"]:
        raise ValueError("invalid_context_scope_or_status")
    mode = "current" if query.get("mode") == "as_of" else query.get("mode", "current")
    if context.get("mode") != mode or context.get("as_of") != query.get("as_of"):
        raise ValueError("context_mode_mismatch")
    items = context["items"]
    if not isinstance(items, list) or not items:
        raise ValueError("empty_context")
    sources = corpus.get("sources", [])
    citations, cited_claims = [], set()
    with KnowledgeStore(Path(vault) / "_meta/knowledge.sqlite", vault=vault) as store:
        store.db.execute("BEGIN")
        try:
            project = store.db.execute("SELECT * FROM projects WHERE id=?", (query["project"],)).fetchone()
            if project is None or project["privacy"] == "excluded" or project["revision"] != context.get("project_revision"):
                raise ValueError("stale_or_foreign_project")
            if project["archived"] and mode != "history":
                raise ValueError("archived_current_access")
            for bundle in items:
                if not isinstance(bundle, dict):
                    raise ValueError("invalid_bundle")
                alternatives = bundle.get("alternatives", [bundle])
                if "alternatives" in bundle:
                    if set(bundle) != {"status", "claim_ids", "alternatives", "omitted_alternatives"}:
                        raise ValueError("invalid_conflict_bundle")
                    if (bundle["status"] != "unresolved" or type(bundle["omitted_alternatives"]) is not int
                            or bundle["omitted_alternatives"] < 0 or not alternatives):
                        raise ValueError("incomplete_conflict_bundle")
                    if sorted(bundle["claim_ids"]) != sorted(cid for item in alternatives for cid in item["claim_ids"]):
                        raise ValueError("forged_conflict_ids")
                    missing = set()
                    for member in bundle["claim_ids"]:
                        rows = store.db.execute("SELECT a.id,b.id FROM relations r JOIN claims a ON a.id=r.from_id "
                            "AND a.project_id=r.project_id JOIN claims b ON b.id=r.to_id AND b.project_id=r.project_id "
                            "WHERE r.project_id=? AND r.kind='contradicts' AND (a.id=? OR b.id=?) "
                            "AND a.status IN ('reported','accepted','disputed') AND b.status IN ('reported','accepted','disputed')",
                            (query["project"], member, member)).fetchall()
                        missing.update(cid for row in rows for cid in row if cid not in bundle["claim_ids"])
                    if bundle["omitted_alternatives"] != len(missing):
                        raise ValueError("forged_omitted_alternatives")
                for item in alternatives:
                    required = {"claim_ids", "status", "evidence_level", "user_stated", "statement", "rationale", "freshness",
                                "valid_from", "valid_to", "recorded_at", "citations", "relations"}
                    if not isinstance(item, dict) or set(item) - required - {"freshness_warning", "section"} or required - set(item):
                        raise ValueError("invalid_claim_bundle")
                    if len(item["claim_ids"]) != 1:
                        raise ValueError("missing_or_forged_claim_id")
                    cid = item["claim_ids"][0]
                    row = store.db.execute("SELECT * FROM claims WHERE id=? AND project_id=?", (cid, query["project"])).fetchone()
                    if row is None or cid in cited_claims:
                        raise ValueError("missing_foreign_or_duplicate_claim")
                    cited_claims.add(cid)
                    for key in ("statement", "rationale", "status", "evidence_level", "valid_from", "valid_to", "recorded_at"):
                        if item[key] != row[key]:
                            raise ValueError("forged_claim_field:" + key)
                    if row["status"] in ("retracted", "quarantined"):
                        raise ValueError("ineligible_claim")
                    if row["status"] == "disputed" and bundle.get("status") != "unresolved":
                        raise ValueError("hidden_dispute")
                    related = store.db.execute("SELECT c.* FROM relations r JOIN claims c ON "
                        "c.id=CASE WHEN r.from_id=? THEN r.to_id ELSE r.from_id END "
                        "WHERE r.project_id=? AND r.kind='contradicts' AND (r.from_id=? OR r.to_id=?) "
                        "AND c.project_id=? AND c.status IN ('reported','accepted','disputed')",
                        (cid, query["project"], cid, cid, query["project"])).fetchall()
                    eligible_related = []
                    for other in related:
                        if query.get("as_of"):
                            instant = datetime.fromisoformat(query["as_of"].replace("Z", "+00:00"))
                            if (not other["valid_from"] or other["valid_end_state"] == "unknown" or other["valid_precision"] != "instant"
                                    or datetime.fromisoformat(other["valid_from"].replace("Z", "+00:00")) > instant
                                    or (other["valid_to"] and datetime.fromisoformat(other["valid_to"].replace("Z", "+00:00")) <= instant)):
                                continue
                        eligible_related.append(other["id"])
                    shown_as_history = row["status"] == "superseded" and item.get("section") == "history"
                    if eligible_related and not shown_as_history and (
                            bundle.get("status") != "unresolved" or (not set(eligible_related) <= set(bundle["claim_ids"])
                            and not bundle.get("omitted_alternatives"))):
                        raise ValueError("hidden_conflicting_alternative")
                    if bundle.get("status") == "unresolved" and row["status"] != "disputed" and not related:
                        raise ValueError("forged_dispute")
                    if query.get("as_of"):
                        instant = datetime.fromisoformat(query["as_of"].replace("Z", "+00:00"))
                        start, end = row["valid_from"], row["valid_to"]
                        if (not start or row["valid_end_state"] == "unknown" or row["valid_precision"] != "instant"
                                or datetime.fromisoformat(start.replace("Z", "+00:00")) > instant
                                or (end and datetime.fromisoformat(end.replace("Z", "+00:00")) <= instant)):
                            raise ValueError("ineligible_as_of_claim")
                    elif mode == "current" and row["status"] == "superseded":
                        raise ValueError("superseded_current_claim")
                    if row["status"] == "superseded" and item.get("section") != "history":
                        raise ValueError("unlabeled_history")
                    all_support = store.db.execute("SELECT s.*,e.source_start,e.source_end,e.quote_sha FROM evidence e "
                        "JOIN sources s ON s.id=e.source_id WHERE e.claim_id=? AND e.role='supports'", (cid,)).fetchall()
                    intact = [s for s in all_support if s["project_id"] == query["project"] and s["privacy"] != "excluded"
                        and policy.sha_text(s["excerpt"]) == s["content_sha"]
                        and 0 <= s["source_start"] < s["source_end"] <= len(s["excerpt"])
                        and policy.sha_text(s["excerpt"][s["source_start"]:s["source_end"]]) == s["quote_sha"]]
                    user = any(s["speaker"] == "user" for s in intact)
                    if type(item["user_stated"]) is not bool or item["user_stated"] != user:
                        raise ValueError("missing_or_forged_user_stated")
                    if not item["citations"]:
                        raise ValueError("missing_source_id")
                    fixture_ids, pasted = set(), False
                    for citation in item["citations"]:
                        if set(citation) != {"source_id", "note_path", "start", "end", "quote_sha", "quote", "source_kind", "speaker"}:
                            raise ValueError("invalid_citation_shape")
                        eligible = [s for s in intact if s["id"] == citation["source_id"]]
                        if not eligible:
                            raise ValueError("unproven_source_id")
                        source = eligible[0]
                        start, end = citation["start"], citation["end"]
                        if (type(start) is not int or type(end) is not int or not source["source_start"] <= start < end <= source["source_end"]
                                or end - start > 240 or source["excerpt"][start:end] != citation["quote"]
                                or policy.sha_text(citation["quote"]) != citation["quote_sha"]
                                or citation["note_path"] != source["note_path"] or citation["source_kind"] != source["kind"]
                                or citation["speaker"] != source["speaker"]):
                            raise ValueError("forged_quote")
                        pasted |= citation["speaker"] == "pasted"
                        policy.relative_path(citation["note_path"], Path(vault))
                        support_text = source["excerpt"][source["source_start"]:source["source_end"]]
                        if not _supports(support_text, item["statement"]) or (item["rationale"] and not _supports(support_text, item["rationale"])):
                            raise ValueError("unsupported_claim_content")
                        fixture_ids.update(s["id"] for s in sources if _identity(s) == _identity(dict(source))
                            and s["project_id"] == query["project"] and _supports(source["excerpt"], s.get("text", "")))
                    text = item["statement"] + "\nReason: " + item["rationale"]
                    text += "\nStatus: " + item["status"] + "; evidence: " + item["evidence_level"]
                    if user:
                        text += "; user_stated"
                    if pasted:
                        text += "; pasted"
                    if bundle.get("status") == "unresolved":
                        text += "; unresolved alternatives; disputed"
                    live = next((p.get("repo", {}).get("head") for p in corpus.get("projects", []) if p["id"] == query["project"]), None)
                    moved = {s["repo_head"] for s in intact if live and s["repo_head"] and s["repo_head"] != live}
                    if moved:
                        warning = item.get("freshness_warning", "")
                        if (item["freshness"] != "repo_moved" or not _positive(warning, LABELS["repo_moved"])
                                or not _positive(warning, LABELS["live_verification_required"]) or any(head not in warning for head in moved)):
                            raise ValueError("missing_repo_moved_label")
                        text += "\n" + warning
                    elif item["freshness"] not in ("unknown", "current"):
                        raise ValueError("forged_freshness")
                    elif item.get("freshness_warning"):
                        raise ValueError("forged_freshness_warning")
                    for relation in item["relations"]:
                        if set(relation) != {"from_id", "kind", "to_id"} or not store.db.execute(
                            "SELECT 1 FROM relations WHERE project_id=? AND from_id=? AND kind=? AND to_id=?",
                            (query["project"], relation["from_id"], relation["kind"], relation["to_id"])).fetchone():
                            raise ValueError("forged_relation")
                    citations.append(Citation(item["citations"][0]["note_path"], text, frozenset(fixture_ids),
                                              item["status"], item["evidence_level"]))
            proven = _proven_warnings(store, query) & set(context.get("warnings") or [])
            if proven:
                note = "\nWarnings: " + "; ".join(sorted(w.replace("_", " ") for w in proven))
                citations = [Citation(c.path, c.text + note, c.source_ids, c.status, c.evidence_level) for c in citations]
        finally:
            store.db.execute("ROLLBACK")
    return citations


def _proven_warnings(store, query):
    """Recompute temporal warnings from the query project's claims; a package cannot assert its own."""
    proven = set()
    if not query.get("as_of"):
        return proven
    rows = store.db.execute("SELECT valid_from, valid_end_state, valid_precision FROM claims WHERE project_id=? "
                            "AND status IN ('reported','accepted','disputed','superseded')", (query["project"],)).fetchall()
    for row in rows:
        if not row["valid_from"] or row["valid_end_state"] == "unknown":
            proven.add("unknown_time_excluded")
        elif row["valid_precision"] == "day":
            proven.add("date_precision_excluded")
    return proven


def _grade_structured(query, gold, context, *, corpus, vault):
    try:
        from agentmem.knowledge.policy import canonical
        from agentmem.knowledge.models import KnowledgeError
        encoded = canonical(context).encode("utf-8")
        if context.get("budget_unit") != "tokens_est_3b":
            raise ValueError("unmeasured_tokenizer")
        used = math.ceil(len(encoded) / 3)
        if context.get("used") != used or context.get("budget") != query.get("budget", 2400) or used > query.get("budget", 2400):
            raise ValueError("invalid_or_exceeded_budget")
        citations = _structured_citations(query, context, corpus, Path(vault).resolve())
        adapter = dict(snippets=[dict(path=c.path, content=c.text) for c in citations], project_id=context["project_id"],
                       budget_unit="tokens_est_3b")
        result = _grade_query(dict(query, budget=max(query.get("budget", 2400), 8000)), gold, adapter,
                              corpus=corpus, vault=vault, proven_citations=citations)
        for forbidden in gold.get("forbidden_strings", []):
            failure = "forbidden_string_present:" + forbidden
            if forbidden in encoded.decode("utf-8") and failure not in result["failures"]:
                result["failures"].append(failure)
        result.update(packed_bytes=len(encoded), used=used, budget_unit="tokens_est_3b")
        result["pass"] = not result["failures"]
        return result
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error, KnowledgeError) as error:
        return dict(query_id=query["query_id"], **{"pass": False}, failures=["invalid_structured_context:" + str(error)],
                    snippet_count=0, packed_bytes=0, unmeasured=[])


def lifecycle_check(vault, corpus):
    """Require exactly the eligible capture identities and their own valid notes."""
    vault = Path(vault).resolve()
    errors = []
    result = {"passed": False, "notes_written": 0, "captures_recorded": 0, "missing_note_files": 0,
              "colliding_captures": 0, "expected_sessions": 0, "redacted_session_ids": 0, "errors": errors}
    projects = {p["id"]: p for p in corpus.get("projects", [])}
    expected = {}
    for session in corpus.get("sessions", []):
        project = projects.get(session.get("project_id"), {})
        if project.get("policy") != "excluded":
            identity = _identity(session)
            if identity in expected:
                errors.append("duplicate_expected_identity")
            expected[identity] = session
    result["expected_sessions"] = len(expected)
    try:
        rows = _rows(vault)
        result["captures_recorded"] = len(rows)
        actual = [_identity(row) for row in rows]
        if len(actual) != len(set(actual)) or set(actual) != set(expected):
            errors.append("capture_identity_set_mismatch")
        paths = []
        path_counts = {}
        for row in rows:
            bound = _contained(vault, row.get("note_path"))
            path_counts[bound] = path_counts.get(bound, 0) + 1
        ranges = {}
        for row in rows:
            identity = _identity(row)
            path = _contained(vault, row.get("note_path"))
            paths.append(path)
            if not path.is_file():
                result["missing_note_files"] += 1
                errors.append("missing_note:" + str(row.get("note_path")))
                continue
            text = path.read_text(encoding="utf-8")
            fields = _header(text)
            if fields.get("session_id") == "[REDACTED]":
                result["redacted_session_ids"] += 1
            if not text.strip() or not _note_identity_matches(fields, row) or path_counts[path] != 1:
                errors.append("note_identity_or_content_mismatch")
                continue
            project = projects.get(expected.get(identity, {}).get("project_id"))
            if project and (fields.get("project") != project["name"] or row.get("project") != project["name"]):
                errors.append("note_project_mismatch")
            body = re.split(r"\r?\n---\r?\n", text, maxsplit=1)[-1]
            summary_response = expected.get(identity, {}).get("summary_response")
            if summary_response:
                summary = json.loads(summary_response).get("summary", "")
                if not summary or _space(summary) not in _space(body):
                    errors.append("note_summary_mismatch")
            elif not re.search(r"\w{3}", re.sub(r"(?m)^#+[^\n]*", "", body)):
                errors.append("empty_note_body")
            if fields.get("session_id") == "[REDACTED]" and not summary_response:
                supporting_sources = {s["id"] for s in corpus.get("sources", []) if _identity(s) == identity}
                supported = any(supporting_sources.intersection(claim.get("source_ids", [])) and _supports(body, claim["statement"])
                                for claim in corpus.get("claims", []))
                if not project or not supported:
                    errors.append("redacted_identity_without_supported_content")
            start, end = row.get("start_offset"), row.get("end_offset")
            if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end <= start:
                errors.append("invalid_capture_range")
            else:
                ranges.setdefault(identity[:2], []).append((identity[2], start, end))
            result["notes_written"] += 1
        result["colliding_captures"] = len(paths) - len(set(paths))
        if result["colliding_captures"]:
            errors.append("colliding_capture_paths")
        for parts in ranges.values():
            previous_end = None
            for part, start, end in sorted(parts):
                if previous_end is not None and start != previous_end:
                    errors.append("noncontiguous_capture_ranges")
                previous_end = end
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as error:
        errors.append("invalid_lifecycle_setup:" + str(error))
    result["passed"] = not errors and result["notes_written"] == len(expected)
    return result
