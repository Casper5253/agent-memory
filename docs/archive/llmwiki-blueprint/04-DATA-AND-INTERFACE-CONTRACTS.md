# Data and interface contracts

Normative proposed contract, schema version 1. All identifiers are opaque to callers. All examples are synthetic. Unknown object keys are rejected in model proposals and mutation requests. Additive response fields are allowed only after incrementing the response minor version. UTF-8 and UTC RFC3339 timestamps are required. Dates without time are represented separately and are never coerced to a claimed exact instant.

## Identity and validation

Canonical JSON uses sorted keys, compact separators, UTF-8, finite numbers only, and no Unicode normalization of source content. Hashes are full lowercase SHA-256 hex. Identifier hashes are computed over canonical JSON arrays, not delimiter-concatenated strings. Examples abbreviate hashes for readability; executable fixtures must use full hashes.

| Identifier | Definition |
|---|---|
| project_id | UUID assigned once when mapping an existing named project; retained on rename |
| source_id | SHA256 of `[project_id, agent, session_id, part, cursor_kind, start, end, span_ordinal, content_sha]` |
| entity_id | SHA256 of `[project_id, entity_kind, canonical_key]` |
| claim_id | UUID assigned on creation; retained across status/review revisions |
| proposal_id | SHA256 of `[source_id, extractor_version, prompt_version, input_sha, base_revision]` |
| operation_id | Caller-provided UUID for manual changes; proposal_id for automatic proposal apply |
| conflict_id | SHA256 of sorted member claim IDs plus project_id |

Project names and paths are not identity. Map canonical roots using `Path.resolve`, Windows case folding where applicable, and longest-root rules. Do not infer project merges from common Git remote names or similar names. A root mapped to two IDs is an error. Session attribution uses the content-based routing of WP-01 A-5, decided per capture part; a manual attribution override wins over it. Unsorted sources are retained but are ineligible for project context until explicitly assigned.

All vault-relative paths must resolve within the configured vault, contain no parent traversal, drive, UNC prefix, alternate data stream, or reparse-point escape, and target only permitted folders. Generated paths are selected by the renderer from IDs and trusted project metadata. Model output cannot contain destination paths. Existing arbitrary note lookup behavior is not inherited by the new API.

## Logical schema

Implement these tables in `knowledge.sqlite`. `store.py` owns schema DDL. Each listed NOT NULL field must be validated before insertion. JSON columns store canonical JSON and are checked with Python validation; do not assume SQLite JSON extensions. Foreign keys are enforced. No SQL identifiers come from callers.

