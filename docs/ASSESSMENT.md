# Agent memory — assessment and plan (2026-10-14)

> Sessions are evidence. Project notes are current knowledge. Search is the bridge between them.

## 1. What exists today

| Item | Finding |
|---|---|
| Vault | The Obsidian vault (`OBSIDIAN_VAULT_PATH` set). Git repo with uncommitted changes. Old numbered layout. |
| Session notes | Codex/Claude session notes spread over many `* Sessions/` folders, several under auto-created project folders named after prompt text. |
| Raw spool | `.obsidian-sync/` = 356 MB of raw transcript byte ranges **inside the vault** (gitignored, but readable by any vault tool or search index). |
| Capture | `~/.codex/obsidian-session-sync` (833 lines Python): Codex-only Stop/PreCompact/SessionEnd/SessionStart hooks, deterministic extraction, no LLM, no retrieval beyond SessionStart injection. |
| Governance | Vault `AGENTS.md` names a retired "Hermes" curator and a stale path (`Documents\Casper\...`). |
| Tooling | Python 3.11 (SQLite 3.45, FTS5 ok), Node 22, uv; no Bun. QMD not installed (`@tobilu/qmd` 2.8.3). |

## 2. Architecture

One pipeline, one vault, one retrieval interface; thin per-agent adapters.

```
 Claude Code ── SessionEnd/Stop hook ─┐
 Codex ──────── Stop/SessionEnd hook ─┼─► enqueue (tiny, <1s, never fails the agent)
 OpenCode ───── plugin session.idle ──┘           │
                                                  ▼
                       worker (single-flight lock, detached, exits when queue empty)
   adapter(agent) → normalized events → redact → extract + quality gate → dedupe (agent:session_id, byte offset)
        → [settled?] summarize (Haiku, isolated) ─fallback→ deterministic summary
        → write immutable 0-Sessions note (atomic, create-once)
        → consolidate into 1-Projects/<P>/<P>.md (reconcile, not append) ─fallback→ "Pending reconciliation"
        → manifest (SQLite) + qmd update
                                                  │
 Retrieval: `mem` CLI + `memory` skill (all agents) + QMD skill/MCP ◄─ QMD index (BM25 + optional vectors)
```

### Directory layout

```
FDE Knowledge/                     (vault)
  AGENTS.md  CLAUDE.md             root rules: naming, update, retention, retrieval (CLAUDE.md imports AGENTS.md)
  Home.md
  0-Sessions/YYYY/MM/              immutable session notes: YYYY-MM-DD-HHMM-<agent>-<slug>-<id8>.md
  1-Projects/<Project>/<Project>.md  current state (mutable, reconciled) + decisions/ (ADRs)
  2-Areas/                         Career, AI, Engineering, Data Science, Reviews
  3-Resources/                     Concepts, Patterns, Technologies, Models, Lessons, Attachments
  4-Archive/                       inactive projects, legacy handoffs
  _meta/                           manifest.sqlite (gitignored), logs/, queue/, locks/, PAUSE, templates/, schema docs

Documents/Projects/agent-memory/   code repo (package `agentmem`, tests, installer)
~/.agents/memory/                  deployed runtime (installer copies the package here; hooks call it)
~/.agents/pstack/local/memory/     shared `memory` skill (synced to all three agents)
```

Writes by unattended capture are limited to the vault (`0-Sessions`, `1-Projects`, `_meta`). No raw transcripts in the vault: the transcript reference is a path + offset, never a copy.

## 3. Agent compatibility matrix

