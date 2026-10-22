# AgentMem knowledge blueprint

Specification revision 1.2, 2026-10-21. Revision 1.1 applies the cross-family review in [11](11-CROSS-FAMILY-REVIEW.md). Revision 1.2 adds the vault sweep, the side-by-side comparison with the current setup, and the local-only vault rule. Revision 1.3 makes project attribution content-based: what a session worked on decides its project, and the folder it ran from is only the fallback. Specification only. No application implementation, installation, vault mutation, hook update, or deployment is authorized by this package.

## Decision

Recommend direction B, selected LLMWiki capabilities built on AgentMem. Retain capture adapters, hook commands, queueing, immutable session notes, project routing, PARA paths, managed markers, and QMD. Add a project-scoped knowledge package with evidence-linked claims, explicit decision succession, deterministic context packaging, and recoverable Markdown projections.

The initial deliverable is a shadow vertical slice. It extracts candidates from synthetic or explicitly approved sources and answers a project-scoped context request. It cannot replace current project state until the evaluation gates pass. Adoption is conditional on demonstrated improvement, not on completing the implementation checklist.

Version 1 is deliberately narrow. It never writes the existing project overview; legacy reconciliation keeps that block. It must show value without operator review: a no-review candidate arm and a small real-vault question set decide adoption. It targets the staleness failure observed in the live vault by recording the repository commit on each source and labeling repository-state claims stale once the repository moves. See [11](11-CROSS-FAMILY-REVIEW.md).

The plan keeps the best of both systems. The current setup stays in charge of everything the new one has not measurably beaten, and the adoption decision is made per type of question, not all-or-nothing ([02](02-RESEARCH-AND-ALTERNATIVES.md), "Current versus proposed"). The existing vault is cleaned in two passes. A hygiene sweep before the new system fixes project boundaries and leftover folders, which helps the current setup immediately. A knowledge backfill after adoption runs the new extraction over past sessions (07, WP-S1 and WP-S2). The release ends with WP-11, which updates every description of AgentMem: its README and skills, the vault rules, HX documentation, and the portfolio website and paper where they describe it.

The vault is local only. It has no remote and refuses pushes. This blueprint lives in a public repository, so it and any committed evaluation material must never contain vault content: no note text, session titles, project-note excerpts, or real-vault questions and answers.

Direction A, reliability and retrieval improvements alone, is the fallback if the slice fails. These improvements are prerequisites and remain useful independently. Direction C, replacing the system with a general LLM-written wiki, is rejected for this release. It duplicates working capture and retrieval, broadens write authority, and lacks local evidence of superior value. See [research and alternatives](02-RESEARCH-AND-ALTERNATIVES.md).

## What remains stable

The user's requirement that hooks remain consistent is binding. Preserve `run.py hook claude`, `run.py hook codex`, `enqueue`, and `worker` invocation contracts. Preserve installed event names, matching, output JSON, recursion guard, detached execution, settle policy, and SessionStart's 1,500-character cap. Do not add model calls to hooks. No hook configuration regeneration is needed for the core work.

The Python project gains `agentmem/knowledge/`. Existing CLI commands retain their arguments and output shapes. New commands live under `mem knowledge` and `mem context`. The vault gains generated pages below each project's `knowledge/` directory. Existing project overview and session paths do not move. A different top-level vault taxonomy is not required to gain wiki relationships.

## Evidence and confidence

Use these labels throughout the package. VERIFIED means inspected source or an observed execution, with the evidence identified. DOCUMENTED means a document asserts it but execution was not established. INFERRED means a consequence reasoned from inspected code. PROPOSED means a binding target requirement, not existing functionality. UNKNOWN identifies a bounded investigation the implementation team must complete.

Verified baseline checkout is AgentMem `03764f8a61e55514c9c50ea82538a898bfae70f2`. The 30 existing tests passed on 2026-10-21 with synthetic fixtures and fake models. No retrieval quality, real-model accuracy, deployed-hook latency, or cost benchmark was measured. HX checkout is `367ab17c129b58aa5310b91da47265ea42f4b4bf`. See [current state](01-CURRENT-STATE.md) and [verification record](10-EVIDENCE-AND-REVIEW.md).

## Reasons for the decision

AgentMem already compiles session summaries into project state. Its main missing contract is the distinction between a statement reported by an agent and a fact established by evidence. Folder-based CURRENT labeling does not establish truth. Current consolidation validates headings and length, not claim provenance or valid decision succession. The proposed claim store makes this distinction inspectable and testable.

Structured relationships are useful for decision rationale, supersession, and source navigation. They do not justify a graph database. SQLite tables and folder-qualified Obsidian links are sufficient for the bounded relationships specified here. Semantic retrieval remains optional. No embedding service, server, scheduled autonomous rewriting, or automatic cross-project entity merge is introduced.

## Package map and authority

| Document | Purpose |
|---|---|
| [01](01-CURRENT-STATE.md) | Source inventory, lifecycle, integration facts, limitations |
| [02](02-RESEARCH-AND-ALTERNATIVES.md) | Primary-source research and rejected alternatives |
| [03](03-TARGET-ARCHITECTURE.md) | Components, lifecycle, persistence, migration |
| [04](04-DATA-AND-INTERFACE-CONTRACTS.md) | Normative schema, state transitions, API and ranking |
| [05](05-RISKS-AND-FAILURE-MODES.md) | Adversarial risks and required mitigation tests |
| [06](06-BENCHMARK-AND-TEST-PLAN.md) | Fixtures, measurements, gates, stop criteria |
| [07](07-IMPLEMENTATION-WORK-PACKAGES.md) | Dependency-ordered executable work briefs |
| [08](08-HERDR-EXECUTION-HANDOVER.md) | Team execution, ownership, review, deployment |
| [09](09-OPEN-QUESTIONS-AND-ASSUMPTIONS.md) | Unknowns, defaults, resolution owners |
| [10](10-EVIDENCE-AND-REVIEW.md) | Inspection and independent review record |
| [11](11-CROSS-FAMILY-REVIEW.md) | Cross-family review findings and revision 1.1 changes |

When documents disagree, 04 owns machine contracts, 06 owns evaluation thresholds, 07 owns task order, and 08 owns team authority. Stop the affected task and repair a disagreement before implementing it. Do not choose whichever wording is easier.

## Investment and stopping rule

Mandatory work is WP-00 through WP-09. WP-00 through WP-05 produce the first value decision. WP-06 through WP-09 are gated on that decision. Optional work WP-10 is not part of release acceptance. No effort estimate is represented as measured. Claude must estimate after WP-00 using actual fixture and code complexity.

Ship only if safety gates pass, the paired evaluation shows the specified quality benefit without material regression, and the measured operating budget is acceptable. If structure adds tokens, manual review burden, or maintenance work without sufficient retrieval gain, retain direction A and stop the wiki rollout.

Two principles informed these choices. Laziness Protocol keeps QMD and the existing capture path instead of adding another retrieval stack. Make Operations Idempotent requires persisted proposals, compare-and-swap revisions, and replayable projection work instead of repeating model calls after partial failure.
