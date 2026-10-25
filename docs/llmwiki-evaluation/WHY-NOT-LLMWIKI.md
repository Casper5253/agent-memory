# The case against adopting LLMWiki

Status: draft for the owner to verify, 2026-10-24. Not a final decision.

This document argues that AgentMem should not adopt the LLMWiki knowledge store as a second memory system. It is written so each step can be checked. Every claim carries a label:

- **Measured**: observed in a test or a live run, with the source named.
- **Inferred**: reasoned from code or from measured results.
- **Judgment**: an opinion that someone could reasonably dispute.

Sources are local unless noted. `.audit/` is never committed. It holds the run log (`llmwiki-run.tsv`), the WP-05 gate report (`wp05/WP-05-gates.md`) and the P7 baseline (`e2e/P7-baseline/report.md`).

## 1. What "LLMWiki" means here

The pattern comes from Andrej Karpathy's [LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f). A model maintains a persistent Markdown wiki over immutable raw sources, guided by a schema file, through ingest, query and lint operations.

The blueprint (`docs/archive/llmwiki-blueprint/00-EXECUTIVE-DECISION.md`) did not plan a full LLM-written wiki. It explicitly rejected that as direction C. The plan was direction B: add selected LLMWiki capabilities to AgentMem. That means a project knowledge store of evidence-linked claims, with explicit decision succession and a context package for each project.

That store is built. It is about 2,600 lines in `agentmem/knowledge/`, installed and switched off with `knowledge_mode: "off"` (Measured). In the tests it is called arm K0. The old search is B1.

The blueprint set its own stopping rule:

> If structure adds tokens, manual review burden, or maintenance work without sufficient retrieval gain, retain direction A and stop the wiki rollout.

This document asks whether that rule now applies.

## 2. Why it was planned

The original reasons were sound for the system as it was on 2026-10-21:

1. AgentMem could not tell a statement an agent reported from a fact established by evidence. Project pages held claims without proof.
2. Decisions had no succession, so a reversed decision could still appear as current.
3. Repository facts went stale without notice.
4. The old search scored 0 on the sealed question set (Measured, WP-05: B1 0/88).

## 3. The argument against

### 3.1 Its main ideas are already in the status page

The status page built in P2 to P6 now has the properties that motivated LLMWiki:

| LLMWiki goal | Status page today | Label |
|---|---|---|
| Claims linked to evidence | Each item cites a passage `[[note#eN]]`; the quote must exist and the claim must match it (`agentmem/grounding.py`) | Measured, live on 2026-10-24 |
| Explicit decision succession | Displacement needs a reason and evidence; only a decision can supersede a decision (`status.py`, p6-supersede) | Measured, tests and live |
| Older evidence never overrides newer | Per-citation source order; overlapping sessions reconcile (p6-order) | Measured, live demo 2026-10-24 |
| Stale repository facts flagged | Live git state at startup; git-state claims are displaced, not stored | Measured |
| Recoverable history | Displaced items go to History.md, labelled HISTORICAL in search | Measured |

The problems LLMWiki was meant to solve were solved in the system that sessions already read at startup (Inferred). What LLMWiki adds beyond this is mainly question answering over a claim store.

### 3.2 On real questions it no longer clearly wins

There are two test sets:

- **The sealed set** is synthetic. Its questions and fixtures were written to test claim structure.
- **Set R** has 36 questions built from the git history of real repositories. Its gold answers come from the repositories, not from AgentMem.

| Test | Old search (B1) | LLMWiki (K0) | Label |
|---|---|---|---|
| Sealed, WP-05 re-gate | 0/83 | 34/83 | Measured |
| Sealed, B1 rerun at today's code | 0/83 | (frozen) 34/83 | Measured, P7 baseline |
| Set R, WP-05 (complete, 36 pairs) | 6 wins | 9 wins, 21 ties | Measured |
| Set R, B1 at today's code vs frozen K0 (28 of 36 graded) | 10 wins | 7 wins, 11 ties | Measured, partial |

On the synthetic set, LLMWiki wins decisively. On real questions, today's search is level with it or ahead.

Two caveats weaken this point:

- The K0 answers are frozen from WP-05 and were not rerun on today's code (Measured). A K0 rerun could gain too.
- The Set R rerun is partial and one run only. 8 pairs failed grading and the stale check was not run (Measured).

