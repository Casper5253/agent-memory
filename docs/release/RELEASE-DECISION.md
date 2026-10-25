# Release decision

**Recommendation: READY WITH LIMITATIONS.**

- **Candidate.** `8da52cf`, plus two vault-hygiene additions (fragment retirement, bounded pending lists), published as `master` and installed in `~/.agents/memory/app`. The installed copy is identical to the repository.
- **Final gate.** The full test suite: 607 tests OK at `8da52cf`, and 618 OK after the additions (`73c4c53`).
- **Scope.** Local release. Nothing is pushed or deployed outside this machine.

## Architecture verdict: SOUND WITH DOCUMENTED LIMITATIONS

The final review (`.audit/release/ARCH-REVIEW.md`, local) found the single-status-page design coherent:

- There is one authority for current state.
- Capture, reconciliation and history are separated.
- The disabled knowledge store is on no write path.
- AgentMem has no dependency on HX or Herdr.

The review rated the reviewed revision UNSOUND because of three proven implementation defects. All three are fixed:

| Finding | Severity | Defect | Fix |
|---|---|---|---|
| F1 | P1 | A retained item could move across sections and then be retired | c21fe73 |
| F2 | P1 | Continuation parts aliased the first part's evidence anchors | 3b8f005 |
| F3 | P1 | Unverifiable search excerpts kept the CURRENT label | 5a4c419, ec7db4b, b7e3bc8 |

Two P2 findings (F4, F5) are fixed. F6 is documented as a limitation. No architectural defect remains.

## Functional verdict: all ten criteria pass

The details are in `ACCEPTANCE-EVIDENCE.md`. All of C1-C10 pass on live evidence or review. C2 passes with one limitation, listed below.

The live lifecycle was demonstrated in actual operation:

1. A fresh session starts with the project's status.
2. A two-turn session with a decision is captured as one note.
3. The decision reaches Key decisions with its citation.
4. A later session's decision supersedes it, and the old one moves to History.md with its reason.
5. A new session can say what changed and what comes next.
6. Search labels page content CURRENT and older content HISTORICAL.

This was shown for Claude Code on Agent Memory, and for Codex on Herdr Orchestration.

## Defects found and fixed during validation

Live runs found defects that the unit suite did not. Each was fixed with a test that fails on the old code, then rechecked live:

| Defect | Severity | Fix |
|---|---|---|
| Short turns judged one increment at a time, so the session was never captured | P1 | fef09cc |
| Startup context showed no items when not every first item fit | P1 | b30e0fb |
| Search mislabelled truncated or stale-line excerpts | P1 | ec7db4b, b7e3bc8 |
| The model could omit a cited decision | P1 | 0a310f5 |
| One rejected item blocked a whole valid update | P2 | 0a310f5 |
| Agent process items reached status pages | P1 | c21fe73 |
| Background workers flashed console windows | P3, user-visible | 2d36a09, 8da52cf |

## Known limitations (nonblocking)

| Limitation | Severity | Workaround |
|---|---|---|
| OpenCode is not verified live in this release. Its quota was spent. Support rests on the shared pipeline, the installed plugin and tests. | P2 | Run one OpenCode session and check `mem sessions --agent opencode` |
| A session killed before any Stop event is recovered only when a later hook or worker run discovers the settled transcript (review F6) | P2 | Any later agent session, or `mem worker` |
| Startup omits status items longer than 280 characters. The page pointer says how many more items there are. | P2 | Read the project page |
| No command releases a retry hold, so held captures cannot be requeued without editing the manifest | P2 | Leave held, as designed; they stay visible in health |
| 112 pre-citation captures stay held; they cannot update status pages | P2 | Their session notes remain searchable as HISTORICAL |
| Some migrated status items are old legacy content until newer sessions replace them | P3 | Normal use refreshes them |
| Older continuation notes from before F2 keep their aliased evidence references | P3 | Their session notes remain the source |
| The `agent-memory-wp02` worktree holds access-denied temp directories and needs a manual cleanup | P3 | None needed for operation |
| Workers started before 2d36a09 can still flash windows until they exit | P3 | Ends on its own; or stop the stale `pythonw` worker |

## Deferred improvements

- A `mem` command to release retry holds.
- Re-summarizing pre-citation captures from their transcripts as new derived evidence.
- A live OpenCode check.

Each of these should be done only when normal use shows a need. This release does not schedule them.

## LLMWiki

The knowledge store stays disabled (`knowledge_mode: "off"`) and is not resumed. The status page now enforces its goals: evidence-linked claims, decision succession and stale-fact handling. On questions built from real repository history, today's search is level with the store or ahead of it. The store also failed its own safety gate by showing superseded facts as current. The full argument is in `docs/llmwiki-evaluation/WHY-NOT-LLMWIKI.md`. The code stays in place, off, until a normal-use problem motivates it.

## Evidence supporting readiness

- Live reports, all local:
  - `agent-memory-rel-live/.audit/release/live/report.md`
  - `.audit/release/live-r2/report.md`
  - `.audit/release/live-r3/report.md`
  - `.audit/release/live-G/report.md`
- The capture log, `_meta/logs/capture.jsonl`.
- Regression tests added in this phase: `tests/test_rel_housekeeping.py`, `test_release_f3_f5.py`, `test_release_deferred_capture.py`, `test_release_decisions.py`, and the F2 and search tests.
- Full suite at `8da52cf`: 607 tests OK; at `73c4c53`: 618 tests OK.

## Vault hygiene completed after the decision

- The 10 fragment projects were retired with `tools/retire_fragments.py`. Every item was copied verbatim into its parent's History.md, and each folder was archived whole in `4-Archive/merged-fragments/`.
- Pending lists on status pages are capped at 10 lines. Overflow moves verbatim into History.md (`tools/cap_pending.py` for existing pages). Project pages went from 844 KB to 90 KB in total.
- Items created by the live acceptance runs were moved from the status pages to History.md with the reason `live-acceptance-test`.

## After this release

Work on AgentMem stops here. Future changes should come from problems met in normal use. The owner approved drafting an update to the portfolio AgentMem page. That is separate from this release and will not be deployed without the owner's approval.