| Table | Columns and constraints |
|---|---|
| meta | key TEXT PK, value TEXT NOT NULL; schema_version=1, db_uuid |
| projects | id TEXT PK; name TEXT NOT NULL; roots_json TEXT NOT NULL; mode TEXT CHECK off/shadow/active; revision INTEGER >=0; privacy TEXT CHECK excluded/local_only/model_allowed; archived INTEGER 0/1; UNIQUE(name COLLATE NOCASE) |
| sources | id TEXT PK; project_id FK; agent TEXT; session_id TEXT; part INTEGER >=1; kind TEXT CHECK transcript_span/legacy_note/manual_statement/approved_document; cursor_kind TEXT CHECK bytes/messages/note_lines/manual; start INTEGER >=0; end INTEGER >=start; span_ordinal INTEGER >=0; locator_json TEXT NOT NULL; original_range_sha TEXT nullable; note_path TEXT NOT NULL; content_sha TEXT NOT NULL; recorded_at TEXT NOT NULL; occurred_at TEXT nullable; occurred_precision TEXT CHECK instant/day/unknown; excerpt TEXT NOT NULL; origin_group TEXT NOT NULL; privacy TEXT; speaker TEXT NOT NULL CHECK user/assistant/pasted/unknown; repo_head TEXT nullable; UNIQUE(project_id,agent,session_id,part,cursor_kind,start,end,span_ordinal,content_sha) |
| entities | id TEXT PK; project_id FK; kind TEXT CHECK project/file/component/concept; canonical_key TEXT NOT NULL; display_name TEXT NOT NULL; aliases_json TEXT NOT NULL; UNIQUE(project_id,kind,canonical_key) |
| claims | id TEXT PK; project_id FK; subject_id FK entities; kind TEXT CHECK fact/decision/preference/question/task; predicate TEXT NOT NULL; value_json TEXT NOT NULL; statement TEXT NOT NULL; rationale TEXT NOT NULL; status TEXT CHECK reported/accepted/disputed/superseded/retracted/quarantined; evidence_level TEXT CHECK reported/documented/verified; valid_from TEXT nullable; valid_to TEXT nullable; valid_precision TEXT CHECK instant/day/unknown; valid_end_state TEXT CHECK unknown/open/closed; recorded_at TEXT NOT NULL; reviewed_at TEXT nullable; review_due_at TEXT nullable; revision INTEGER >=1; content_sha TEXT NOT NULL |
| evidence | claim_id FK; source_id FK; source_start INTEGER; source_end INTEGER; quote_sha TEXT NOT NULL; role TEXT CHECK supports/refutes/context; PK(claim_id,source_id,source_start,source_end,role) |
| relations | project_id FK; from_id FK claims; kind TEXT CHECK supersedes/depends_on/contradicts/derived_from; to_id FK claims; rationale TEXT NOT NULL; PK(from_id,kind,to_id); from_id != to_id |
| proposals | id TEXT PK; project_id FK; source_id FK; base_revision INTEGER; input_sha TEXT; extractor_version TEXT; prompt_version TEXT; payload_json TEXT; state TEXT CHECK queued/ready/applied/rejected/review; error_code TEXT nullable; created_at TEXT; reported_cost_usd REAL nullable; input_tokens INTEGER nullable; output_tokens INTEGER nullable |
| revisions | project_id FK; project_revision INTEGER; claim_id FK; claim_revision INTEGER; snapshot_json TEXT NOT NULL; operation_id TEXT NOT NULL; recorded_at TEXT NOT NULL; PK(claim_id,claim_revision) |
| operations | id TEXT PK; project_id FK; request_sha TEXT NOT NULL; expected_revision INTEGER; result_revision INTEGER; result_json TEXT NOT NULL; actor_kind TEXT CHECK system/agent/operator; actor_id TEXT; approval_ref TEXT nullable; recorded_at TEXT NOT NULL |
| jobs | id TEXT PK; project_id FK; source_id nullable FK; kind TEXT CHECK import/extract/project; dedupe_key TEXT NOT NULL UNIQUE; proposal_id nullable FK; state TEXT CHECK pending/running/done/error/quarantined; attempts INTEGER; next_try_at TEXT nullable; lease_token TEXT nullable; lease_until TEXT nullable; error_code TEXT nullable |
| outbox | project_id FK PRIMARY KEY; desired_revision INTEGER NOT NULL; acknowledged_revision INTEGER NOT NULL DEFAULT 0 |
| mode_transitions (deferred; not created in schema 1, see 03 overview takeover) | project_id FK PRIMARY KEY; operation_id TEXT; from_mode TEXT; to_mode TEXT; state TEXT CHECK pending/complete/external_edit; expected_overview_sha TEXT; backup_path TEXT nullable; target_revision INTEGER; created_at TEXT; completed_at TEXT nullable |
| projections | project_id FK; path TEXT; desired_revision INTEGER; rendered_revision INTEGER; rendered_sha TEXT nullable; status TEXT CHECK pending/clean/external_edit/error; PK(project_id,path) |
| imports | id TEXT PK; agent TEXT; session_id TEXT; part INTEGER; capture_sha TEXT; project_id FK; imported_at TEXT; UNIQUE(agent,session_id,part,capture_sha) |
| import_sources | import_id FK; source_id FK; span_ordinal INTEGER; PK(import_id,source_id); UNIQUE(import_id,span_ordinal) |

Index claims on `(project_id,status,subject_id,predicate)` and `(project_id,review_due_at)`. Index evidence on source_id, relations on both endpoints, jobs on state/next_try_at, sources on project_id/recorded_at, and revisions on project_id/project_revision. A single transaction checks that every linked row has the same project_id. SQLite foreign keys alone cannot enforce that condition.

An excerpt has at most 6,000 UTF-8 bytes. Source packets total at most 32 KiB per capture. Select user and final assistant text in original order, first and last turns first under the cap; retain selection ordinals and `truncated=true` in the proposal input metadata. Never describe an excerpt as the complete session. Source offsets in evidence are Unicode code-point offsets into the redacted excerpt, with end exclusive. The original cursor range remains a separate locator. `quote_sha` hashes the exact selected substring. Missing or out-of-range evidence rejects the proposal.