| | Claude Code 2.1.299 | Codex 0.162 | OpenCode 1.18.27 |
|---|---|---|---|
| Transcript | `~/.claude/projects/<slug>/<id>.jsonl` (user/assistant, tool_use/tool_result blocks) | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` (session_meta, response_item, event_msg) | SQLite `opencode.db`; read via `opencode export <id> --pure` |
| Session end | `SessionEnd` hook ✔ | `SessionEnd` hook ✔ (+ `Stop` per turn) | none; `session.idle` per turn via plugin |
| Checkpoint | `Stop`, `PreCompact` | `Stop`, `PreCompact` | `session.idle`, `session.compacted` |
| Start-of-session recall | `SessionStart` hook (small pointer, ≤1.5k chars) | `SessionStart` hook | plugin `session.created` cannot inject; skill + AGENTS.md rule instead |
| Adapter | hook JSON gives `transcript_path` | hook JSON gives `transcript_path` | plugin passes `sessionID`; worker exports |
| Recursion guard | summarizer runs `claude -p --setting-sources "" --no-session-persistence --tools ""` (no user hooks, no transcript) + `AGENTMEM_CAPTURE=1` env | env guard in hook | `--pure` export + env guard |
| Herdr | unchanged (pane state hooks untouched) | `herdr-agent-state.ps1` hook untouched | `herdr-agent-state.js` plugin untouched; ours is a separate file |

"Settled" rule: a session is summarized only after `SessionEnd`, or after 20 min with no new transcript bytes (covers OpenCode and crashed sessions). Per-turn triggers only advance deterministic capture state.

## 4. Implementation plan and Herdr assignment

> **As executed:** Casper chose to have Claude build this solo (no Herdr distribution). The stages below ran in order inside one Claude session. Codex's review/validation role was replaced by the automated acceptance suite plus live runs against real Claude, Codex and OpenCode transcripts. An independent Codex review (`interrogate`) is still a recommended follow-up.

| Stage | Owner | Deliverable | Gate |
|---|---|---|---|
| 0 | dev-claude | backups; repo; interface spec (`docs/SPEC.md`: normalized event schema, manifest schema, note template, CLI contract); vault reorganization (migration script, dry-run, apply) | dry-run reviewed by Codex |
| 1 | OpenCode | core: atomic write, file lock, manifest, redaction, 3 adapters, extractor + quality gate, session-note renderer, unit tests | Codex review |
| 2 | Codex | acceptance suite for the 12 required scenarios (fixtures, no network) | runs red→green as stages land |
| 3 | OpenCode | queue + worker, summarizer (isolated Haiku) + fallback, consolidator, `mem` CLI, QMD wrapper | Codex review + suite |
| 4 | dev-claude | install: hooks (Claude, Codex), OpenCode plugin, `memory` skill via pstack sync, QMD install + collections + contexts; retire old Codex obsidian hooks (archive) | live capture of one real session per agent |
| 5 | Codex | end-to-end validation across agents; adversarial review (`interrogate`) | report |

Token balance: Claude ≈ spec, reorg, install, integration; OpenCode ≈ most code; Codex ≈ tests and review.

## 5. Model usage and cost

Deterministic work (parsing, redaction, dedupe, gating, notes, manifest, indexing) costs no model calls.

| Call | When | Input | Output |
|---|---|---|---|
| Summarize | once per settled, substantial session | digest capped at ~12k tokens (never the raw transcript) | ~1.5k |
| Consolidate | once per session that touched a known project | project note ≤ 3k + new decisions ≤ 1k | ~2k |

At ~10 substantial sessions/day: ≈ 170k input + 35k output Haiku tokens/day, on the Claude subscription. Trivial sessions (below the gate) cost nothing. Retrieval is local: QMD BM25 is free; vectors use a ~300 MB local embedding model; reranking/query expansion (~1.7 GB more) is optional.

## 6. Risks and trade-offs

| Risk | Mitigation |
|---|---|
| Secrets reach the vault | redaction before any write or model call; no raw transcripts in vault; test 10; move the 356 MB raw spool out of the vault |
| Summaries hallucinate | session notes keep deterministic facts (files, commands) separate from model prose; transcript reference on every note; consolidation cites session links |
| Reorg breaks links | migration rewrites folder-qualified wikilinks; vault committed + zipped before; dry-run first |
| Hook slows an agent | hooks only enqueue; worker is detached; hook timeout 5 s; failures logged, never surfaced as agent errors |
| Concurrent agents | per-session lock + manifest transactions; single-flight worker lock |
| Codex hook trust | Codex skips changed hooks until re-trusted: user approves once |
| QMD on Windows/CPU | BM25 path needs no models; `mem search` falls back to manifest + bounded scan if QMD is down |
| LLM unavailable | deterministic note still written; consolidation deferred to "Pending reconciliation"; retried later (test 11) |
