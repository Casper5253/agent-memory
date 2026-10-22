# Herdr execution handover

The receiving team can execute this document without the originating conversation. The user requested a specification, not application changes during blueprint creation. This handover governs a later implementation run after the user starts it.

## Team ownership

Claude Code leads coordination, integration, consequential decisions, and final live verification. OpenCode implements most routine work, including fixtures, data access, CLI adapters, renderers, migration tooling, and diagnostics. Codex owns architecture oversight, independent code review, poisoning/isolation/crash tests, and benchmark scrutiny. An implementation worker does not approve its own critical package.

Use the local `herdr-team` and `hx-router` skills and inspect their current contracts before sending briefs. Read `~/.agents/pstack/TEAM.md` and project instructions. Never assume agent names, pool headroom, model availability, or tool versions from this document. Discover the actual team, then assign bounded briefs with owned files and acceptance commands. This blueprint does not require a particular model version.

## Start sequence

1. Read 00, 01, 04, 06, and 09. Compare repository HEAD with `03764f8`. If code has changed, inspect the delta and refresh affected evidence before implementation.
2. Inspect `git status --short`. Preserve unrelated work. Create a task branch `feature/agentmem-knowledge` from the selected clean base; do not reset the user's tree.
3. Reproduce the baseline test command with an existing valid Python. Establish isolated test paths. Do not repair global PATH, Git trust, or shared credentials.
4. Discover Herdr with the normal authorized CLI. In the blueprint run it returned an elevated-server PermissionDenied error. Treat a repeated error as a setup blocker; do not restart the server, resend briefs blindly, or claim agents are idle from a failed read.
5. Register work packages in HX with dependencies and owned files. Keep operational task state in `.hx`; keep durable architecture in these documents and review artifacts. Do not assume `hx checkpoint --promote` exists.
6. Execute WP-00, then the dependency graph. Remain in shadow until all named activation gates pass.

## Allocation and review checkpoints

| Package | Implementer | Required independent review | Lead gate |
|---|---|---|---|
| WP-00 | OpenCode | Codex gold fixtures and grader anti-gaming | Freeze baseline and sealed evaluation |
| WP-01 | OpenCode with Codex consultation | Codex crash/identity review | Accept B1 separately |
| WP-02 | OpenCode | Codex schema, transaction, scope invariants | Freeze schema 1 |
| WP-03 | OpenCode | Codex privacy and untrusted-model boundary | Approve proposal contract |
| WP-04 | OpenCode | Codex ranking/budget/isolation | Demonstrate vertical slice |
| WP-05 | OpenCode collects; Codex verifies | Codex paired measurement review | Claude adopt/reject/inconclusive |
| WP-06 | OpenCode | Codex authority and temporal semantics | Accept review workflow |
| WP-07 | OpenCode | Codex migration/editor-race review | Review rendered artifacts and rollback |
| WP-08 | OpenCode | Codex independent restore and leak tests | Accept operational runbook |
| WP-09 | OpenCode prepares; Claude integrates | Codex release diff/pilot review | Claude live provider acceptance |
| WP-S1 | OpenCode tool; Claude runs with user | Codex merge-evidence rules | User approves plan and result |
| WP-S2 | OpenCode | Codex ordering and idempotency | User approves spend; Claude spot check |
| WP-11 | Claude drafts; OpenCode edits | Codex claims versus evidence | User approves website and paper text |

## Delegation boundaries

Each brief includes task ID, exact base commit, allowed files, contract sections, expected behavior, forbidden side effects, validation commands, and report destination. Implementers must report ambiguities rather than invent new schema fields, statuses, APIs, or dependencies. Do not delegate application implementation before WP-00 freezes the baseline.

Example brief, adapted to the live skill's syntax:

```text
[team from dev-claude] TASK: WP-02 structured store at <base-commit>.
FILES: agentmem/knowledge/models.py, store.py, policy.py, tests/test_knowledge_store.py; config.py by lead coordination.
CONTRACT: docs/llmwiki-blueprint/04-DATA-AND-INTERFACE-CONTRACTS.md.
ACCEPT: store tests and full unittest suite; operation replay and stale CAS assertions.
BOUNDARY: no hooks, install, vault, HX changes, model calls, or network.
REPORT: commit, changed files, commands/results, remaining risks to dev-claude.
```

Codex review briefs name the exact commit range and ask for ranked findings with concrete reproducers. Review the final code, not only the worker's summary. A passing fake-model test does not justify saying real extraction is accurate.

## Branches, commits, and artifacts

Use one commit per independently testable work package or a small dependency-ordered series when a package requires a preparatory regression test. For parallel WP-01/WP-02, use separate worktrees and branches. Claude integrates in dependency order with normal merges or cherry-picks after review; do not rewrite another agent's shared branch.

The AgentMem repository is public and the vault is local only. Never commit or push vault content: note text, session titles, project-note excerpts, project maps, or real-vault questions, answers, and outputs. Never add a remote to the vault. Keep machine-readable results in `.audit/knowledge/<task-id>/` locally and compact shareable reports in `docs/llmwiki-evaluation/`. Do not commit live source records, personal paths beyond approved documentation, raw transcripts, database files, credentials, or model outputs containing private data. Synthetic fixtures and public benchmark summaries can be committed.

Each task report contains base/head commits, owned-file list, actual test command and exit result, evidence path, unresolved findings, and next dependency. Each checkpoint records current task revision and exact next action so compaction does not require rediscovering architecture. New architecture decisions get an amendment to the relevant document plus a decision-log row, not only a chat message.

## Verification workflow

Run focused tests during implementation, then the full existing and new suite at the package boundary. Do not repeatedly rerun a full suite without a new change or unresolved risk. Codex independently exercises critical negative cases and at least one recovery path per persistence package.

Before integration, check `git diff --check`, inspect the changed-file boundary, verify synthetic fixture use, and read the report. Before activation, run the sealed evaluator and restore drill. Before final release, compare hook settings/plugin hashes and demonstrate fresh sessions for Claude, Codex, and OpenCode. Preserve observed provider evidence without leaking transcripts.

The user required consistent hooks. Changes to `hook.py` may only improve internal overview content under the existing contract; no new lifecycle event, matcher, timeout, hook JSON shape, or inline model call. If an implementation discovers that a hook contract must change, stop that slice and explain the necessity. Do not hide it under a project restructuring task.

## Escalation and stopping conditions

Escalate architectural contradictions, unknown source format compatibility, unrecoverable external edits, production privacy concerns, or adoption-gate failure to Claude. Claude resolves from evidence where possible and documents the decision. Ask the user only for actual product/authority choices, live deployment scope, private-data access, or changes to vault governance that the user has not already authorized.

Stop mutation immediately on evidence leakage, cross-project data return, silent lost writes, unknown schema, missing verified backup during activation, or altered hook integration bytes. Continue read-only diagnosis and preserve evidence. Stop the broader wiki rollout if WP-05 rejects the investment. Inconclusive is not approval.

No automatic push, force-push, global configuration rewrite, paid model run, or production deployment is implied by completing local code. Prepare the exact release artifact and validation evidence before requesting any final authorization that the current session does not already grant.

## Completion report

Claude's final report states what users can now do, the exact release commit, baseline/candidate results, hook compatibility evidence, pilot scope, backup/rollback location, and unresolved limitations. It must distinguish source tests, synthetic integration, real-provider observation, and measured retrieval improvement. A clean suite alone is insufficient.