`locator_json` contains `{format_version,provider_record_type,event_ordinal,message_ids,redactor_version,truncated}`. message_ids is an empty array for byte cursors and the exact covered IDs for OpenCode when supplied; missing IDs are explicitly marked in format_version metadata. `original_range_sha` hashes the original source bytes or canonical original OpenCode messages without storing them. `content_sha` hashes the stored redacted excerpt. Excerpt integrity is checked on every read. Original-source verification happens only in explicit doctor/import verification using trusted provider roots; normal context reads never reopen transcripts. Missing original sources reduce freshness/availability metadata but do not invalidate an intact stored reported excerpt. Only a claim whose verified artifact is missing/mismatched loses verified eligibility.

Job dedupe_key is canonical SHA256 over `[kind,import_id]` for import, `[extract,source_id,extractor_version,prompt_version,base_revision]` for extraction, and `[project,project_id]` for projection. Revision conflict can create one new extraction job generation, but total attempts across all generations for the same source/extractor/prompt version remain capped at three. Count those attempts transactionally before leasing. At most one project outbox row exists; per-path projections are delivery bookkeeping, not the outbox itself. Acknowledge outbox revision only after all required pages and overview match that revision.

Each completed capture import schedules one extraction job, anchored to its lowest-ordinal source_id. `build_proposal_input` loads all spans in that source's import_sources group under the total packet cap. Each proposed evidence span names its source_id and excerpt offsets. The validator permits only sources in that input group. This produces one normal extraction call per capture rather than one call per excerpt. A capture with no eligible spans records a completed empty import and schedules no model call. Deterministic legacy import may create reported claims without extraction. Claim content_sha hashes `[subject_id,kind,predicate,value,statement,rationale,valid_from,valid_to,valid_precision,valid_end_state]`; status/review changes belong to revisions and do not change this content identity.

Temporal validation requires valid_from null iff valid_precision=unknown. valid_end_state=closed requires non-null valid_to later than valid_from; open/unknown require null valid_to. Open is an explicit assertion that the interval remains effective, not an inference from an absent end date. A day-precision date uses YYYY-MM-DD and does not resolve an intraday as-of request. New model proposals default to unknown end-state unless the quoted source explicitly establishes continuation. Operator review may establish open or closed intervals.

### Speaker and user-stated evidence

The importer sets `speaker` from the adapter event role, never from model output. `user` is text the user typed. `pasted` is text inside a user message that the provider marks as pasted or attached. Where a provider does not mark pastes, text inside a fenced or quoted block, or a single user message longer than 1,500 characters, is also `pasted`. These heuristics are provisional; WP-00 records per provider whether pastes are marked (Q17). `assistant` covers final assistant statements. Legacy note sources are `unknown`.

A claim is `user_stated` when at least one `supports` evidence row cites a `user` span. It is derived at read time, not stored, and never changes status or evidence_level. It is a ranking and labeling signal only. An exact-slot conflict still marks every member disputed regardless of speaker. A model proposal cannot set speaker or user_stated.

### Repository-state claims

`repo_state` is a registered predicate class: `head_commit`, `release_tag`, `branch`, `test_count`, `deployed_version`, and `file_location`. Members are added by code/config review, never by a model. These predicates are single-valued for exact-slot conflict detection. The extraction prompt asks the model to use them for statements about the repository's present condition.

Freshness of a repo_state claim is computed at read time. If the project root's current HEAD, read with a 500 ms bounded read-only `git rev-parse HEAD`, differs from the citing source's `repo_head`, freshness is `repo_moved`. The bundle then states the recorded short commit and that the value must be verified live. A null `repo_head` or a failed read gives freshness `unknown`. Accepted status does not exempt a repo_state claim from this rule.

### Example claim bundle

```json
{
  "claim_id": "f15d35ad-9840-4eb9-92d7-32acfa80ccef",
  "project_id": "562ad87a-a717-47d1-9001-c9b3a6612b4e",
  "kind": "decision",
  "subject": {"kind": "component", "canonical_key": "capture"},
  "predicate": "storage_backend",
  "value": "sqlite",
  "statement": "Use SQLite for capture metadata.",
  "rationale": "The worker needs transactional cursor updates.",
  "status": "reported",
  "evidence_level": "reported",
  "valid_from": null,
  "valid_to": null,
  "recorded_at": "2026-10-21T08:00:00Z",
  "review_due_at": null,
  "source": {"kind": "legacy_note", "note_path": "0-Sessions/2026/10/synthetic.md"}
}
```

