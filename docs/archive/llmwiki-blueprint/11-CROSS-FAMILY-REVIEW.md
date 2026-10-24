# Cross-family review

Specification revision 1.1, 2026-10-21. Reviewer: Claude (Opus 5.5), from a different model family than the revision 1.0 author and its independent reviewer. Inputs: the original mission brief, all of revision 1.0, AgentMem source at `03764f8`, and live vault structure (file counts, ignore rules, remote metadata, and one project note). No application code, vault content, hook, or runtime changed.

## Verdict

Direction B stands. Revision 1.0 is careful, internally consistent, and accurate where checked. It is over-scoped for a single-user local tool, and it aims at the wrong problem in two places. Its value case depends on operator review that its workflow makes unlikely. Its adoption evaluation would therefore measure the benefit of that review rather than the system. It also has no mechanism for the staleness failure that the live vault shows today.

Revision 1.1 narrows version 1, adds three mechanisms aimed at observed failures, and changes the adoption evaluation. The changes are listed per document below. The X-numbered findings are additive to R01-R28.

## Spot-check of the current-state inventory

VERIFIED against `03764f8`: `pipeline.py:49-52` recovers notes by the last eight alphanumeric ID characters without the agent; `pipeline.py:170-179` re-reads deferred transcripts from `start_offset` with no end bound; `search.py:71` labels results by folder; `hook.py:12` caps context at 1,500; `install.py:85,102` sets 15-second hook timeouts. 01 is accurate on every point checked.

Omissions from 01, VERIFIED on the live vault:

- The vault was a Git repository with a private remote, to which only an early skeleton had ever been pushed. On 2026-10-21 the user confirmed the vault must stay local. The remote was removed and a pre-push hook now refuses pushes. The vault's ignore rules still would not cover any artifact this blueprint adds.
- Scale after one week of capture (2026-10-14 to 2026-10-21): 191 session notes and 27 project folders, about 27 notes per day. No claim counts exist yet.
- The overview staleness failure is observable now. On 2026-10-21 one project's overview state block reported a commit and a release tag as current. The repository was 22 commits past that commit and had a newer release tag. The stale lines were model-written repository state that consolidation never retired.

## Findings

| ID / severity | Finding | Evidence | Disposition in 1.1 |
|---|---|---|---|
| X1 critical | Review starvation. Authority comes only from operator acceptance, and the specified review command needs a UUID, two revision numbers, and an approval reference per claim. With one operator and about 27 captures per day, nearly every claim stays `reported`. The canonical fixtures T02-T04 pre-seed accepted decisions, so G2 would credit K with curation that will not exist in practice. | 04 CLI table; 06 T02-T04; Q14 | Add a no-review arm K0 that must pass G2 on its own. Report reviewed-arm gains separately, with measured review minutes. Add a short-form review command and a ranked review queue (04, 06). |
| X2 high | No speaker distinction. A decision the user typed and a suggestion the assistant made are both `reported` with equal weight. The user's own words are the cheapest strong signal available, and the design discards it. Pasted text inside a user message is not the user speaking. | 04 sources schema has origin_group but no speaker | Add `speaker` to sources. Derive `user_stated` at read time and rank it within reported bundles. Pasted content never counts as user speech (04, 05 R30). |
| X3 high | Volatile repository state is the observed staleness failure, and nothing targets it. Commit IDs, tags, branches, and test counts are cheap to read live, go stale within hours, and are what the model put into "current state". K would extract them as claims that go stale the same way. | One project overview versus `git rev-list --count <recorded>..HEAD` = 22 | Record the repository HEAD on each source. Register a `repo_state` predicate class whose freshness becomes `repo_moved` when the project HEAD differs. Context labels these as needing live verification. Stamp the HEAD on new session notes in direction A as well (04, 07 WP-01). |
| X4 high | Taking over the overview state block drives most of the complexity: writer fencing, `mode_transitions`, a native `ReplaceFileW` wrapper with partial-error recovery, old-worker drains, and three-way restore. None of that is needed to test whether structured context helps. | 03 writer-fenced protocol; 04 mode_transitions; WP-07 | Version 1 never writes the existing overview. Legacy reconciliation keeps sole ownership. Generated pages live only under `knowledge/`. The takeover protocol is retained, but marked deferred to WP-10 behind a measured need (03, 04, 07). |
| X5 high | Git was an unaddressed egress path. The knowledge DB, backups, capture receipts, and shadow previews would all be tracked by the vault repository, which then had a remote. | Vault `.gitignore`; remote present on 2026-10-21 | Require ignore entries for all new `_meta` artifacts. `doctor` fails if any is tracked and warns if the vault gains a remote. The user removed the remote; the vault is local only (03, 05 R29, 07, 09 Q16) |
| X6 high | The adoption evidence is synthetic only. The fixtures were designed alongside K, so a synthetic win shows conformance to the specification, not usefulness (brief goal 8). | 06 fixture plan | Add a small real-vault question set R, written before K runs and graded against repository truth, kept local. Adoption requires G2 on R as well as on the sealed synthetic set (06, 07 WP-00, WP-05). |
| X7 medium | The budget unit is ambiguous. Default budget 2,400 means tokens with a tokenizer and UTF-8 bytes without one, about a 4x difference, while G4 compares tokens. | 04 retrieval step 9 | Budget is always stated in tokens. The offline fallback enforces it as 3 UTF-8 bytes per token, a conservative over-count for English text, and labels the unit (04). |
| X8 medium | Speculative machinery in mandatory scope. The `verify` command with verifier allowlists and evidence roots has one consumer (T01). The 100k-claim stress corpus is roughly four years of the observed capture rate. | 04 verify section; 06 scale fixtures | Move `verify` to WP-10; T01 uses a seeded documentary source. 1k and 10k corpora are mandatory; 100k is optional (04, 06, 07). |
| X9 medium | The direction A fallback is only WP-01. If WP-05 rejects K, revision 1.0 leaves nothing to ship for staleness or mislabelled CURRENT results. | 00 stopping rule | Define direction A as WP-01 plus A-1 (HEAD stamping), A-2 (CURRENT label limited to the overview state block), and A-3 (consolidation prompt marks repository state with its commit). B1 includes them, which also makes the baseline honest (07 WP-01). |

