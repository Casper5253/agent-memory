"""Read-only, project-scoped context from authoritative supported claims."""
import json
import math
import re
import sqlite3
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

from . import policy as p
from .models import KnowledgeError


@dataclass(frozen=True)
class ContextRequest:
    query: str
    project: str | None = None
    cwd: str | None = None
    mode: str = "current"
    as_of: str | None = None
    budget: int = 2400
    semantic: bool = False
    tokenizer_id: str | None = None


@dataclass
class ContextResponse:
    schema_version: str = "1.0"
    status: str = "ok"
    project_id: str | None = None
    project_revision: int = 0
    mode: str = "current"
    as_of: str | None = None
    degraded: bool = False
    partial: bool = False
    budget: int = 2400
    budget_unit: str = "tokens_est_3b"
    used: int = 0
    items: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    omitted: dict = field(default_factory=lambda: dict(budget=0, scope=0, invalid_evidence=0))
    errors: list = field(default_factory=list)

    def to_dict(self):
        data = asdict(self)
        if self.errors:
            return {key: data[key] for key in ("schema_version", "status", "errors")}
        return data


def error_response(code, status="invalid_request"):
    return ContextResponse(status=status, errors=[dict(code=code, message="Context request could not be served.",
        retryable=status in ("busy", "unavailable"), reference_id=None)])


def tokens(text):
    return set(re.findall(r"[^\W_]+", text.casefold(), re.UNICODE))


def count_payload(payload, tokenizer=None):
    text = p.canonical(payload)
    return tokenizer.count(text) if tokenizer else math.ceil(len(text.encode("utf-8")) / 3)


def measure(response, tokenizer=None):
    for _ in range(20):
        used = count_payload(response.to_dict(), tokenizer)
        if response.used == used:
            return used
        response.used = used
    raise KnowledgeError("token_count_unstable")


def resolve_scope(store, request):
    projects = [dict(row) for row in store.db.execute("SELECT * FROM projects ORDER BY id")]
    if request.project:
        matches = [row for row in projects if row["id"] == request.project
                   or row["name"].casefold() == request.project.casefold()]
    elif request.cwd:
        cwd = Path(request.cwd).resolve()
        roots = [(Path(root).resolve(), row) for row in projects for root in json.loads(row["roots_json"])]
        owners = {}
        for root, row in roots:
            owners.setdefault(root, set()).add(row["id"])
        if any(len(ids) > 1 for root, ids in owners.items() if cwd.is_relative_to(root)):
            raise KnowledgeError("ambiguous_project")
        contained = [(len(root.parts), row) for root, row in roots if cwd.is_relative_to(root)]
        depth = max((n for n, _ in contained), default=-1)
        matches = list({row["id"]: row for n, row in contained if n == depth}.values())
    else:
        matches = []
    if len(matches) != 1:
        raise KnowledgeError("project_not_found" if not matches else "ambiguous_project")
    project = matches[0]
    if project["privacy"] == "excluded":
        raise KnowledgeError("project_excluded")
    if project["archived"] and request.mode != "history":
        raise KnowledgeError("archived_requires_history")
    return project


