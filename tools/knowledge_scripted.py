"""Frozen synthetic extraction replay. No gold or expected-row access."""
import json
import re


def seed_repository_heads(roots, projects):
    import os
    import subprocess
    from pathlib import Path
    for project in projects:
        head = project.get("repo", {}).get("head")
        if not head:
            continue
        for directory in roots[project["id"]]["roots"]:
            subprocess.run(["git", "init", "-q", directory], check=True, capture_output=True, timeout=5,
                env=dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull))
            (Path(directory) / ".git" / "HEAD").write_text(head + "\n", encoding="ascii")


def _instruction_only(sentence):
    return bool(re.search(
        r"\b(?:ignore|disregard|override)\s+(?:(?:all|the)\s+)?(?:previous|prior|earlier)\s+instructions\b"
        r"|\bmark\s+this\s+(?:as\s+)?(?:verified|accepted)\b"
        r"|\b(?:execute|run)\s+(?:a\s+)?(?:shell|terminal|system)\s+command\b",
        sentence, re.IGNORECASE))


class ScriptedExtractor:
    version = "scripted-v1"

    def __init__(self, source_metadata):
        self.source_metadata = source_metadata
        self.calls = 0

    def __call__(self, prompt):
        self.calls += 1
        data = json.loads(prompt.split("EXTRACTION INPUT:\n", 1)[1].split("\n\nOUTPUT SCHEMA", 1)[0])
        entities, claims = {}, []
        for source in data["sources"]:
            text = source["excerpt"].strip()
            fenced = re.search(r"```[^\n]*\n(.*?)```", text, re.S)
            if fenced:
                text = fenced[1].strip()
            text = re.sub(r"^(?:The source says:|According to the source:)\s*", "", text)
            head, separator, rationale = text.partition(" Reason: ")
            sentences = re.split(r"(?<=[.!?])\s+", head)
            statement = next((sentence.strip() for sentence in sentences
                              if not _instruction_only(sentence)
                              and not re.match(r"(?i)^(?:record|review|add|follow|please)\b", sentence.strip())
                              and re.search(r"\b[\w]+(?:-[\w]+){2,}\b", sentence)), "")
            subject = re.search(r"\b[\w]+(?:-[\w]+){2,}\b", statement)
            if not subject:
                continue
            subject = subject[0]
            if not statement or len(statement) > 600:
                continue
            rationale = rationale.strip().rstrip(".") if separator else ""
            if len(rationale) > 1000:
                rationale = rationale[:1000].rsplit(" ", 1)[0]
            predicate, value, kind = "statement", statement, "fact"
            storage = re.search(r"(?i)\b(?:use|uses)\s+(\w+)\b.*\bstorage\b", statement)
            slot = re.search(r"\b(api_mode|release|capacity|runtime_version)\s+is\s+([^.;]+)", statement)
            if storage and source["speaker"] != "pasted":
                predicate, value, kind = "storage_backend", storage[1].casefold(), "decision"
            elif re.search(r"\bcurrent release\b|\brevision\b.*\brepo\b", statement):
                predicate = "repo_state"
            elif slot:
                predicate, value = slot[1], slot[2].strip()
            ref = entities.setdefault(subject, "e" + str(len(entities)))
            metadata = self.source_metadata[source["source_id"]]
            effective = None if re.search(r"(?i)no effective date|undated|unknown effective", text) else metadata.get("occurred_at")
            claims.append(dict(ref="c" + str(len(claims)), subject_ref=ref, kind=kind, predicate=predicate, value=value,
                statement=statement, rationale=rationale, valid_from=effective, valid_to=None,
                valid_precision="instant" if effective else "unknown", valid_end_state="open" if effective else "unknown",
                evidence=[dict(source_id=source["source_id"], start=0, end=len(source["excerpt"]), role="supports")]))
            if len(claims) == 20:
                break
        return json.dumps(dict(schema_version=1,
            entities=[dict(ref=ref, kind="component", canonical_key=name, display_name=name, aliases=[]) for name, ref in entities.items()],
            claims=claims, relations=[]))