## Changes made in revision 1.1

- 00: revision line, scope paragraph, package map entry for this document.
- 03: version 1 does not write the overview; rollout modes, projection, migration, and rollback simplified to match; source capture records speaker and repository HEAD; Git egress paragraph; writer-fenced protocol retitled as deferred.
- 04: `sources.speaker` and `sources.repo_head`; `repo_state` predicate class and freshness rule; `user_stated` derivation and ranking; short-form review commands and review queue; token-based budget unit; `mode_transitions` and `verify` marked deferred.
- 05: R29 (Git egress), R30 (pasted text treated as user speech), R31 (review starvation), R32 (volatile repository state).
- 06: K0 arm, real-vault set R, G2 applied to K0 and R, 100k corpus made optional.
- 07: WP-00 builds set R; WP-01 adds A-1 to A-3; WP-02 adds the Git-tracking doctor check; WP-06 drops `verify`; WP-07 drops overview takeover; WP-09 applies vault ignore entries with approval; WP-10 lists overview takeover and `verify` as candidates.
- 09: Q16 to Q18. 10: review record. `decisions.tsv`: rows for these decisions.

## Kept deliberately

- The separate `knowledge.sqlite` authority, `reported` as the default status, exact-quote evidence, and no automatic merges. These are the core of the design and they are sound.
- WP-01 reliability fixes and the B0/B1/K separation. B1 is likely to deliver a real share of the benefit on its own, and the design measures that honestly.
- Job leases. A model call can outlive its timeout, and fencing a late response is cheap.
- The sealed synthetic evaluation. It remains a correctness and anti-regression gate. It is no longer the only evidence for adoption.

## Open for challenge

Codex should attack these 1.1 additions specifically. (1) Can a pasted or quoted block be misclassified as typed user speech in any provider format, given R30's heuristics? (2) Does `user_stated` ranking let a user's early, later-reversed decision outrank a valid assistant-reported correction? The intended answer is that an exact-slot conflict still marks both as disputed, but the ranking path should be tested. (3) Is one `git rev-parse` per context request acceptable within G5 on the target workstation?

## Revision 1.2 additions

At the user's request, revision 1.2 adds a vault hygiene sweep before the new system (WP-S1) and a knowledge backfill after adoption (WP-S2). It also adds an explicit current-versus-proposed table with a per-question-class verdict (02, WP-05) and the local-only vault rule (00, 03, 08, R29, R34). Vault-derived details were removed from this package because the AgentMem repository is public.