The example is a public response bundle, not a direct SQL row. Source and entity IDs are resolved by the service. A report of a decision is not evidence that the operator accepted it.

## Semantics and state transitions

| Input/action | Preconditions | Result |
|---|---|---|
| Model extract | Existing allowed source; valid quotes; project matches | reported claim, or review proposal; never accepted |
| Exact duplicate | Same project/subject/kind/predicate/value/statement/rationale and effective interval | Reuse claim, attach new evidence; repeated origin does not raise evidence_level |
| Similar wording | Different canonical content | Separate reported claim; optional proposed alias/duplicate review |
| Accept | Explicit operator approval reference, evidence present, expected revision matches | accepted, reviewed_at recorded; material decision authority changes only here |
| Verify evidence | Approved deterministic verifier result or approved documentary registration | evidence_level changes; does not independently accept a decision |
| Contradiction | Exact single-value conflict or reviewed semantic conflict | Conflict relations; members disputed; preserve previous snapshots |
| Supersede | Explicit operator approval for successor and transition, explicit predecessor, reason, same project, no cycle | Atomically accept successor and supersede predecessor; append supersedes relation |
| Retract | Explicit authorized correction and reason | retracted; excluded from current context; evidence retained |
| Quarantine | Invalid source, privacy violation, corrupt relation | quarantined; excluded from all normal context |
| Restore | Explicit review with fresh evidence and expected revision | new revision; never delete old revision |
| Delete generated page | Renderer-owned and matching expected hash | Rebuildable deletion only; no claim deletion |
| Privacy purge | Operator-directed separate procedure | Remove prohibited bytes from DB, exports, projections, index, backups; retain content-free tombstone |

Acceptance is not a flag a model may choose. The CLI accepts `--approval-ref` as an audit reference, not authentication. In this single-user local threat model, an agent able to run arbitrary shell commands under the user's identity can forge that reference or edit the database. Team instructions and tool permissions enforce operator authority. If adversarial local agents are in scope, stop and add a separate authenticated approval channel before activation. Content injection alone must never trigger an accept command. Agents may run an acceptance only when the user directs that specific acceptance in the current session.

Default registered single-valued predicates are `storage_backend`, `runtime_version`, `api_mode`, `deployment_target`, and the `repo_state` class. All others default to non-exclusive `statement` behavior. Predicate registration is code/config reviewed by Codex; a model cannot register a new exclusive predicate. Canonical values are JSON scalars or sorted string lists; no floating-point factual values in version 1. Overlap uses half-open intervals `[valid_from,valid_to)`. Null endpoints mean unknown unless valid_end_state=open explicitly establishes an ongoing interval. Unknown-time pairs produce a potential conflict requiring review, not an automatic winner.

`accepted` plus `verified` means the system has both an acceptance event and a verified evidence record. It is not an eternal guarantee. Version 1 permits verified status only through an operator-reviewed documentary source registered at import through the allowlisted mapping in 03. The artifact-based `verify` command below is deferred to WP-10. Arbitrary model text, command transcripts, and an unverified document cannot set verified. A passed test establishes only its named behavior at its recorded commit and environment. Most imported agent claims remain `reported`. Confidence probabilities are omitted from authoritative schema because no calibrated estimator exists.

Supersession is a single review operation, not an accept command followed by a second command. It accepts the designated successor even when it was disputed, closes the predecessor's interval only when an explicit effective boundary is supplied, and records the reason. If other unresolved competitors remain, the successor stays disputed and the result reports unresolved member IDs; it must not claim a resolved current decision. A plain accept action on one member of an unresolved conflict does not resolve that conflict. Retraction of a competing claim removes that competitor from current eligibility, but restoration of a previously disputed remaining claim requires explicit review. The relation history remains.

Version 1 does not merge nonidentical entities or claims. Exact duplicate imports attach evidence to the same claim as defined above. A proposed fuzzy merge returns review_required and no merge mutation. To correct an identity, explicitly retract the erroneous claim and create a new claim with derived_from evidence and a review reason. Do not rewrite old source identities or retroactively splice revision histories.

## Internal interfaces

Use Python dataclasses, enums, and functions, not a framework. All mutation functions receive an explicit store and project ID. No module global active project.

