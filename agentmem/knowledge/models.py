"""Typed inputs and results for the version 1 knowledge service."""
from dataclasses import dataclass
from enum import Enum
from typing import Any


class Mode(str, Enum):
    OFF = "off"
    SHADOW = "shadow"
    ACTIVE = "active"


class Privacy(str, Enum):
    EXCLUDED = "excluded"
    LOCAL_ONLY = "local_only"
    MODEL_ALLOWED = "model_allowed"


class ClaimStatus(str, Enum):
    REPORTED = "reported"
    ACCEPTED = "accepted"
    DISPUTED = "disputed"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"
    QUARANTINED = "quarantined"


class KnowledgeError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ValidationError(KnowledgeError):
    pass


@dataclass(frozen=True)
class CaptureIdentity:
    agent: str
    session_id: str
    part: int


@dataclass(frozen=True)
class SourceInput:
    agent: str
    session_id: str
    part: int
    kind: str
    cursor_kind: str
    start: int
    end: int
    span_ordinal: int
    locator_json: dict
    original_range_sha: str | None
    note_path: str
    content_sha: str
    recorded_at: str
    occurred_at: str | None
    occurred_precision: str
    excerpt: str
    origin_group: str
    privacy: str
    speaker: str
    repo_head: str | None = None


@dataclass(frozen=True)
class EvidenceSpan:
    source_id: str
    start: int
    end: int
    role: str = "supports"


@dataclass(frozen=True)
class EntityInput:
    ref: str
    kind: str
    canonical_key: str
    display_name: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class ClaimInput:
    ref: str
    subject_ref: str
    kind: str
    predicate: str
    value: Any
    statement: str
    rationale: str
    valid_from: str | None
    valid_to: str | None
    valid_precision: str
    valid_end_state: str
    evidence: tuple[EvidenceSpan, ...]


@dataclass(frozen=True)
class ClaimEndpoint:
    ref: str | None = None
    claim_id: str | None = None
    expected_revision: int | None = None


@dataclass(frozen=True)
class RelationInput:
    from_claim: ClaimEndpoint
    kind: str
    to_claim: ClaimEndpoint
    rationale: str


@dataclass(frozen=True)
class ValidatedProposal:
    schema_version: int
    entities: tuple[EntityInput, ...]
    claims: tuple[ClaimInput, ...]
    relations: tuple[RelationInput, ...]


@dataclass(frozen=True)
class ReviewRequest:
    operation_id: str
    project_id: str
    action: str
    claim_id: str
    expected_project_revision: int
    expected_claim_revision: int
    actor_kind: str
    actor_id: str
    approval_ref: str | None
    reason: str
    target_status: str | None = None
    evidence: tuple[EvidenceSpan, ...] = ()
    successor_claim_id: str | None = None
    expected_successor_revision: int | None = None
    effective_boundary: str | None = None


@dataclass(frozen=True)
class ImportResult:
    import_id: str
    project_id: str
    project_revision: int
    source_ids: tuple[str, ...]
    extraction_job_id: str | None
    empty: bool
    replayed: bool = False


@dataclass(frozen=True)
class ApplyResult:
    operation_id: str
    project_id: str
    project_revision: int
    created_claim_ids: tuple[str, ...]
    changed_claim_ids: tuple[str, ...]
    conflict_ids: tuple[str, ...]
    projection_pending: bool
    replayed: bool = False


@dataclass(frozen=True)
class BackupResult:
    backup_id: str
    schema_version: int
    project_revisions: dict[str, int]
    file_sha: str
    integrity_result: str
    recorded_at: str
