"""SQLite authority for project-scoped knowledge, schema version 1."""
import hashlib
import json
import os
import sqlite3
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from . import policy as p
from .models import (ApplyResult, BackupResult, CaptureIdentity, ImportResult,
                     KnowledgeError, ReviewRequest, ValidationError)

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE projects (
 id TEXT PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE, roots_json TEXT NOT NULL,
 mode TEXT NOT NULL CHECK(mode IN ('off','shadow','active')),
 revision INTEGER NOT NULL CHECK(revision>=0),
 privacy TEXT NOT NULL CHECK(privacy IN ('excluded','local_only','model_allowed')),
 archived INTEGER NOT NULL CHECK(archived IN (0,1)));
CREATE TABLE sources (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 agent TEXT NOT NULL, session_id TEXT NOT NULL, part INTEGER NOT NULL CHECK(part>=1),
 kind TEXT NOT NULL CHECK(kind IN ('transcript_span','legacy_note','manual_statement','approved_document')),
 cursor_kind TEXT NOT NULL CHECK(cursor_kind IN ('bytes','messages','note_lines','manual')),
 start INTEGER NOT NULL CHECK(start>=0), end INTEGER NOT NULL CHECK(end>=start),
 span_ordinal INTEGER NOT NULL CHECK(span_ordinal>=0), locator_json TEXT NOT NULL,
 original_range_sha TEXT, note_path TEXT NOT NULL, content_sha TEXT NOT NULL,
 recorded_at TEXT NOT NULL, occurred_at TEXT,
 occurred_precision TEXT NOT NULL CHECK(occurred_precision IN ('instant','day','unknown')),
 excerpt TEXT NOT NULL, origin_group TEXT NOT NULL,
 privacy TEXT NOT NULL CHECK(privacy IN ('excluded','local_only','model_allowed')),
 speaker TEXT NOT NULL CHECK(speaker IN ('user','assistant','pasted','unknown')), repo_head TEXT,
 UNIQUE(project_id,agent,session_id,part,cursor_kind,start,end,span_ordinal,content_sha));
CREATE TABLE entities (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 kind TEXT NOT NULL CHECK(kind IN ('project','file','component','concept')),
 canonical_key TEXT NOT NULL, display_name TEXT NOT NULL, aliases_json TEXT NOT NULL,
 UNIQUE(project_id,kind,canonical_key));
CREATE TABLE claims (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 subject_id TEXT NOT NULL REFERENCES entities(id),
 kind TEXT NOT NULL CHECK(kind IN ('fact','decision','preference','question','task')),
 predicate TEXT NOT NULL, value_json TEXT NOT NULL, statement TEXT NOT NULL, rationale TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('reported','accepted','disputed','superseded','retracted','quarantined')),
 evidence_level TEXT NOT NULL CHECK(evidence_level IN ('reported','documented','verified')),
 valid_from TEXT, valid_to TEXT,
 valid_precision TEXT NOT NULL CHECK(valid_precision IN ('instant','day','unknown')),
 valid_end_state TEXT NOT NULL CHECK(valid_end_state IN ('unknown','open','closed')),
 recorded_at TEXT NOT NULL, reviewed_at TEXT, review_due_at TEXT,
 revision INTEGER NOT NULL CHECK(revision>=1), content_sha TEXT NOT NULL);
CREATE TABLE evidence (
 claim_id TEXT NOT NULL REFERENCES claims(id), source_id TEXT NOT NULL REFERENCES sources(id),
 source_start INTEGER NOT NULL CHECK(source_start>=0),
 source_end INTEGER NOT NULL CHECK(source_end>source_start), quote_sha TEXT NOT NULL,
 role TEXT NOT NULL CHECK(role IN ('supports','refutes','context')),
 PRIMARY KEY(claim_id,source_id,source_start,source_end,role));
CREATE TABLE relations (
 project_id TEXT NOT NULL REFERENCES projects(id), from_id TEXT NOT NULL REFERENCES claims(id),
 kind TEXT NOT NULL CHECK(kind IN ('supersedes','depends_on','contradicts','derived_from')),
 to_id TEXT NOT NULL REFERENCES claims(id), rationale TEXT NOT NULL,
 PRIMARY KEY(from_id,kind,to_id), CHECK(from_id!=to_id));
CREATE TABLE proposals (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), source_id TEXT NOT NULL REFERENCES sources(id),
 base_revision INTEGER NOT NULL CHECK(base_revision>=0), input_sha TEXT NOT NULL,
 extractor_version TEXT NOT NULL, prompt_version TEXT NOT NULL, payload_json TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('queued','ready','applied','rejected','review')),
 error_code TEXT, created_at TEXT NOT NULL, reported_cost_usd REAL,
 input_tokens INTEGER CHECK(input_tokens>=0), output_tokens INTEGER CHECK(output_tokens>=0));