```text
import_capture(store, capture_identity, capture_fingerprint, sources) -> ImportResult
build_proposal_input(store, source_id, max_input_units) -> ProposalInput
validate_proposal(input, model_json) -> ValidatedProposal | ValidationError
apply_proposal(store, proposal_id, expected_project_revision, operation_id) -> ApplyResult
review_claim(store, ReviewRequest) -> ApplyResult
render_project(store, project_id, expected_revision) -> ProjectionResult
build_context(store, ContextRequest, qmd_client, tokenizer, clock) -> ContextResponse
backup_store(store, destination) -> BackupResult
```

`ApplyResult` contains operation_id, project_id, project_revision, created_claim_ids, changed_claim_ids, conflict_ids, and projection_pending. Applying the same operation_id and same request hash returns the stored result, with `replayed=true`. Reuse with different payload returns `idempotency_conflict` and writes nothing. A stale expected revision returns `revision_conflict` and writes nothing. Source records are immutable; a changed source creates a new ID and invalidates dependent evidence eligibility until reviewed.

```text
apply(request):
    validate shape, limits, allowed project and source references
    begin immediate
    if operation exists:
        compare request hash; return original result or conflict
    require project.revision == request.expected_revision
    recheck source hashes, privacy and all referenced claim revisions
    evaluate exact duplicate and contradiction rules
    reject forbidden transitions or supersession cycles
    write claims, evidence, relations and immutable revisions
    increment project revision exactly once
    write operation result and mark projection desired revision
    commit
```

## CLI contracts

Existing commands and JSON result types remain unchanged. New global `--vault` follows existing placement before the subcommand. New commands use JSON envelopes with `schema_version:"1.0"`, `status`, and `errors` by default when `--json` is supplied. Human output is generated from the same result object.

| Command | Behavior |
|---|---|
| `mem context TERMS --project NAME_OR_ID [--mode current|history] [--as-of UTC] [--budget N] [--semantic] [--json]` | Read-only package; default current, budget 2400; project may be inferred only from unambiguous cwd |
| `mem knowledge init --project P --mode shadow` | Create schema/map after explicit invocation; no capture replay |
| `mem knowledge import --project P --dry-run --json` | Count inputs and return plan fingerprint; no writes |
| `mem knowledge import --project P --apply PLAN_HASH --json` | Revalidate fingerprint, idempotently import legacy reported claims |
| `mem knowledge reconcile --project P [--limit 10] --json` | Import outstanding captures, extract/propose within policy; no automatic acceptance |
| `mem knowledge review CLAIM --action accept|retract|restore --expected-revision N --claim-revision N --operation-id UUID --approval-ref REF --reason TEXT --json` | Explicit authorized review operation; material acceptance remains operator-owned |
| `mem knowledge supersede OLD --with NEW --expected-revision N --old-revision N --new-revision N --operation-id UUID --approval-ref REF --reason TEXT [--effective-at UTC] --json` | Atomically approve successor and supersede predecessor; unresolved additional competitors stay explicit |
| `mem knowledge accept CLAIM --reason TEXT [--json]` | Operator short form. Reads current project and claim revisions, generates operation_id, records approval_ref `cli:operator`, then runs the same review operation. Refuses a member of an unresolved conflict and points to supersede. `supersede OLD --with NEW --reason TEXT` accepts the same short form |
| `mem knowledge pending --project P [--limit 10] --json` | Read-only review queue: unaccepted decision claims, user-stated first, then by distinct origin count and recency |
| `mem knowledge conflicts --project P --json` | Conflict groups with both sides and evidence |
| `mem knowledge status [--project P] --json` | Read-only counts, lag, mode, backup status |
| `mem knowledge doctor [--project P] --json` | Read-only integrity and drift checks |
| `mem knowledge repair --project P --json` | Retry only derived work; no external-edit overwrite |
| `mem knowledge backup --output PATH --json` | Consistent verified SQLite snapshot and metadata manifest |
| `mem knowledge export --project P --output DIR --json` | Portable versioned records and projections; obey privacy policy |
| `mem knowledge mode --project P --set off|shadow|active --expected-revision N --operation-id UUID --json` | Per-project switch; active requires gate artifact reference in config |

New exit codes are 0 success including empty results, 2 invalid request/scope, 3 revision or idempotency conflict, 4 unavailable/busy, 5 integrity/privacy violation, and 6 review required. Degraded context with usable results exits 0 and sets `degraded=true`. If the authoritative knowledge DB is unavailable, context returns status unavailable and exit 4; it must not silently substitute legacy snippets as verified claims. Legacy `mem search` remains a separate fallback command.

