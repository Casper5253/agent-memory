"""Boundary validation and immutable identity rules. No storage or external calls."""
import hashlib
import json
import re
import uuid
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from pathlib import PurePosixPath, PureWindowsPath

from .models import ValidationError

SINGLE_VALUED = frozenset({"storage_backend", "runtime_version", "api_mode", "deployment_target",
                          "head_commit", "release_tag", "branch", "test_count", "deployed_version", "file_location"})
LIVE_STATUSES = ("reported", "accepted", "disputed")
NS = uuid.uuid5(uuid.NAMESPACE_URL, "agentmem:knowledge:v1")


def canonical(value):
    try:
        if is_dataclass(value):
            value = asdict(value)
        result = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        result.encode("utf-8", errors="strict")
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValidationError("invalid_json") from None


def sha_text(value):
    try:
        return hashlib.sha256(value.encode("utf-8", errors="strict")).hexdigest()
    except (AttributeError, UnicodeError):
        raise ValidationError("invalid_utf8") from None


def identity(fields):
    return sha_text(canonical(fields))


def uuid_identity(fields):
    return str(uuid.uuid5(NS, "\x1f".join(map(str, fields))))


def text(value, maximum=None, *, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValidationError("invalid_text")
    sha_text(value)
    if maximum is not None and len(value) > maximum:
        raise ValidationError("text_limit")
    return value


def integer(value, minimum=0):
    if type(value) is not int or value < minimum or value > 2**63 - 1:
        raise ValidationError("invalid_integer")
    return value


def enum(value, choices):
    if not isinstance(value, str) or value not in choices:
        raise ValidationError("invalid_enum")
    return value


def digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValidationError("invalid_hash")
    return value


def uuid_id(value):
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError
    except (TypeError, ValueError, AttributeError):
        raise ValidationError("invalid_uuid") from None
    return value


def instant(value):
    text(value)
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)", value):
            raise ValueError
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError("invalid_timestamp") from None
    return value


def time_value(value, precision):
    if precision == "day":
        return date.fromisoformat(value)
    utc_text = value[:-1] if value.endswith("Z") else value[:-6]
    fraction = utc_text[20:].rstrip("0") if len(utc_text) > 19 else ""
    return datetime.fromisoformat(utc_text[:19]), fraction


def temporal(start, end, precision, end_state):
    enum(precision, ("instant", "day", "unknown"))
    enum(end_state, ("unknown", "open", "closed"))
    if (start is None) != (precision == "unknown"):
        raise ValidationError("invalid_interval")
    for value in (start, end):
        if value is None:
            continue
        if precision == "instant":
            instant(value)
        elif precision == "day":
            try:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    raise ValueError
                date.fromisoformat(value)
            except (ValueError, TypeError):
                raise ValidationError("invalid_date") from None
        else:
            raise ValidationError("invalid_interval")
    if end_state == "closed":
        if start is None or end is None or time_value(end, precision) <= time_value(start, precision):
            raise ValidationError("invalid_interval")
    elif end is not None:
        raise ValidationError("invalid_interval")


def value_json(value):
    if value is None or type(value) in (str, int, bool):
        return canonical(value)
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        if list(value) == sorted(value):
            return canonical(value)
    raise ValidationError("unsupported_value")