The synthetic set may favour K0 by design, since its fixtures are structured claims (Inferred). Its 0/83 for B1 includes 52 unmeasured cases (Measured).

### 3.3 It failed its own safety gate

At the last gate (WP-05 re-gate, 8c6d892), K0 failed G3. Empty results did not say "no trustworthy knowledge" for two cases, and 5 cases showed a superseded fact as current (Measured).

Showing outdated decisions as current is the failure this whole project exists to remove (Judgment). The fix would reuse the status page's succession rules, which means building the same logic twice in two stores (Inferred).

### 3.4 Two writers of "current state" is the risk the owner named

The owner's requirement is one product that works end to end, not two 80% systems that do not know about each other (owner decision, 2026-10-24).

LLMWiki stores its own claims about each project. The status page also states current project state. If both are live, they can disagree, and a session cannot tell which to trust (Inferred). Preventing that requires a shared capture path, shared succession rules, shared health and a combined test. That is estimated at about 3 days (Judgment, from today's pace).

### 3.5 Every live run found bugs the tests missed

Today's live runs found six real defects that 572 unit tests did not catch:

- a migration classifier that kept 0 of 39 items
- a next step retiring a decision
- overlapping sessions parked forever
- a silent no-op marked done
- a split receipt using the wrong project
- startup context truncation

(Measured, `.audit/llmwiki-run.tsv`.) Each system with its own write path needs its own live hardening. A second store doubles that surface (Inferred).

### 3.6 Cost

| Cost | LLMWiki | Label |
|---|---|---|
| Code to maintain | about 2,600 more lines | Measured |
| Model calls | an extraction pass per capture, plus a backfill over 213 session notes | Inferred from design |
| Startup tokens | none if used only for questions | Inferred |
| Build and verify | 1 day (questions only) to 3 days (full merge) | Judgment |

## 4. The strongest arguments for LLMWiki

A fair verdict has to answer these:

1. **"Why" questions.** On real Set R decision and rationale questions, K0 won 4 and B1 won 0, with 4 ties (Measured, WP-05). The status page keeps at most 8 decisions, so older reasoning falls into history. That is where a claim store could answer better than text search.
2. **Temporal questions.** On sealed temporal questions, K0 scored 3/16 and B1 0/16 (Measured). The numbers are small, but the direction is clear.
3. **The rerun is incomplete.** Section 3.2 rests on a partial, single run against frozen K0 answers.
4. **The work is already paid for.** The store exists and mostly passed its gates (G1, G2, G5, G6, G7). Shelving it wastes that (Judgment).

## 5. Verdict

**Recommendation (Judgment).** Do not adopt LLMWiki as a second store of project state. Keep the status page as the only source of current state, and keep the knowledge store switched off.

The one open question is whether a narrow use earns its place: answering "why did we decide X" from history. Answer it with a small test, not a build.

**The test that would change this decision:**

- About 10 questions the owner would really ask, weighted to "why" and "when" questions, with gold answers from the repositories.
- Run them against today's search and a K0 rerun on today's code.
- If K0 wins clearly on "why" and "when" questions, build the slim version, about 1 day. It would answer questions only, from the same captures, defer to the status page on anything current, and reuse its succession rules.
- If it does not, retire the store and record the result in LESSONS.md.

**What is not decided here:** deleting the store's code. It stays switched off until that test runs.

## 6. How to check this document

| Claim | Check |
|---|---|
| Store size and switch | `agentmem/knowledge/`, `agentmem/config.py` (`knowledge_mode`) |
| Sealed numbers | `.audit/wp05/WP-05-gates.md`, `.audit/e2e/P7-baseline/report.md` |
| Set R numbers | `.audit/llmwiki-run.tsv` row "Set R grading completed", and the P7 baseline report |
| G3 failures | `.audit/llmwiki-run.tsv` row "WP-05 re-gate at 8c6d892" |
| Status page properties | `agentmem/status.py`, `agentmem/grounding.py`, tests `test_p6_*.py` |
| Live defects | `.audit/llmwiki-run.tsv`, rows dated 2026-10-24 |
| Original plan and stopping rule | `docs/archive/llmwiki-blueprint/00-EXECUTIVE-DECISION.md` |