Errors have `{code,message,retryable,reference_id}`. Messages use fixed safe text and IDs, not raw exceptions or source bodies. `--json` writes exactly one JSON object to stdout, no progress chatter. Diagnostic counts may go to stderr. Schema initialization never happens inside hooks or context reads.

## Retrieval and ranking

1. Resolve project and privacy before lookup. Explicit `--project` wins. No unscoped search fallback. Archived projects require history mode. No automatic reference-project expansion.
2. Snapshot eligible records and project_revision in a short read transaction, then close it before any QMD call. In current mode omit superseded, retracted, quarantined. Keep reported/accepted/disputed but distinguish them. Reject missing/mismatched evidence hashes. For `--as-of`, inspect current stored history and explicit effective intervals, including superseded predecessors whose interval contains the requested time. This answers what the presently known record says was effective then, not what the database knew at that earlier time. Require a known valid_from and either a known valid_to after the time or an explicit still-open interval. Unknown-time claims are excluded with a warning. Date-only effective evidence cannot resolve intraday questions. Recheck project_revision after QMD; retry the read/package once on change, then return busy rather than mixing revisions.
3. Tokenize query using Unicode word extraction, casefold, and split `_`, `-`, `/`, and `.`. Keep identifiers including one/two-character tokens. Match exact subject/predicate/aliases first. Candidate cap is 50. Scan at most 5,000 eligible claims for local fallback, in claim_id order; report `partial=true` if exceeded.
4. QMD lexical top 50 and optional semantic top 50 are candidate discovery only. Impose 1,500 ms total backend budget, not 1,500 ms per attempt. Map paths only to this project's generated pages. Reject off-vault paths, unsupported collections, stale revision headers, and candidate-only pages not yet indexed. Do not return QMD snippets directly. Convert valid page IDs to current DB claim IDs.
5. Rank lexical matches by number of distinct matched query tokens descending, exact subject match first, then claim_id. Fuse local lexical, QMD lexical, and optional semantic rank lists with `sum(1/(60+rank))`, missing ranks contribute zero. Deduplicate IDs. Break fusion ties by claim_id. This formula is provisional and must be held fixed during the frozen evaluation.
6. Expand at most one hop and at most 10 neighbors from the top 10 seeds. Follow supersedes and contradicts for history/conflict context and depends_on for rationale. Do not follow derived_from to recursively regenerate evidence. Expansion never crosses projects.
7. Build indivisible bundles. Each contains statement, status, evidence_level, rationale when present, effective/recorded time, freshness, and one source citation. A conflict bundle contains all conflicting alternatives or an explicit omitted-alternatives count and status unresolved. Never include one side as accepted while hiding a known conflict. Superseded predecessors appear only in a labeled history subsection.
8. Pack relevant conflict warnings first, then accepted eligible bundles, then user-stated reported bundles, then other reported bundles, then `repo_moved` bundles, each group preserving fusion order. Default accepted allocation is a soft 60%, reported/history a soft 25%, provenance/warnings 15%. Unused allocation can transfer; the final hard budget controls. No claim is cut mid-statement or separated from its citation. Skip an oversized bundle and report omission.
9. Render and measure the entire package, including envelope and citations. Return only when under budget. No query-time model compression.

Budget is always stated in tokens: default 2,400, allowed range 1,024 to 8,000. If a caller supplies a supported tokenizer ID through `--tokenizer ID`, count exact tokens with that pinned tokenizer and include its version. Version 1's guaranteed offline fallback enforces the budget as 3 UTF-8 bytes per token, a conservative over-count for English text, and reports `budget_unit:"tokens_est_3b"`. It is an enforcement bound, not a measured token count. Evaluation reports actual tokens separately with a pinned tokenizer. Never claim that characters/4 or a byte count is a measured token count. Do not include raw source excerpts beyond the cited 240-code-point quote limit in context. Envelope metadata and warning text have fixed maximum lengths; if no bundle fits, return the compact empty envelope with omission counts. Invalid-budget requests return a fixed error object under 256 UTF-8 bytes rather than trying to honor an invalid budget.

### Context response example