def relative_path(value, vault=None):
    text(value)
    if "\\" in value or ":" in value or "\x00" in value or PureWindowsPath(value).drive:
        raise ValidationError("unsafe_path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValidationError("unsafe_path")
    if path.parts[0] not in ("0-Sessions", "1-Projects", "2-Areas", "3-Resources", "4-Archive"):
        raise ValidationError("unsafe_path")
    if vault is not None and not (vault / value).resolve().is_relative_to(vault.resolve()):
        raise ValidationError("unsafe_path")
    return value


def keys(obj, required, optional=()):
    if not isinstance(obj, dict) or set(obj) - set(required) - set(optional) or set(required) - set(obj):
        raise ValidationError("invalid_keys")


def validate_source(source, capture):
    from .models import SourceInput
    if not isinstance(source, SourceInput):
        raise ValidationError("invalid_source")
    if (source.agent, source.session_id, source.part) != (capture.agent, capture.session_id, capture.part):
        raise ValidationError("capture_identity_conflict")
    enum(source.kind, ("transcript_span", "legacy_note", "manual_statement", "approved_document"))
    enum(source.cursor_kind, ("bytes", "messages", "note_lines", "manual"))
    integer(source.start)
    integer(source.end, source.start)
    integer(source.span_ordinal)
    enum(source.speaker, ("user", "assistant", "pasted", "unknown"))
    enum(source.privacy, ("excluded", "local_only", "model_allowed"))
    text(source.excerpt, empty=True)
    if len(source.excerpt.encode("utf-8")) > 6000:
        raise ValidationError("excerpt_limit")
    if digest(source.content_sha) != sha_text(source.excerpt):
        raise ValidationError("source_hash_mismatch")
    if source.original_range_sha is not None:
        digest(source.original_range_sha)
    if source.repo_head is not None:
        if not isinstance(source.repo_head, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", source.repo_head):
            raise ValidationError("invalid_repo_head")
    relative_path(source.note_path)
    instant(source.recorded_at)
    temporal(source.occurred_at, None, source.occurred_precision, "unknown")
    text(source.origin_group)
    keys(source.locator_json, ("format_version", "provider_record_type", "event_ordinal", "message_ids", "redactor_version", "truncated"))
    text(source.locator_json["provider_record_type"])
    for key in ("format_version", "redactor_version"):
        version = source.locator_json[key]
        if type(version) is int:
            integer(version, 1)
        else:
            text(version)
    integer(source.locator_json["event_ordinal"])
    ids = source.locator_json["message_ids"]
    if not isinstance(ids, list) or any(not isinstance(x, str) or not x for x in ids):
        raise ValidationError("invalid_message_ids")
    if source.cursor_kind == "bytes" and ids:
        raise ValidationError("invalid_message_ids")
    if type(source.locator_json["truncated"]) is not bool:
        raise ValidationError("invalid_truncated")
    canonical(source.locator_json)


def overlaps(a, b):
    if a["valid_precision"] != b["valid_precision"] or a["valid_precision"] == "unknown":
        return True
    if a["valid_end_state"] == "unknown" or b["valid_end_state"] == "unknown":
        return True
    return ((a["valid_to"] is None or time_value(b["valid_from"], b["valid_precision"]) < time_value(a["valid_to"], a["valid_precision"])) and
            (b["valid_to"] is None or time_value(a["valid_from"], a["valid_precision"]) < time_value(b["valid_to"], b["valid_precision"])))


def validate_proposal(model_json):
    from .models import (ClaimEndpoint, ClaimInput, EntityInput, EvidenceSpan, RelationInput, ValidatedProposal)
    if isinstance(model_json, ValidatedProposal):
        model_json = proposal_payload(model_json)
    if isinstance(model_json, (str, bytes)):
        try:
            raw = model_json.encode("utf-8", errors="strict") if isinstance(model_json, str) else model_json
            raw.decode("utf-8", errors="strict")
        except UnicodeError:
            raise ValidationError("invalid_utf8") from None
        if len(raw) > 32 * 1024:
            raise ValidationError("proposal_limit")
        def unique_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValidationError("duplicate_key")
                result[key] = value
            return result
        try:
            model_json = json.loads(model_json, object_pairs_hook=unique_pairs)
        except (ValueError, UnicodeError, RecursionError):
            raise ValidationError("invalid_json") from None
    encoded = canonical(model_json)
    if len(encoded.encode("utf-8")) > 32 * 1024:
        raise ValidationError("proposal_limit")
    model_json = json.loads(encoded)
    keys(model_json, ("schema_version", "entities", "claims", "relations"))
    if type(model_json["schema_version"]) is not int or model_json["schema_version"] != 1:
        raise ValidationError("unsupported_schema")
    for name, cap in (("entities", 8), ("claims", 20), ("relations", 20)):
        if not isinstance(model_json[name], list) or len(model_json[name]) > cap:
            raise ValidationError("proposal_limit")
    def ref(value):
        if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9_-]{1,32}", value):
            raise ValidationError("invalid_ref")
        return value
    entities, claims, relations = [], [], []
    for item in model_json["entities"]:
        keys(item, ("ref", "kind", "canonical_key", "display_name", "aliases"))
        ref(item["ref"])
        enum(item["kind"], ("project", "file", "component", "concept"))
        text(item["canonical_key"])
        text(item["display_name"], 120)
        if not isinstance(item["aliases"], list) or len(item["aliases"]) > 10:
            raise ValidationError("alias_limit")
        for alias in item["aliases"]:
            text(alias, 120)
        entities.append(EntityInput(**dict(item, aliases=tuple(item["aliases"]))))
    entity_refs = {entity.ref for entity in entities}
    if len(entity_refs) != len(entities):
        raise ValidationError("duplicate_ref")
    for item in model_json["claims"]:
        keys(item, ("ref", "subject_ref", "kind", "predicate", "value", "statement", "rationale",
                    "valid_from", "valid_to", "valid_precision", "valid_end_state", "evidence"))
        ref(item["ref"])
        if ref(item["subject_ref"]) not in entity_refs:
            raise ValidationError("unknown_ref")
        enum(item["kind"], ("fact", "decision", "preference", "question", "task"))
        text(item["predicate"], 120)
        value_json(item["value"])
        text(item["statement"], 600)
        text(item["rationale"], 1000, empty=True)
        temporal(item["valid_from"], item["valid_to"], item["valid_precision"], item["valid_end_state"])
        if not isinstance(item["evidence"], list) or not 1 <= len(item["evidence"]) <= 4:
            raise ValidationError("evidence_limit")
        spans = tuple(evidence_span(span) for span in item["evidence"])
        if not any(span.role == "supports" for span in spans):
            raise ValidationError("missing_support")
        claims.append(ClaimInput(**dict(item, evidence=spans)))
    claim_refs = {claim.ref for claim in claims}
    if len(claim_refs) != len(claims):
        raise ValidationError("duplicate_ref")
    def endpoint(item):
        if isinstance(item, dict) and set(item) == {"ref"}:
            if ref(item["ref"]) not in claim_refs:
                raise ValidationError("unknown_ref")
            return ClaimEndpoint(ref=item["ref"])
        keys(item, ("claim_id", "expected_revision"))
        uuid_id(item["claim_id"])
        integer(item["expected_revision"], 1)
        return ClaimEndpoint(claim_id=item["claim_id"], expected_revision=item["expected_revision"])
    for item in model_json["relations"]:
        keys(item, ("from", "kind", "to", "rationale"))
        enum(item["kind"], ("contradicts", "depends_on", "derived_from"))
        text(item["rationale"], 1000)
        relations.append(RelationInput(endpoint(item["from"]), item["kind"], endpoint(item["to"]), item["rationale"]))
    return ValidatedProposal(1, tuple(entities), tuple(claims), tuple(relations))


def evidence_span(item):
    from .models import EvidenceSpan
    if isinstance(item, EvidenceSpan):
        item = asdict(item)
    keys(item, ("source_id", "start", "end", "role"))
    digest(item["source_id"])
    integer(item["start"])
    integer(item["end"], item["start"] + 1)
    enum(item["role"], ("supports", "refutes", "context"))
    return EvidenceSpan(**item)


def proposal_payload(proposal):
    def endpoint(item):
        if item.ref is not None:
            return {"ref": item.ref}
        return {"claim_id": item.claim_id, "expected_revision": item.expected_revision}
    return {"schema_version": proposal.schema_version,
            "entities": [dict(asdict(item), aliases=list(item.aliases)) for item in proposal.entities],
            "claims": [dict(asdict(item), evidence=[asdict(span) for span in item.evidence]) for item in proposal.claims],
            "relations": [{"from": endpoint(item.from_claim), "kind": item.kind, "to": endpoint(item.to_claim),
                           "rationale": item.rationale} for item in proposal.relations]}


def claim_content(row):
    return identity([row["subject_id"], row["kind"], row["predicate"], json.loads(row["value_json"]),
                     row["statement"], row["rationale"], row["valid_from"], row["valid_to"],
                     row["valid_precision"], row["valid_end_state"]])