def extract_seeded_captures(cfg):
    from agentmem.knowledge.store import KnowledgeStore
    from agentmem.knowledge.proposals import extract_and_apply
    from agentmem.knowledge.models import KnowledgeError
    path = cfg.vault / cfg.knowledge_db
    with KnowledgeStore(path) as store:
        metadata = {row["id"]: dict(row) for row in store.db.execute("SELECT * FROM sources")}
        jobs = [row[0] for row in store.db.execute("SELECT source_id FROM jobs WHERE kind='extract' ORDER BY project_id,source_id")]
    extractor = ScriptedExtractor(metadata)
    outcomes = []
    for source in jobs:
        try:
            result = extract_and_apply(path, source, extractor, cfg=cfg, extractor_version=extractor.version, timeout_s=10)
            outcomes.append(dict(source_id=source, status="applied", claims=len(result.created_claim_ids)))
        except KnowledgeError as error:
            outcomes.append(dict(source_id=source, status="error", code=error.code))
    return dict(extraction="scripted", version=extractor.version, calls=extractor.calls,
                applied=sum(row["status"] == "applied" for row in outcomes), outcomes=outcomes)


def apply_seeded_approvals(cfg, approvals, fixture_sources):
    import uuid
    from agentmem.knowledge.store import KnowledgeStore
    from agentmem.knowledge.models import ReviewRequest, KnowledgeError
    sources = {source["id"]: source for source in fixture_sources}
    outcomes = []
    with KnowledgeStore(cfg.vault / cfg.knowledge_db, writable=True) as store:
        mapped = []
        for approval in approvals:
            source = sources[approval["source_id"]]
            candidates = store.db.execute("SELECT DISTINCT c.* FROM claims c JOIN evidence e ON e.claim_id=c.id "
                "JOIN sources s ON s.id=e.source_id WHERE c.project_id=? AND s.agent=? AND s.session_id=? AND s.part=?",
                (approval["project_id"], source["agent"], source["session_id"], source["part"])).fetchall()
            if len(candidates) != 1:
                outcomes.append(dict(approval_ref=approval["approval_ref"], status="unmapped"))
                continue
            target = dict(candidates[0])
            mapped.append((approval, target))
        latest = {}
        for approval, target in mapped:
            slot = (target["project_id"], target["subject_id"], target["predicate"])
            latest[slot] = max(latest.get(slot, ""), approval["effective_at"])
        for approval, target in mapped:
            slot = (target["project_id"], target["subject_id"], target["predicate"])
            if approval["effective_at"] != latest[slot]:
                outcomes.append(dict(approval_ref=approval["approval_ref"], status="covered_by_later_seeded_review"))
                continue
            competitors = store.db.execute("SELECT * FROM claims WHERE project_id=? AND subject_id=? AND predicate=? "
                "AND id!=? AND status IN ('reported','accepted','disputed') AND value_json!=? "
                "AND valid_from IS NOT NULL AND valid_from<? ORDER BY id",
                (target["project_id"], target["subject_id"], target["predicate"], target["id"], target["value_json"], approval["effective_at"])).fetchall()
            try:
                for old in competitors or [None]:
                    current = store.get_claim(target["project_id"], target["id"])
                    request = ReviewRequest(str(uuid.uuid4()), target["project_id"], "supersede" if old else "accept",
                        old["id"] if old else target["id"], store.get_project(target["project_id"])["revision"],
                        old["revision"] if old else current["revision"], "operator", approval["operator"],
                        approval["approval_ref"], approval["reason"], successor_claim_id=target["id"] if old else None,
                        expected_successor_revision=current["revision"] if old else None,
                        effective_boundary=approval["effective_at"] if old else None)
                    store.review_claim(request)
                outcomes.append(dict(approval_ref=approval["approval_ref"], status="applied"))
            except KnowledgeError as error:
                outcomes.append(dict(approval_ref=approval["approval_ref"], status="error", code=error.code))
    return outcomes