```json
{
  "schema_version": "1.0",
  "status": "ok",
  "project_id": "562ad87a-a717-47d1-9001-c9b3a6612b4e",
  "project_revision": 7,
  "mode": "current",
  "as_of": null,
  "degraded": false,
  "partial": false,
  "budget": 2400,
  "budget_unit": "tokens_est_3b",
  "used": 810,
  "items": [],
  "warnings": ["No eligible supported claim matched."],
  "omitted": {"budget": 0, "scope": 0, "invalid_evidence": 0},
  "errors": []
}
```

`used` in this illustrative example is not a measured serialization size. Executable tests calculate it from the final canonical payload using a fixed-width counter field or a repeat-until-stable serialization loop. Each item schema is `{claim_ids,status,evidence_level,user_stated,statement,rationale,freshness,valid_from,valid_to,recorded_at,citations,relations}`. Citation schema is `{source_id,note_path,start,end,quote_sha,quote,source_kind}`. Relations carry IDs and type. No absolute source paths appear in the context response.

## Limits and retry policy

Proposal output is at most 32 KiB, 20 claims, 20 relationships, 8 new conceptual entities, and 4 evidence spans per claim. Statement maximum is 600 characters, rationale 1,000, display name 120, and alias count 10. A batch uses at most 12,000 input budget units and 2,000 output tokens where the model CLI supports a hard output limit; otherwise reject oversize output after receipt and record that spend is not hard-capped by the provider. Default one extraction attempt plus two retry attempts per source/version, with 60/300-second backoff. No retry for schema-invalid output without an explicit retry request.

Source import, extraction, and projection have separate job states. Apply is a short idempotent operation on a persisted ready proposal, not a fourth job type. Startup reconciles ready proposals without completed operations before scheduling new extraction. A running job lease expires after 240 seconds, exceeding the 180-second model timeout. A lease token fences late completion. Before commit, require the current lease token. Attempts increment before external calls. Budget exhaustion leaves pending work, not a false success. Per-run defaults are 10 captures, 2 model calls, and 300 seconds. A project marked local_only makes zero model calls across capture, remember, deferred reconciliation, and new extraction; imported claims remain reported.

Daily automatic backup runs at the start of the first mutation after 24 hours; if it fails, reject new knowledge mutations but allow capture and reads. Keep seven daily and four weekly backups only after verifying a newer backup; never rotate the last verified backup. Logs retain at most five 5-MiB files. Completed transient jobs may be compacted after 30 days, while operations, sources, revisions, and accepted history remain. A warning threshold of 100,000 claims or 1 GiB DB size is provisional; it stops automatic extraction for that project pending maintenance review, not capture.

## Verification request and configuration

Deferred to WP-10 in revision 1.1; not part of version 1. If built, add `mem knowledge verify --request FILE --json`. Its exact request is `{schema_version,operation_id,project_id,claim_id,expected_project_revision,expected_claim_revision,approval_ref,artifact_path,artifact_sha,verifier_id,subject_id,predicate,value,commit,environment,outcome,observed_at,reason}`. The artifact must be redacted, at most 32 KiB, and contained in the project's configured evidence root. verifier_id must be operator-allowlisted; outcome must equal pass. Subject/predicate/value must match the claim. Read/hash the artifact, register approved_document evidence, and update evidence_level and operation atomically. Missing approval, changed hash, scope mismatch, unsupported verifier, or stale revision rejects all writes. This establishes only the declared verification scope. Models cannot call this service.

New Config fields/defaults are `knowledge_mode="off"`, `knowledge_db="_meta/knowledge.sqlite"`, `knowledge_project_ids={}`, `project_privacy={}`, `knowledge_input_units=12000`, `knowledge_output_tokens=2000`, `knowledge_run_captures=10`, `knowledge_run_calls=2`, `knowledge_run_seconds=300`, `knowledge_qmd_timeout_ms=1500`, `knowledge_backup_dir="_meta/knowledge-backups"`, and, only with the deferred `verify` command, `knowledge_verified_artifact_roots={}`. Path defaults are vault-relative. External evidence roots require explicit registration and cannot include credentials/private journal. Known invalid values fail validation. Project mode/revision live only in the DB; global mode is a maximum, not duplicate project authority. Reload privacy before every model call/publication. Enrollment requires an explicit privacy choice.

ReviewRequest uses operation_id and expected_project_revision/expected_claim_revision supplied by CLI flags. SupersedeRequest includes both predecessor/successor expected claim revisions. ModeRequest includes operation_id and expected project revision. The same request after a lost response returns the prior result; the same ID with changed payload fails. Init is idempotent by project ID/name mapping. Import apply uses the plan hash as operation ID.

