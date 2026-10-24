# 12. Slim plan amendment

Revision 1.4, 2026-10-23. Approved by the user. From WP-04 onward this document takes precedence over [06](06-BENCHMARK-AND-TEST-PLAN.md) and [07](07-IMPLEMENTATION-WORK-PACKAGES.md) where they differ. It changes the depth of verification. It does not change scope, contracts or safety invariants.

## Unchanged and strict

- Privacy. No vault content in public artifacts. Every test is isolated from the real vault and the real search index. Every model call checks privacy first.
- No lost or overwritten notes. Capture identity, crash recovery and one crash-and-retry test per mutating step stay.
- The head-to-head test. The sealed synthetic set, the grader that cannot be gamed, the K0 no-review arm and real-vault set R all stay. G0, G1, G3 and the store portion of G7 stay invariant.
- Hooks stay byte-identical in protocol and behavior.

## Lightened

| Package | Change |
|---|---|
| WP-04 | Scope unchanged. Codex review covers ranking, budget and project isolation. |
| WP-05 | One timing pass of 10 runs per representative query instead of 5 warm-ups and 30 repetitions. Per-case paired win/loss/tie counts, no bootstrap intervals. One recorded real-model extraction run with its cost, instead of three. Set R unchanged. |
| WP-06 | Accept and supersede with the short review queue. Retract, quarantine and restore stay as WP-02 service calls; no new CLI for them. |
| WP-07 | Render project pages and `knowledge/index.md`, never write the overview, and skip a generated page the user edited (external_edit). Modes are off and shadow only. |
| WP-08 | One online backup and restore test plus the git-ignore check. The vault's local git snapshot covers broader recovery. |
| WP-09 | A few days of normal use with a short defect note instead of a formal 20-session pilot report. G8 becomes "no unresolved P1 defect after that period". |
| WP-10 | Dropped. |
| Reviews | Codex reviews WP-04 and any change that touches capture, privacy or authority. Other packages get a lighter check by an agent that did not write them. |

## WP-S1 approval rule

Approved by the user on 2026-10-22 and 2026-10-23. Items with proof apply without per-item review: a worktree confirmed by Git itself, a leftover-layout folder, and stale-line prefixing. The user approved the remaining name-similarity fragment merges as a batch on 2026-10-23. Re-attributions apply automatically only after the user accepts the 30-item calibration sample. Every apply starts with a local vault snapshot, never deletes a file, and never edits or moves a session note.

## Set R

The user chose four projects on 2026-10-23, one more than 06 requires. Their names are vault content and are recorded only under `.audit/knowledge/R/`.
