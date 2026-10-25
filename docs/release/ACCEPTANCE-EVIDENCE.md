# Acceptance evidence

This document maps every criterion in `PRODUCT-CONTRACT.md` to its evidence. It covers the release candidate named in `RELEASE-DECISION.md`.

Each row separates two kinds of evidence:

- **Automated.** Tests in `tests/`, run on synthetic fixtures.
- **Live.** Observed on the installed app, with the real vault and real Claude Code and Codex sessions, on 2026-10-24 and 2026-10-25.

Live evidence files are local (`.audit/release/...`) and never committed, because they contain vault text. This document cites them by path and session id only.

## Live runs

| Run | Candidate | Scenarios | Report |
|---|---|---|---|
| Round 1 | b30e0fb | A-G | `agent-memory-rel-live/.audit/release/live/report.md` |
| Recovery | ec7db4b | missed captures from round 1 | `_meta/logs/capture.jsonl` lines 2528-2529 |
| Round 2 | ec7db4b | B-E | `.audit/release/live-r2/report.md` |
| Live G | 0a310f5 (retry) | G: fresh two-turn Codex session | `.audit/release/live-G/report.md`; capture.jsonl from line 2601 |
| Round 3 | 2d36a09 | C6, C8, C2/C3 | `.audit/release/live-r3/report.md` |

## Matrix

| # | Criterion | Automated evidence | Live evidence | Result |
|---|---|---|---|---|
| C1 | Recognize the project, including worktrees | `tests/test_p1a_*`, project attribution tests; worktree commondir routing | Sessions in `agent-memory-rel-live` (a linked worktree) attributed to Agent Memory. Sessions in `herdr-orchestration-rel-live` attributed to Herdr Orchestration. All 11 former fragment roots resolve to their parent after the project-map update (probe output in this release's run log). | **PASS** |
| C2 | Startup context: every populated section, within the cap, nothing cut silently | `tests/test_p2_startup.py`, `tests/test_p6_fit_and_hold.py`, `tests/test_release_f3_f5.py` (F4: one item per section at the cap) | Round 1 A: Agent Memory context had all 5 populated sections in 3,928 characters. Omitted items are reported with an "N more items in <page>" pointer. Round 1 G: Herdr Orchestration showed 3 of 4 populated sections; "Latest progress" items exceed the 280-character startup limit (P2, see limitations). | **PASS with limitation** |
| C3 | A fresh agent resumes from the injected context alone | — | Round 1 A (`65483cea`): from the context alone, the agent correctly stated current work, open items and the next step. Round 2 D (`e287c73a`, tools off): named the decision that replaced the earlier one, the unfinished items and a next step. One listed item was stale page content, not a delivery fault. | **PASS** |
| C4 | Capture meaningful sessions: normal end, kill, no end | `tests/test_release_deferred_capture.py` (two short turns judged together, Claude and Codex; fails on the old code), `tests/test_p1b_lifecycle.py`, `tests/test_p1a_orphan_recovery.py` | Round 2 B (`7d0f7613`): a two-turn Claude session was captured as one note. Round 1 F (`20a37061`): a session killed before SessionEnd was captured after the settle window and a later worker run. Round 1 missed two two-turn sessions; that root cause was fixed in fef09cc, and both sessions were recovered live (`13a62e17`, `01a16d2b`). Live G (`01a170a7`): a fresh two-turn Codex session was captured to Herdr Orchestration. Round 3 (`S3`): a capture that read a transcript still being written was deferred as trivial, then captured whole on the next run. | **PASS** |
| C5 | No redundant notes | `tests/test_p1a_session_notes.py`, continuation tests | Round 2 B: one session gave exactly one new session note (vault status diff). Round 1 F: the killed session gave one row and one note. Repeat worker runs changed no note (round 1 F hashes). | **PASS** |
| C6 | One authoritative status, cited, failures visible | `tests/test_p6_noop.py`, `tests/test_p2_status.py`, `tests/test_rel_housekeeping.py`, `tests/test_p6_order.py` | Items on the Agent Memory and Herdr Orchestration pages cite `[[note#eN]]` anchors. Rejections, holds and deferrals are logged (`status-item`, `pending-retry-held`, `capture-deferred`). Round 2 found that a model could omit a cited decision (S1b) and that one bad item blocked a whole update (S2). Both are fixed in 0a310f5 (`tests/test_release_decisions.py`, which fails on the old code). Round 3: decision D3 is under Key decisions, citing S3's `#e11`. Live G: the Codex session that one rejected decision had blocked was retried on 0a310f5, and its decision and items reached the Herdr Orchestration page with citations. | **PASS** |
| C7 | Superseded items leave the page with their reason; compatible, newer evidence only | `tests/test_p6_supersede.py` (49 section pairs), `tests/test_p6_order.py`, F1 move test | Round 2 C: the round 1 decision (cited to `13a62e17#e8`) was superseded by S2's decision (`0e330dde`) and recorded in History.md as `[decisions; superseded]` with S2's evidence link. An older next step was superseded by a newer next step the same way. | **PASS** |
| C8 | Older knowledge retrievable, labelled correctly | `tests/test_knowledge_capture_recovery.py`, `tests/test_release_f3_f5.py`, search label tests | Round 2 E: the earlier decision was found and labelled HISTORICAL, in session notes and History.md. The status-page hit was labelled UNVERIFIED, because QMD truncates snippets with "..."; this is fixed in b7e3bc8. Round 3: status-page hits are labelled CURRENT and session hits HISTORICAL. | **PASS** |
| C9 | Autonomous and reliable: idempotent, recoverable, failures in health | journal recovery tests (`test_p2_*`), `tests/test_p6_retry_quarantine.py`, `tests/test_release_f3_f5.py` (F5 retrying in health) | Round 1 F: two extra worker runs and two consolidations changed no project or session file (hash comparison). The startup health line showed pending, held and failed counts in every captured context. No manual step was needed for capture. Recovery of the two missed sessions was a one-time manual requeue after the fix. | **PASS** |
| C10 | Understandable and maintainable | Architecture review `.audit/release/ARCH-REVIEW.md`: design coherent, knowledge store on no write path | `SYSTEM-ARCHITECTURE.md` written against the code. The knowledge store is disabled (`knowledge_mode: "off"`). | **PASS** |

## Integration coverage

| Agent | Live | Automated |
|---|---|---|
| Claude Code | Demonstrated: startup context, capture, reconciliation, kill recovery, fresh-session continuity | yes |
| Codex | Demonstrated: startup routing, two-turn capture and reconciliation with citations on Herdr Orchestration (`01a170a7`; also `01a16d2b`, recovered after the fix) | yes |
| OpenCode | **NOT VERIFIED live** in this release. Its monthly quota was nearly spent. Support rests on the shared pipeline, the installed plugin and automated tests. | yes |

## Test suite

The full suite runs once as the final gate (`python -m unittest discover -s tests`).

| Commit | Result | Note |
|---|---|---|
| 3b8f005 | 596 tests, 2 failures | Both from F3/F4. Fixed in b30e0fb; the affected 50 startup and search tests pass. |
| fef09cc | 599 tests OK | After the capture-path fix |
| 0a310f5 | 607 tests OK | After pass 2 |
| 8da52cf | 607 tests OK | Final candidate: adds window-free child processes (2d36a09, 8da52cf) |