def temporal_eligible(claim, request, warnings):
    if claim["status"] in ("retracted", "quarantined"):
        return False
    if request.as_of:
        if not claim["valid_from"] or claim["valid_end_state"] == "unknown":
            warnings.add("unknown_time_excluded")
            return False
        if claim["valid_precision"] == "day":
            warnings.add("date_precision_excluded")
            return False
        instant = datetime.fromisoformat(request.as_of.replace("Z", "+00:00"))
        start = datetime.fromisoformat(claim["valid_from"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(claim["valid_to"].replace("Z", "+00:00")) if claim["valid_to"] else None
        return start <= instant and (end is None or instant < end)
    return request.mode == "history" or claim["status"] in p.LIVE_STATUSES


def _evidence(store, claim):
    citations, user_stated, heads = [], False, set()
    for span in store.db.execute("SELECT e.*,s.project_id,s.excerpt,s.content_sha,s.note_path,s.kind,s.speaker,"
            "s.privacy,s.repo_head FROM evidence e JOIN sources s ON s.id=e.source_id "
            "WHERE e.claim_id=? AND e.role='supports' ORDER BY s.speaker!='user',s.id,e.source_start", (claim["id"],)):
        if span["project_id"] != claim["project_id"] or span["privacy"] == "excluded":
            continue
        excerpt = span["excerpt"]
        start, end = span["source_start"], span["source_end"]
        if not (0 <= start < end <= len(excerpt)) or p.sha_text(excerpt) != span["content_sha"]:
            continue
        if p.sha_text(excerpt[start:end]) != span["quote_sha"]:
            continue
        try:
            p.relative_path(span["note_path"], store.vault)
        except KnowledgeError:
            continue
        user_stated |= span["speaker"] == "user"
        if span["repo_head"]:
            heads.add(span["repo_head"])
        end = min(end, start + 240)
        quote = excerpt[start:end]
        citations.append(dict(source_id=span["source_id"], note_path=span["note_path"], start=start,
            end=end, quote_sha=p.sha_text(quote), quote=quote, source_kind=span["kind"]))
    return citations, user_stated, heads


def _snapshot(store, request):
    warnings, claims = set(), {}
    store.db.execute("BEGIN")
    try:
        project = resolve_scope(store, request)
        statuses = "'reported','accepted','disputed','superseded'" if request.as_of or request.mode == "history" else "'reported','accepted','disputed'"
        rows = store.db.execute("SELECT c.*,e.canonical_key,e.display_name,e.aliases_json FROM claims c "
            "JOIN entities e ON e.id=c.subject_id AND e.project_id=c.project_id "
            "WHERE c.project_id=? AND c.status IN (" + statuses + ") ORDER BY c.id LIMIT 5001", (project["id"],)).fetchall()
        invalid = 0
        for row in rows[:5000]:
            claim = dict(row)
            if not temporal_eligible(claim, request, warnings):
                continue
            if claim["evidence_level"] == "verified":
                warnings.add("verified_artifact_unavailable_wp06")
                invalid += 1
                continue
            citations, user, heads = _evidence(store, claim)
            if not citations:
                invalid += 1
                continue
            claim.update(citations=citations[:1], user_stated=user, heads=heads)
            claims[claim["id"]] = claim
        relations = [dict(row) for row in store.db.execute(
            "SELECT * FROM relations WHERE project_id=? ORDER BY from_id,kind,to_id", (project["id"],))
            if row["from_id"] in claims and row["to_id"] in claims]
        projections = [dict(row) for row in store.db.execute(
            "SELECT * FROM projections WHERE project_id=? AND status='clean' AND rendered_revision=?",
            (project["id"], project["revision"]))]
        return project, claims, relations, projections, warnings, invalid, len(rows) > 5000
    finally:
        store.db.execute("ROLLBACK")


def lexical_ranks(query, claims):
    terms = tokens(query)
    ranked = []
    for cid, c in claims.items():
        subjects = [c["canonical_key"], c["display_name"], *json.loads(c["aliases_json"])]
        text = " ".join([*subjects, c["predicate"], c["statement"], c["rationale"]])
        matched = len(terms & tokens(text))
        if matched:
            exact = any(tokens(s) <= terms for s in subjects if tokens(s))
            ranked.append((-matched, -int(exact), cid))
    return [cid for _, _, cid in sorted(ranked)[:50]]


def fuse_ranks(*rankings):
    scores = {}
    for ranking in rankings:
        for rank, cid in enumerate(dict.fromkeys(ranking), 1):
            scores[cid] = scores.get(cid, 0) + 1 / (60 + rank)
    return sorted(scores, key=lambda cid: (-scores[cid], cid))[:50]


def qmd_ranks(store, project, claims, projections, hits):
    if not isinstance(hits, list):
        raise ValueError("malformed_qmd")
    pages = {row["path"]: row for row in projections}
    result = []
    for hit in hits[:50]:
        if not isinstance(hit, dict):
            continue
        raw = unquote(str(hit.get("file") or hit.get("path") or ""))
        if raw.startswith("qmd://vault-projects/"):
            raw = "1-Projects/" + raw[len("qmd://vault-projects/"):]
        elif "://" in raw:
            continue
        try:
            path = Path(raw)
            if path.is_absolute():
                raw = str(path.resolve().relative_to(store.vault)).replace("\\", "/")
            p.relative_path(raw, store.vault)
            page = pages.get(raw)
            if page is None or not raw.startswith("1-Projects/") or "/knowledge/" not in raw:
                continue
            disk = store.vault / raw
            if disk.stat().st_size > 128 * 1024:
                continue
            text = disk.read_text(encoding="utf-8")
            if p.sha_text(text) != page["rendered_sha"]:
                continue
            header = text.split("---", 2)
            if len(header) != 3 or header[0].strip():
                continue
            fields = dict(re.findall(r"(?m)^([a-z_]+):\s*([^\n]+)", header[1]))
            if fields.get("project_id", "").strip(' "') != project["id"]:
                continue
            if fields.get("project_revision", "").strip(' "') != str(project["revision"]):
                continue
            ids = re.findall(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", text)
            result.extend(cid for cid in ids if cid in claims and cid not in result)
        except (ValueError, OSError, KnowledgeError):
            continue
    return result[:50]


class QmdClient:
    def __init__(self, index=""):
        self.index = index

    def search(self, query, *, semantic=False, timeout):
        from ..search import _qmd
        completed = _qmd(["vsearch" if semantic else "search", query, "--format", "json", "-n", "50",
                          "-c", "vault-projects"], timeout=timeout, index=self.index)
        if completed.returncode:
            raise ValueError("qmd_failed")
        raw = completed.stdout.decode("utf-8")
        return json.loads(raw[raw.index("["):])


def repo_head(roots):
    if len(roots) != 1:
        return None
    try:
        result = subprocess.run(["git", "-C", roots[0], "rev-parse", "HEAD"], capture_output=True, timeout=.5)
        head = result.stdout.decode("ascii").strip()
        return head if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40,64}", head) else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _item(claim, relations, head):
    freshness = "unknown"
    if claim["predicate"] in ("repo_state", "head_commit", "release_tag", "branch", "test_count", "deployed_version"):
        if head and claim["heads"]:
            freshness = "repo_moved" if claim["heads"] != {head} else "current"
    item = {key: claim[key] for key in ("status", "evidence_level", "user_stated", "statement", "rationale",
                                     "valid_from", "valid_to", "recorded_at", "citations")}
    item.update(claim_ids=[claim["id"]], freshness=freshness,
        relations=[{k: r[k] for k in ("from_id", "kind", "to_id")} for r in relations
                   if claim["id"] in (r["from_id"], r["to_id"])][:10])
    if freshness == "repo_moved":
        item["freshness_warning"] = "repo_moved; live_verification_required; recorded " + ",".join(sorted(claim["heads"]))
    if claim["status"] == "superseded":
        item["section"] = "history"
    return item


def _bundles(ranked, claims, relations, head):
    selected = list(ranked)
    for cid in ranked[:10]:
        for r in relations:
            if r["kind"] not in ("supersedes", "contradicts", "depends_on"):
                continue
            neighbor = r["to_id"] if r["from_id"] == cid else r["from_id"] if r["to_id"] == cid else None
            if neighbor and neighbor not in selected and len(selected) < len(ranked) + 10:
                selected.append(neighbor)
    adjacency = {cid: set() for cid in claims}
    for r in relations:
        if r["kind"] == "contradicts" and all(claims[cid]["status"] in p.LIVE_STATUSES for cid in (r["from_id"], r["to_id"])):
            adjacency[r["from_id"]].add(r["to_id"])
            adjacency[r["to_id"]].add(r["from_id"])
    groups, seen = [], set()
    for cid in selected:
        if cid in seen:
            continue
        members, pending = set(), [cid]
        while pending:
            current = pending.pop()
            if current not in members:
                members.add(current)
                pending.extend(adjacency[current] - members)
        seen.update(members)
        items = [_item(claims[member], relations, head) for member in sorted(members)]
        conflict = len(members) > 1 or any(item["status"] == "disputed" for item in items)
        if conflict:
            bundle = dict(status="unresolved", claim_ids=sorted(members), alternatives=items, omitted_alternatives=0)
            priority = 0
        else:
            bundle = items[0]
            priority = (4 if bundle["freshness"] == "repo_moved" else 1 if bundle["status"] == "accepted"
                        else 2 if bundle["user_stated"] else 3)
        groups.append((priority, len(groups), bundle))
    return [bundle for _, _, bundle in sorted(groups)]


def build_context(store, request, qmd_client=None, tokenizer=None, clock=time.monotonic):
    if type(request.budget) is not int or not 1024 <= request.budget <= 8000:
        return error_response("invalid_budget")
    if not isinstance(request.query, str) or not tokens(request.query) or len(request.query) > 4096:
        return error_response("empty_or_oversized_query")
    if request.mode not in ("current", "history"):
        return error_response("invalid_mode")
    if request.tokenizer_id is not None and (tokenizer is None or request.tokenizer_id != tokenizer.id):
        return error_response("unsupported_tokenizer")
    if request.as_of:
        try:
            p.instant(request.as_of)
        except KnowledgeError:
            return error_response("invalid_as_of")
    try:
        for attempt in range(2):
            project, claims, relations, projections, warnings, invalid, partial = _snapshot(store, request)
            lexical = lexical_ranks(request.query, claims)
            lists = [lexical]
            degraded = False
            if qmd_client is not None:
                deadline = clock() + 1.5
                for semantic in ([False, True] if request.semantic else [False]):
                    remaining = deadline - clock()
                    if remaining <= 0:
                        degraded = True
                        warnings.add("qmd_timeout")
                        break
                    try:
                        hits = qmd_client.search(request.query, semantic=semantic, timeout=remaining)
                        if clock() > deadline:
                            raise TimeoutError
                        lists.append(qmd_ranks(store, project, claims, projections, hits))
                    except (OSError, ValueError, TypeError, TimeoutError, subprocess.TimeoutExpired):
                        degraded = True
                        warnings.add("qmd_unavailable")
                        break
            head = repo_head(json.loads(project["roots_json"])) if any(c["predicate"] in (
                "repo_state", "head_commit", "release_tag", "branch", "test_count", "deployed_version") for c in claims.values()) else None
            revision = store.db.execute("SELECT revision FROM projects WHERE id=?", (project["id"],)).fetchone()
            if revision is None or revision[0] != project["revision"]:
                if attempt == 0:
                    continue
                return error_response("revision_changed", "busy")
            response = ContextResponse(project_id=project["id"], project_revision=project["revision"],
                mode=request.mode, as_of=request.as_of, budget=request.budget, degraded=degraded, partial=partial,
                budget_unit="tokens:" + tokenizer.id + ":" + tokenizer.version if tokenizer else "tokens_est_3b")
            response.omitted["invalid_evidence"] = invalid
            response.warnings = sorted(warnings)
            for bundle in _bundles(fuse_ranks(*lists), claims, relations, head):
                response.items.append(bundle)
                if measure(response, tokenizer) > request.budget:
                    response.items.pop()
                    response.omitted["budget"] += len(bundle["claim_ids"])
                    if bundle["status"] == "unresolved":
                        response.warnings = sorted(set(response.warnings) | {"unresolved_alternatives_omitted"})
            if not response.items:
                response.warnings = sorted(set(response.warnings) | {"No eligible supported claim matched; missing_evidence."})
            while measure(response, tokenizer) > request.budget and response.items:
                response.omitted["budget"] += len(response.items.pop()["claim_ids"])
            if measure(response, tokenizer) > request.budget:
                return error_response("envelope_exceeds_budget")
            return response
    except sqlite3.Error:
        return error_response("store_unavailable", "unavailable")
    except KnowledgeError as exc:
        return error_response(exc.code, "privacy_violation" if exc.code == "project_excluded" else "invalid_request")