CREATE TABLE operations (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), request_sha TEXT NOT NULL,
 expected_revision INTEGER NOT NULL CHECK(expected_revision>=0),
 result_revision INTEGER NOT NULL CHECK(result_revision>=0), result_json TEXT NOT NULL,
 actor_kind TEXT NOT NULL CHECK(actor_kind IN ('system','agent','operator')),
 actor_id TEXT NOT NULL, approval_ref TEXT, recorded_at TEXT NOT NULL);
CREATE TABLE revisions (
 project_id TEXT NOT NULL REFERENCES projects(id), project_revision INTEGER NOT NULL CHECK(project_revision>=1),
 claim_id TEXT NOT NULL REFERENCES claims(id), claim_revision INTEGER NOT NULL CHECK(claim_revision>=1),
 snapshot_json TEXT NOT NULL, operation_id TEXT NOT NULL REFERENCES operations(id) DEFERRABLE INITIALLY DEFERRED,
 recorded_at TEXT NOT NULL, PRIMARY KEY(claim_id,claim_revision));
CREATE TABLE jobs (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), source_id TEXT REFERENCES sources(id),
 kind TEXT NOT NULL CHECK(kind IN ('import','extract','project')), dedupe_key TEXT NOT NULL UNIQUE,
 proposal_id TEXT REFERENCES proposals(id),
 state TEXT NOT NULL CHECK(state IN ('pending','running','done','error','quarantined')),
 attempts INTEGER NOT NULL CHECK(attempts>=0), next_try_at TEXT, lease_token TEXT, lease_until TEXT, error_code TEXT);
CREATE TABLE outbox (
 project_id TEXT PRIMARY KEY REFERENCES projects(id), desired_revision INTEGER NOT NULL CHECK(desired_revision>=0),
 acknowledged_revision INTEGER NOT NULL DEFAULT 0 CHECK(acknowledged_revision>=0 AND acknowledged_revision<=desired_revision));
CREATE TABLE projections (
 project_id TEXT NOT NULL REFERENCES projects(id), path TEXT NOT NULL,
 desired_revision INTEGER NOT NULL CHECK(desired_revision>=0), rendered_revision INTEGER NOT NULL CHECK(rendered_revision>=0),
 rendered_sha TEXT, status TEXT NOT NULL CHECK(status IN ('pending','clean','external_edit','error')),
 PRIMARY KEY(project_id,path));
CREATE TABLE imports (
 id TEXT PRIMARY KEY, agent TEXT NOT NULL, session_id TEXT NOT NULL, part INTEGER NOT NULL CHECK(part>=1),
 capture_sha TEXT NOT NULL, project_id TEXT NOT NULL REFERENCES projects(id), imported_at TEXT NOT NULL,
 UNIQUE(agent,session_id,part,capture_sha), UNIQUE(agent,session_id,part));
CREATE TABLE import_sources (
 import_id TEXT NOT NULL REFERENCES imports(id), source_id TEXT NOT NULL REFERENCES sources(id),
 span_ordinal INTEGER NOT NULL CHECK(span_ordinal>=0), PRIMARY KEY(import_id,source_id), UNIQUE(import_id,span_ordinal));
