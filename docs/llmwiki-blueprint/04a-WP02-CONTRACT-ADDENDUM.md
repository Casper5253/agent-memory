# 04a. WP-02 contract addendum

Revision 1.3a, 2026-10-22. This addendum closes three gaps the WP-02 implementer found in [04](04-DATA-AND-INTERFACE-CONTRACTS.md). It adds definitions only. Where 04 already states a rule, 04 wins. Contract owner: Claude.

## Identifiers

All generated IDs are lowercase UUIDv5 strings in the namespace `NS = uuid5(NAMESPACE_URL, "agentmem:knowledge:v1")`. The name is the listed fields joined by U+001F.

| Row | Name fields |
|---|---|
| projects | caller-supplied stable ID (from `knowledge_project_ids`) |
| imports | agent, session_id, part, capture_sha |
| claims | project_id, content_sha |

Source, entity and proposal identities follow the canonical arrays in 04, which take precedence. A claim ID is derived from its content identity, so an exact duplicate resolves to the existing claim and only attaches evidence (04 "exact duplicate").

## 1. Capture import

`capture_identity` is `CaptureIdentity(agent: str, session_id: str, part: int)`. `agent` is one of `claude`, `codex` or `opencode`. `session_id` is non-empty and never the redaction marker. `part` is at least 1.

`capture_fingerprint` is a 64-character lowercase hex SHA-256, stored as `imports.capture_sha`. It is the capture's content hash from its WP-01 recovery receipt, or the manifest `captures.digest_sha` for a legacy capture without one.

`sources` is a list of `SourceInput`. It holds the `sources` columns except `id` and `project_id`, which the store derives. `span_ordinal` values are unique within the list.

`ImportResult` is a frozen dataclass:

```text
ImportResult(import_id, project_id, project_revision, source_ids, extraction_job_id, empty, replayed)
```

`source_ids` is ordered by span_ordinal. `extraction_job_id` is null when no span is eligible, and then `empty` is true. A first import writes the import row, sources, import_sources and at most one extraction job, records an operation with ID `import:<import_id>`, and increments the project revision once. Repeating the same identity and fingerprint returns the stored result with `replayed=true` and writes nothing. The same identity with a different fingerprint returns `capture_identity_conflict` and writes nothing, which matches the WP-01 edge rule. A source that would violate the 04 excerpt or packet limits rejects the whole import.

## 2. Proposal payload

`proposals.payload_json` stores the canonical JSON of the validated payload below. `validate_proposal` parses model JSON into `ValidatedProposal`, which has the same shape with typed fields. Unknown keys at any level are a `ValidationError`, so a model cannot supply `status`, `evidence_level`, `speaker`, `user_stated`, `quote_sha`, IDs or approval fields.

```json
{
  "schema_version": 1,
  "entities": [
    {"ref": "e1", "kind": "component", "canonical_key": "storage", "display_name": "Storage", "aliases": []}
  ],
  "claims": [
    {"ref": "c1", "subject_ref": "e1", "kind": "decision", "predicate": "storage_backend", "value": "sqlite",
     "statement": "New work uses SQLite.", "rationale": "Atomic cursor updates.",
     "valid_from": null, "valid_to": null, "valid_precision": "unknown", "valid_end_state": "unknown",
     "evidence": [{"source_id": "<uuid>", "start": 0, "end": 42, "role": "supports"}]}
  ],
  "relations": [
    {"from": {"ref": "c1"}, "kind": "contradicts", "to": {"claim_id": "<uuid>", "expected_revision": 3}, "rationale": "Same slot, different value."}
  ]
}
```

- `ref` matches `^[a-z0-9_-]{1,32}$` and is local to the proposal. `subject_ref` names an entity in the same payload. An entity whose `(kind, canonical_key)` already exists in the project resolves to that entity.
- Each claim has at least one evidence span with role `supports`. Every `source_id` must belong to the proposal's input group. Offsets follow 04. The validator computes `quote_sha` itself.
- A relation endpoint is either `{"ref"}` for a claim in this payload or `{"claim_id", "expected_revision"}` for an existing claim. `apply_proposal` rechecks every `expected_revision` and returns `revision_conflict` on any mismatch.
- A model may propose only `contradicts`, `depends_on` and `derived_from` relations. `supersedes` exists only as a review operation (04).
- Every claim created from a proposal is `reported` with evidence level `reported`. The exact-slot rule in 04 may then mark members `disputed` during apply. Nothing else changes authority.
- Temporal and value rules are those in 04.

## 3. Review and restore

`ReviewRequest` is a frozen dataclass:

```text
ReviewRequest(operation_id, project_id, action, claim_id, expected_project_revision, expected_claim_revision,
              actor_kind, actor_id, approval_ref, reason, target_status=None, evidence=(),
              successor_claim_id=None, expected_successor_revision=None, effective_boundary=None)
```

`action` is one of `accept`, `supersede`, `retract`, `quarantine` or `restore`. `accept` and `supersede` require `actor_kind="operator"` and a non-empty `approval_ref`. Every action requires a non-empty `reason`. `supersede` uses the successor fields as 04 defines.

Restore applies only to a `retracted` or `quarantined` claim. `evidence` supplies the fresh evidence as one or more spans `{source_id, start, end, role}`. The spans name existing sources in the same project whose stored `content_sha` still matches. At least one span has role `supports` and is not already attached to the claim. `target_status` is `reported` or `accepted`. `accepted` needs operator approval as above. If the claim still has an unresolved exact-slot competitor, the result is `disputed` whatever the target, and `conflict_ids` lists the competitors. Restore writes a new claim revision and never deletes an old one.

## 4. Import scope and revision

The signature is `import_capture(store, project_id, capture_identity, capture_fingerprint, sources) -> ImportResult`. The caller passes the project explicitly, as 04 requires of every mutation; it comes from WP-01 attribution or `knowledge_project_ids`. Every source row must carry that project.

Import is exempt from caller-supplied revision compare-and-swap. An import only appends rows keyed by capture identity, never reads or changes existing claims, and is idempotent. The store reads the current project revision inside `BEGIN IMMEDIATE` and increments it once. Two concurrent imports of the same identity produce one import and one replay. A missing, archived or `excluded` project returns an error and writes nothing. `apply_proposal` and `review_claim` keep the strict expected-revision check, and a stale one writes zero rows.

## 5. Gaps found during implementation

For any further gap that does not add or change a stored column, enum value, status transition or authority rule, choose the reading most consistent with 04 and this addendum, list it under Assumptions in the report, and continue. Stop and report only for a gap that would change one of those.

## 6. Authority rulings from the WP-02 review

- A model-proposed `contradicts` relation is stored as a relation only. It never changes any claim's status. Claims become `disputed` only through the exact-slot rule in 04 or an operator-reviewed semantic conflict.
- `retract` requires `actor_kind="operator"` and an `approval_ref`. `quarantine` may also come from `actor_kind="system"`, which is used for integrity and privacy failures, and requires a reason in every case.
- `content_sha` is fixed when a claim is created and never rewritten. A review that closes an interval updates `valid_to` and `valid_end_state` and records the change in the revision snapshot. The claim keeps its content identity, so a later exact duplicate still resolves to it.
- `accept` on a member of an unresolved exact-slot conflict returns `unresolved_conflict` and writes nothing: no claim change, no revision increment, no operation row. Only `supersede`, `retract` or `quarantine` can end the conflict.