CREATE INDEX claims_slot ON claims(project_id,status,subject_id,predicate);
CREATE INDEX claims_review ON claims(project_id,review_due_at);
CREATE INDEX evidence_source ON evidence(source_id);
CREATE INDEX relations_from ON relations(from_id);
CREATE INDEX relations_to ON relations(to_id);
CREATE INDEX jobs_ready ON jobs(state,next_try_at);
CREATE INDEX sources_recorded ON sources(project_id,recorded_at);
CREATE INDEX revisions_project ON revisions(project_id,project_revision);
"""


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _git_safe(path):
    parent = path.parent
    while not parent.exists():
        parent = parent.parent
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    command = ["git", "-c", "core.excludesFile=" + os.devnull, "-C", str(parent)]
    try:
        probe = subprocess.run(command + ["rev-parse", "--is-inside-work-tree"],
                               capture_output=True, timeout=2, env=env)
        if probe.returncode:
            # An existing .git marker makes an unreadable repository unsafe.
            if any((ancestor / ".git").exists() for ancestor in (parent, *parent.parents)):
                raise KnowledgeError("git_check_failed")
            return
        if probe.stdout.strip() != b"true":
            return
        ignored = subprocess.run(command + ["check-ignore", "--quiet", "--", str(path)],
                                 capture_output=True, timeout=2, env=env)
        if ignored.returncode != 0:
            raise KnowledgeError("unignored_database")
    except (OSError, subprocess.TimeoutExpired):
        raise KnowledgeError("git_check_failed") from None


class KnowledgeStore:
    def __init__(self, path, *, writable=False, timeout=1.0, clock=utc_now, fault=None, vault=None):
        self.path = Path(path).resolve()
        self.vault = Path(vault).resolve() if vault is not None else (
            self.path.parent.parent if self.path.parent.name == "_meta" else self.path.parent)
        self.writable = writable
        self.clock = clock
        self.fault = fault
        self.db = None
        try:
            self.db = sqlite3.connect(self.path.as_uri() + ("?mode=rw" if writable else "?mode=ro"),
                                      uri=True, isolation_level=None, timeout=timeout)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            if self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='meta'").fetchone() is None:
                raise KnowledgeError("unsupported_schema")
            version = self.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            db_uuid = self.db.execute("SELECT value FROM meta WHERE key='db_uuid'").fetchone()
            if version is None or version[0] != str(SCHEMA_VERSION) or db_uuid is None:
                raise KnowledgeError("unsupported_schema")
            p.uuid_id(db_uuid[0])
            if writable:
                self.db.execute("PRAGMA synchronous=FULL")
            else:
                self.db.execute("PRAGMA query_only=ON")
        except BaseException as exc:
            self.close()
            if isinstance(exc, sqlite3.Error):
                raise KnowledgeError("store_unavailable") from None
            raise

    @classmethod
    def create(cls, path, **kwargs):
        path = Path(path).resolve()
        _git_safe(path)
        if path.exists():
            return cls(path, writable=True, **kwargs)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb"):
                pass
        except FileExistsError:
            raise KnowledgeError("store_creation_race") from None
        db = sqlite3.connect(path, isolation_level=None)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)
            db.execute("INSERT INTO meta VALUES ('schema_version',?)", (str(SCHEMA_VERSION),))
            db.execute("INSERT INTO meta VALUES ('db_uuid',?)", (str(uuid.uuid4()),))
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()
        return cls(path, writable=True, **kwargs)

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @contextmanager
    def _tx(self):
        if not self.writable:
            raise KnowledgeError("read_only")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            yield
            self.db.execute("COMMIT")
        except BaseException as exc:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            if isinstance(exc, sqlite3.OperationalError):
                code = "store_busy" if "locked" in str(exc) or "busy" in str(exc) else "store_unavailable"
                raise KnowledgeError(code) from None
            if isinstance(exc, sqlite3.IntegrityError):
                raise KnowledgeError("constraint_violation") from None
            raise

    def _write(self, sql, args=()):
        result = self.db.execute(sql, args)
        if self.fault is not None:
            self.fault()
        return result

    def _project(self, project_id):
        p.uuid_id(project_id)
        row = self.db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if row is None:
            raise KnowledgeError("project_not_found")
        if row["archived"] or row["privacy"] == "excluded":
            raise KnowledgeError("project_ineligible")
        return row

    def register_project(self, project_id, name, roots, *, privacy, mode="off"):
        p.uuid_id(project_id)
        p.text(name, 120)
        p.enum(privacy, ("excluded", "local_only", "model_allowed"))
        p.enum(mode, ("off", "shadow", "active"))
        if not isinstance(roots, (tuple, list)) or any(not isinstance(root, str) or not root for root in roots):
            raise ValidationError("invalid_roots")
        for root in roots:
            p.text(root)
        normalized = sorted({os.path.normcase(str(Path(root).resolve())) for root in roots})
        encoded = p.canonical(normalized)
        with self._tx():
            rows = self.db.execute("SELECT * FROM projects").fetchall()
            for row in rows:
                if row["id"] == project_id:
                    if (row["name"], row["roots_json"], row["privacy"], row["mode"]) != (name, encoded, privacy, mode):
                        raise KnowledgeError("project_mapping_conflict")
                    return dict(row)
                if row["name"].casefold() == name.casefold() or set(json.loads(row["roots_json"])) & set(normalized):
                    raise KnowledgeError("project_mapping_conflict")
            self._write("INSERT INTO projects VALUES (?,?,?,?,0,?,0)", (project_id, name, encoded, mode, privacy))
        return dict(self.db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone())

    def get_project(self, project_id):
        return dict(self._project(project_id))

    def _source(self, project_id, source_id):
        row = self.db.execute("SELECT * FROM sources WHERE id=? AND project_id=?", (source_id, project_id)).fetchone()
        if row is None:
            raise KnowledgeError("source_scope")
        p.relative_path(row["note_path"], self.vault)
        if p.sha_text(row["excerpt"]) != row["content_sha"]:
            raise KnowledgeError("source_hash_mismatch")
        if row["privacy"] == "excluded":
            raise KnowledgeError("source_ineligible")
        return row

    def get_source(self, project_id, source_id):
        self._project(project_id)
        return dict(self._source(project_id, source_id))

    def _claim(self, project_id, claim_id, revision=None):
        row = self.db.execute("SELECT * FROM claims WHERE id=? AND project_id=?", (claim_id, project_id)).fetchone()
        if row is None:
            raise KnowledgeError("claim_scope")
        if revision is not None and row["revision"] != revision:
            raise KnowledgeError("revision_conflict")
        return row

    def get_claim(self, project_id, claim_id):
        self._project(project_id)
        row = dict(self._claim(project_id, claim_id))
        evidence = self.db.execute("SELECT * FROM evidence WHERE claim_id=?", (claim_id,)).fetchall()
        row["user_stated"] = False
        for span in evidence:
            source = self._source(project_id, span["source_id"])
            if p.sha_text(source["excerpt"][span["source_start"]:span["source_end"]]) != span["quote_sha"]:
                raise KnowledgeError("quote_hash_mismatch")
            if span["role"] == "supports" and source["speaker"] == "user":
                row["user_stated"] = True
        return row

    def integrity_check(self):
        result = [row[0] for row in self.db.execute("PRAGMA integrity_check")]
        if result != ["ok"] or self.db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise KnowledgeError("integrity_failed")
        return "ok"

    def _replay(self, operation_id, project_id, request_sha, result_type):
        row = self.db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
        if row is None:
            return None
        if row["project_id"] != project_id or row["request_sha"] != request_sha:
            raise KnowledgeError("idempotency_conflict")
        result = json.loads(row["result_json"])
        for name in ("source_ids", "created_claim_ids", "changed_claim_ids", "conflict_ids"):
            if name in result:
                result[name] = tuple(result[name])
        return result_type(**dict(result, replayed=True))

    def _finish(self, result, request_sha, expected, actor_kind, actor_id, approval_ref=None):
        updated = self._write("UPDATE projects SET revision=? WHERE id=? AND revision=?",
                              (result.project_revision, result.project_id, expected))
        if updated.rowcount != 1:
            raise KnowledgeError("revision_conflict")
        self._write("INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (getattr(result, "operation_id", "import:" + getattr(result, "import_id", "")),
                     result.project_id, request_sha, expected, result.project_revision, p.canonical(asdict(result)),
                     actor_kind, actor_id, approval_ref, self.clock()))
        self._write("INSERT INTO outbox(project_id,desired_revision) VALUES (?,?) "
                    "ON CONFLICT(project_id) DO UPDATE SET desired_revision=excluded.desired_revision",
                    (result.project_id, result.project_revision))

    def import_capture(self, project_id, capture_identity, capture_fingerprint, sources):
        if not isinstance(capture_identity, CaptureIdentity):
            raise ValidationError("invalid_capture_identity")
        p.enum(capture_identity.agent, ("claude", "codex", "opencode"))
        p.text(capture_identity.session_id)
        if capture_identity.session_id.casefold() in ("[redacted]", "<redacted>", "redacted"):
            raise ValidationError("invalid_capture_identity")
        p.integer(capture_identity.part, 1)
        p.digest(capture_fingerprint)
        if not isinstance(sources, (list, tuple)):
            raise ValidationError("invalid_sources")
        for source in sources:
            p.validate_source(source, capture_identity)
            p.relative_path(source.note_path, self.vault)
        sources = sorted(sources, key=lambda source: source.span_ordinal)
        if len({source.span_ordinal for source in sources}) != len(sources):
            raise ValidationError("duplicate_ordinal")
        if len(p.canonical([asdict(source) for source in sources]).encode("utf-8")) > 32 * 1024:
            raise ValidationError("packet_limit")
        import_id = p.uuid_identity([capture_identity.agent, capture_identity.session_id,
                                    capture_identity.part, capture_fingerprint])
        operation_id = "import:" + import_id
        request_sha = p.identity([project_id, asdict(capture_identity), capture_fingerprint,
                                 [asdict(source) for source in sources]])
        with self._tx():
            project = self._project(project_id)
            previous = self.db.execute("SELECT * FROM imports WHERE agent=? AND session_id=? AND part=?",
                                       (capture_identity.agent, capture_identity.session_id, capture_identity.part)).fetchone()
            if previous is not None and (previous["capture_sha"] != capture_fingerprint or previous["project_id"] != project_id):
                raise KnowledgeError("capture_identity_conflict")
            replay = self._replay(operation_id, project_id, request_sha, ImportResult)
            if replay is not None:
                return replay
            self._write("INSERT INTO imports VALUES (?,?,?,?,?,?,?)",
                        (import_id, capture_identity.agent, capture_identity.session_id, capture_identity.part,
                         capture_fingerprint, project_id, self.clock()))
            source_ids, eligible = [], []
            for source in sources:
                if source.privacy == "excluded":
                    raise KnowledgeError("source_ineligible")
                source_id = p.identity([project_id, source.agent, source.session_id, source.part, source.cursor_kind,
                                        source.start, source.end, source.span_ordinal, source.content_sha])
                values = asdict(source)
                values["locator_json"] = p.canonical(source.locator_json)
                self._write("INSERT INTO sources VALUES (" + ",".join("?" for _ in range(22)) + ")",
                            (source_id, project_id, *values.values()))
                self._write("INSERT INTO import_sources VALUES (?,?,?)", (import_id, source_id, source.span_ordinal))
                source_ids.append(source_id)
                if source.excerpt and source.privacy == "model_allowed" and project["privacy"] == "model_allowed":
                    eligible.append(source_id)
            job_id = None
            if eligible:
                dedupe = p.identity(["extract", source_ids[0], "v1", "v1", project["revision"] + 1])
                job_id = dedupe
                self._write("INSERT INTO jobs VALUES (?,?,?,'extract',?,NULL,'pending',0,NULL,NULL,NULL,NULL)",
                            (job_id, project_id, source_ids[0], dedupe))
            result = ImportResult(import_id, project_id, project["revision"] + 1, tuple(source_ids), job_id, not eligible)
            self._finish(result, request_sha, project["revision"], "system", "capture_import")
            return result

    def _input_sources(self, project_id, source_id):
        self._source(project_id, source_id)
        rows = self.db.execute("SELECT source_id FROM import_sources WHERE import_id IN "
                               "(SELECT import_id FROM import_sources WHERE source_id=?) ORDER BY span_ordinal",
                               (source_id,)).fetchall()
        if not rows:
            raise KnowledgeError("input_group_missing")
        return {row[0]: self._source(project_id, row[0]) for row in rows}

    def _validate_span(self, project_id, span, allowed=None):
        span = p.evidence_span(span)
        if allowed is not None and span.source_id not in allowed:
            raise KnowledgeError("evidence_scope")
        source = self._source(project_id, span.source_id)
        if span.end > len(source["excerpt"]):
            raise ValidationError("evidence_range")
        return span, p.sha_text(source["excerpt"][span.start:span.end])

    def save_proposal(self, project_id, source_id, payload, base_revision, *,
                      extractor_version="v1", prompt_version="v1", input_sha=None):
        validated = p.validate_proposal(payload)
        payload_json = p.canonical(p.proposal_payload(validated))
        p.integer(base_revision)
        p.text(extractor_version)
        p.text(prompt_version)
        with self._tx():
            project = self._project(project_id)
            if project["privacy"] != "model_allowed":
                raise KnowledgeError("model_disallowed")
            sources = self._input_sources(project_id, source_id)
            if any(source["privacy"] != "model_allowed" for source in sources.values()):
                raise KnowledgeError("model_disallowed")
            for claim in validated.claims:
                for span in claim.evidence:
                    self._validate_span(project_id, span, sources)
            input_sha = input_sha or p.identity([[key, row["content_sha"]] for key, row in sources.items()])
            p.digest(input_sha)
            proposal_id = p.identity([source_id, extractor_version, prompt_version, input_sha, base_revision])
            row = self.db.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
            if row is not None:
                if row["payload_json"] != payload_json or row["project_id"] != project_id:
                    raise KnowledgeError("idempotency_conflict")
                return proposal_id
            if project["revision"] != base_revision:
                raise KnowledgeError("revision_conflict")
            self._write("INSERT INTO proposals VALUES (?,?,?,?,?,?,?,?,'ready',NULL,?,NULL,NULL,NULL)",
                        (proposal_id, project_id, source_id, base_revision, input_sha, extractor_version,
                         prompt_version, payload_json, self.clock()))
            return proposal_id

    def _attach(self, project_id, claim_id, spans, allowed=None):
        changed = False
        for span in spans:
            span, quote_sha = self._validate_span(project_id, span, allowed)
            self._claim(project_id, claim_id)
            cursor = self._write("INSERT OR IGNORE INTO evidence VALUES (?,?,?,?,?,?)",
                                 (claim_id, span.source_id, span.start, span.end, quote_sha, span.role))
            changed = changed or cursor.rowcount > 0
        return changed

    def _supports(self, project_id, claim_id):
        spans = self.db.execute("SELECT * FROM evidence WHERE claim_id=?", (claim_id,)).fetchall()
        supports = False
        for span in spans:
            source = self._source(project_id, span["source_id"])
            if span["source_end"] > len(source["excerpt"]) or p.sha_text(source["excerpt"][span["source_start"]:span["source_end"]]) != span["quote_sha"]:
                raise KnowledgeError("quote_hash_mismatch")
            supports = supports or span["role"] == "supports"
        if not supports:
            raise KnowledgeError("missing_support")

    def _edge(self, project_id, from_id, kind, to_id, rationale):
        self._claim(project_id, from_id)
        self._claim(project_id, to_id)
        if from_id == to_id:
            raise KnowledgeError("relation_cycle")
        if kind != "contradicts":
            visited, pending = set(), [to_id]
            while pending:
                current = pending.pop()
                if current == from_id:
                    raise KnowledgeError("relation_cycle")
                if current in visited:
                    continue
                visited.add(current)
                if len(visited) > 10000:
                    raise KnowledgeError("graph_limit")
                targets = self.db.execute("SELECT to_id FROM relations WHERE project_id=? AND from_id=? "
                                          "AND kind!='contradicts' LIMIT 10001", (project_id, current)).fetchall()
                if len(targets) > 10000 or len(pending) + len(targets) > 20000:
                    raise KnowledgeError("graph_limit")
                pending.extend(row[0] for row in targets if row[0] not in visited)
        self._write("INSERT OR IGNORE INTO relations VALUES (?,?,?,?,?)", (project_id, from_id, kind, to_id, rationale))

    def _competitors(self, project_id, claim_id, excluded=()):
        claim = self._claim(project_id, claim_id)
        competitors = set()
        if claim["predicate"] in p.SINGLE_VALUED:
            rows = self.db.execute("SELECT * FROM claims WHERE project_id=? AND subject_id=? AND predicate=? "
                                   "AND status IN ('reported','accepted','disputed') AND id!=?",
                                   (project_id, claim["subject_id"], claim["predicate"], claim_id)).fetchall()
            for row in rows:
                if row["id"] not in excluded and row["value_json"] != claim["value_json"] and p.overlaps(claim, row):
                    competitors.add(row["id"])
        rows = self.db.execute("SELECT c.* FROM relations r JOIN claims c ON "
                               "c.id=CASE WHEN r.from_id=? THEN r.to_id ELSE r.from_id END "
                               "WHERE r.project_id=? AND r.kind='contradicts' AND (r.from_id=? OR r.to_id=?) "
                               "AND c.project_id=? AND c.status IN ('reported','accepted','disputed')",
                               (claim_id, project_id, claim_id, claim_id, project_id)).fetchall()
        competitors.update(row["id"] for row in rows if row["id"] not in excluded)
        return competitors

    def _dispute(self, project_id, claim_id, changed, excluded=()):
        competitors = self._competitors(project_id, claim_id, excluded)
        if competitors:
            members = {claim_id, *competitors}
            for member in members:
                row = self._claim(project_id, member)
                if row["status"] != "disputed":
                    self._write("UPDATE claims SET status='disputed' WHERE id=?", (member,))
                    changed.add(member)
            for other in competitors:
                self._edge(project_id, min(claim_id, other), "contradicts", max(claim_id, other), "Exact-slot or reviewed conflict.")
            return members
        return set()

    def _snapshots(self, project_id, changed, created, revision, operation_id, review=None):
        for claim_id in sorted(changed | created):
            if claim_id not in created:
                self._write("UPDATE claims SET revision=revision+1 WHERE id=? AND project_id=?", (claim_id, project_id))
            row = dict(self._claim(project_id, claim_id))
            snapshot = dict(row)
            if review is not None:
                snapshot["review"] = asdict(review)
            snapshot["evidence"] = [dict(span) for span in self.db.execute(
                "SELECT * FROM evidence WHERE claim_id=? ORDER BY source_id,source_start,source_end,role", (claim_id,))]
            self._write("INSERT INTO revisions VALUES (?,?,?,?,?,?,?)",
                        (project_id, revision, claim_id, row["revision"], p.canonical(snapshot), operation_id, self.clock()))

    def apply_proposal(self, project_id, proposal_id, expected_project_revision, operation_id):
        p.digest(proposal_id)
        p.integer(expected_project_revision)
        if operation_id != proposal_id:
            p.uuid_id(operation_id)
        with self._tx():
            project = self._project(project_id)
            proposal = self.db.execute("SELECT * FROM proposals WHERE id=? AND project_id=?", (proposal_id, project_id)).fetchone()
            if proposal is None:
                raise KnowledgeError("proposal_scope")
            request_sha = p.identity(["apply", project_id, proposal_id, expected_project_revision,
                                     proposal["payload_json"], proposal["input_sha"], proposal["base_revision"],
                                     proposal["source_id"], proposal["extractor_version"], proposal["prompt_version"]])
            replay = self._replay(operation_id, project_id, request_sha, ApplyResult)
            if replay is not None:
                return replay
            if project["revision"] != expected_project_revision or proposal["base_revision"] != expected_project_revision:
                raise KnowledgeError("revision_conflict")
            if proposal["state"] != "ready":
                raise KnowledgeError("proposal_not_ready")
            if project["privacy"] != "model_allowed":
                raise KnowledgeError("model_disallowed")
            validated = p.validate_proposal(proposal["payload_json"])
            sources = self._input_sources(project_id, proposal["source_id"])
            if any(row["privacy"] != "model_allowed" for row in sources.values()):
                raise KnowledgeError("model_disallowed")
            for item in validated.claims:
                for span in item.evidence:
                    self._validate_span(project_id, span, sources)
            for relation in validated.relations:
                for endpoint in (relation.from_claim, relation.to_claim):
                    if endpoint.claim_id is not None:
                        self._claim(project_id, endpoint.claim_id, endpoint.expected_revision)
            entities, claims, created, changed, conflicts = {}, {}, set(), set(), set()
            for entity in validated.entities:
                entity_id = p.identity([project_id, entity.kind, entity.canonical_key])
                existing = self.db.execute("SELECT project_id FROM entities WHERE id=?", (entity_id,)).fetchone()
                if existing is not None and existing[0] != project_id:
                    raise KnowledgeError("entity_scope")
                self._write("INSERT OR IGNORE INTO entities VALUES (?,?,?,?,?,?)",
                            (entity_id, project_id, entity.kind, entity.canonical_key, entity.display_name,
                             p.canonical(entity.aliases)))
                entities[entity.ref] = entity_id
            for item in validated.claims:
                row = dict(subject_id=entities[item.subject_ref], kind=item.kind, predicate=item.predicate,
                           value_json=p.value_json(item.value), statement=item.statement, rationale=item.rationale,
                           valid_from=item.valid_from, valid_to=item.valid_to, valid_precision=item.valid_precision,
                           valid_end_state=item.valid_end_state)
                content_sha = p.claim_content(row)
                existing = self.db.execute("SELECT id FROM claims WHERE project_id=? AND content_sha=?",
                                           (project_id, content_sha)).fetchone()
                claim_id = existing[0] if existing else p.uuid_identity([project_id, content_sha])
                if not existing:
                    self._write("INSERT INTO claims VALUES (?,?,?,?,?,?,?,?,'reported','reported',?,?,?,?,?,NULL,NULL,1,?)",
                                (claim_id, project_id, row["subject_id"], item.kind, item.predicate, row["value_json"],
                                 item.statement, item.rationale, item.valid_from, item.valid_to, item.valid_precision,
                                 item.valid_end_state, self.clock(), content_sha))
                    created.add(claim_id)
                if self._attach(project_id, claim_id, item.evidence, sources):
                    changed.add(claim_id)
                claims[item.ref] = claim_id
            for relation in validated.relations:
                from_id = claims[relation.from_claim.ref] if relation.from_claim.ref else relation.from_claim.claim_id
                to_id = claims[relation.to_claim.ref] if relation.to_claim.ref else relation.to_claim.claim_id
                self._edge(project_id, from_id, relation.kind, to_id, relation.rationale)
                changed.update((from_id, to_id))
                if relation.kind == "contradicts":
                    for member in (from_id, to_id):
                        row = self._claim(project_id, member)
                        if row["status"] in p.LIVE_STATUSES:
                            self._write("UPDATE claims SET status='disputed' WHERE id=?", (member,))
                            conflicts.add(member)
            for claim_id in set(claims.values()):
                if self._claim(project_id, claim_id)["status"] in p.LIVE_STATUSES:
                    conflicts.update(self._dispute(project_id, claim_id, changed))
            revision = project["revision"] + 1
            self._snapshots(project_id, changed, created, revision, operation_id)
            self._write("UPDATE proposals SET state='applied' WHERE id=?", (proposal_id,))
            result = ApplyResult(operation_id, project_id, revision, tuple(sorted(created)),
                                 tuple(sorted(changed - created)), tuple(sorted(conflicts)), True)
            self._finish(result, request_sha, project["revision"], "agent", "proposal_apply")
            return result

    def review_claim(self, request):
        if not isinstance(request, ReviewRequest):
            raise ValidationError("invalid_review")
        p.uuid_id(request.operation_id)
        p.uuid_id(request.claim_id)
        p.integer(request.expected_project_revision)
        p.integer(request.expected_claim_revision, 1)
        p.enum(request.action, ("accept", "supersede", "retract", "quarantine", "restore"))
        p.enum(request.actor_kind, ("system", "agent", "operator"))
        p.text(request.actor_id)
        p.text(request.reason, 1000)
        if request.approval_ref is not None:
            p.text(request.approval_ref)
        if request.action in ("accept", "supersede") or request.target_status == "accepted":
            if request.actor_kind != "operator" or not request.approval_ref:
                raise KnowledgeError("approval_required")
        if request.action == "supersede":
            p.uuid_id(request.successor_claim_id)
            p.integer(request.expected_successor_revision, 1)
        elif any(value is not None for value in (request.successor_claim_id, request.expected_successor_revision,
                                                 request.effective_boundary)):
            raise ValidationError("invalid_review_fields")
        if request.action == "restore":
            p.enum(request.target_status, ("reported", "accepted"))
            if not 1 <= len(request.evidence) <= 4:
                raise ValidationError("evidence_limit")
        elif request.target_status is not None or request.evidence:
            raise ValidationError("invalid_review_fields")
        request_sha = p.identity(["review", asdict(request)])
        with self._tx():
            project = self._project(request.project_id)
            replay = self._replay(request.operation_id, request.project_id, request_sha, ApplyResult)
            if replay is not None:
                return replay
            if project["revision"] != request.expected_project_revision:
                raise KnowledgeError("revision_conflict")
            claim = self._claim(request.project_id, request.claim_id, request.expected_claim_revision)
            changed, conflicts = {request.claim_id}, set()
            status = claim["status"]
            if request.action == "accept":
                if status not in p.LIVE_STATUSES:
                    raise KnowledgeError("invalid_transition")
                self._supports(request.project_id, request.claim_id)
                competitors = self._competitors(request.project_id, request.claim_id)
                status = "disputed" if competitors else "accepted"
                conflicts.update(competitors)
                if competitors:
                    conflicts.add(request.claim_id)
            elif request.action == "supersede":
                successor = self._claim(request.project_id, request.successor_claim_id, request.expected_successor_revision)
                if status not in p.LIVE_STATUSES or successor["status"] not in p.LIVE_STATUSES:
                    raise KnowledgeError("invalid_transition")
                self._supports(request.project_id, request.successor_claim_id)
                self._edge(request.project_id, request.successor_claim_id, "supersedes", request.claim_id, request.reason)
                status = "superseded"
                if request.effective_boundary is not None:
                    p.temporal(claim["valid_from"], request.effective_boundary, claim["valid_precision"], "closed")
                    updated = dict(claim, valid_to=request.effective_boundary, valid_end_state="closed")
                    self._write("UPDATE claims SET valid_to=?,valid_end_state='closed',content_sha=? WHERE id=?",
                                (request.effective_boundary, p.claim_content(updated), request.claim_id))
                competitors = self._competitors(request.project_id, request.successor_claim_id, (request.claim_id,))
                successor_status = "disputed" if competitors else "accepted"
                self._write("UPDATE claims SET status=?,reviewed_at=? WHERE id=?",
                            (successor_status, self.clock(), request.successor_claim_id))
                changed.add(request.successor_claim_id)
                if competitors:
                    conflicts.update({request.successor_claim_id, *competitors})
            elif request.action in ("retract", "quarantine"):
                status = "retracted" if request.action == "retract" else "quarantined"
            else:
                if status not in ("retracted", "quarantined"):
                    raise KnowledgeError("invalid_transition")
                spans = tuple(p.evidence_span(span) for span in request.evidence)
                fresh = False
                for span in spans:
                    self._validate_span(request.project_id, span)
                    if span.role == "supports" and self.db.execute(
                            "SELECT 1 FROM evidence WHERE claim_id=? AND source_id=? AND source_start=? AND source_end=? AND role=?",
                            (request.claim_id, span.source_id, span.start, span.end, span.role)).fetchone() is None:
                        fresh = True
                if not fresh:
                    raise KnowledgeError("fresh_evidence_required")
                self._attach(request.project_id, request.claim_id, spans)
                self._supports(request.project_id, request.claim_id)
                competitors = self._competitors(request.project_id, request.claim_id)
                status = "disputed" if competitors else request.target_status
                if competitors:
                    conflicts.update({request.claim_id, *competitors})
            self._write("UPDATE claims SET status=?,reviewed_at=? WHERE id=?",
                        (status, self.clock(), request.claim_id))
            if status == "disputed":
                conflicts.update(self._dispute(request.project_id, request.claim_id, changed))
            if request.action == "supersede" and conflicts:
                conflicts.update(self._dispute(request.project_id, request.successor_claim_id, changed, (request.claim_id,)))
            revision = project["revision"] + 1
            self._snapshots(request.project_id, changed, set(), revision, request.operation_id, request)
            result = ApplyResult(request.operation_id, request.project_id, revision, (), tuple(sorted(changed)),
                                 tuple(sorted(conflicts)), True)
            self._finish(result, request_sha, project["revision"], request.actor_kind, request.actor_id, request.approval_ref)
            return result

    def backup(self, destination):
        if self.db.in_transaction:
            raise KnowledgeError("backup_in_transaction")
        destination = Path(destination).resolve()
        if destination == self.path or destination.exists():
            raise KnowledgeError("backup_destination_exists")
        _git_safe(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb"):
            pass
        target = sqlite3.connect(destination)
        try:
            deadline = time.monotonic() + 30
            def progress(_status, _remaining, _total):
                if time.monotonic() > deadline:
                    raise KnowledgeError("backup_timeout")
            self.db.backup(target, pages=128, progress=progress, sleep=0.05)
            if [row[0] for row in target.execute("PRAGMA integrity_check")] != ["ok"]:
                raise KnowledgeError("integrity_failed")
            if target.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise KnowledgeError("integrity_failed")
            revisions = dict(target.execute("SELECT id,revision FROM projects"))
            version = int(target.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0])
            backup_id = str(uuid.uuid4())
        except sqlite3.Error:
            raise KnowledgeError("backup_failed") from None
        finally:
            target.close()
        return BackupResult(backup_id, version, revisions, hashlib.sha256(destination.read_bytes()).hexdigest(),
                            "ok", self.clock())


def backup_store(store, destination):
    return store.backup(destination)


def import_capture(store, project_id, capture_identity, capture_fingerprint, sources):
    return store.import_capture(project_id, capture_identity, capture_fingerprint, sources)


def apply_proposal(store, project_id, proposal_id, expected_project_revision, operation_id):
    return store.apply_proposal(project_id, proposal_id, expected_project_revision, operation_id)


def review_claim(store, request):
    return store.review_claim(request)


validate_proposal = p.validate_proposal
