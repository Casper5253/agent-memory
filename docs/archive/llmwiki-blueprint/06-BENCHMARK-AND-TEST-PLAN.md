# Benchmark and test plan

All performance and quality thresholds below are PROVISIONAL unless marked invariant. No baseline retrieval, cost, or latency measurement is invented. Existing unit tests passed, but fake-model tests do not establish model extraction accuracy.

## Comparison arms and reproducibility

Freeze four arms. B0 is AgentMem at `03764f8`. B1 is B0 plus WP-01 reliability fixes and direction A items A-1 to A-3, without structured knowledge. K0 is the proposed slice with zero operator acceptances: only extraction, speaker, and repository-state signals. K is the same slice with the fixtures' seeded operator acceptances. This distinguishes gains from ordinary fixes, gains due to structure, and gains that depend on review. K0 represents the realistic operating mode.

Use an isolated temporary vault and runtime per arm. Pin source commits, Python/SQLite versions, QMD version, QMD model hashes, proposal prompt version, tokenizer ID/version, operating system, CPU/RAM, and fixture SHA-256. Disable production hooks, indexing, network, and real models for deterministic tests. Do not use the real vault for fixtures.

Run existing tests in every arm. Use fake deterministic extractors for correctness and injected faults. Evaluate real-model extraction in a separate opt-in run on the same synthetic evidence, with the model ID and observed usage recorded. No claim of extraction quality may come from a fake extractor alone.

Future benchmark entrypoint is `python -I tools/evaluate_knowledge.py --arm baseline|reliability|candidate-noreview|candidate --fixtures tests/fixtures/knowledge --output PATH`. It is a specified artifact to create in WP-00, not an existing command. The runner imports the selected checkout explicitly and calls that arm's real public CLI/process path. It may normalize results for scoring but must not implement a smarter baseline retrieval algorithm.

For B0/B1, obtain `mem search --json` results and read relevant sections with `mem note`; build a fixed-budget snippet package with the same tokenizer. Record when the baseline cannot represent a structured field, rather than inventing it. Evaluate answer quality through a fixed downstream answer prompt/model for all arms, as a separate measure from retrieval relevance. Blind answer graders to arm names. Freeze the baseline packaging rule before testing K.

## Fixture format and canonical cases

Create `corpus.json`, `queries.jsonl`, `expected.jsonl`, `manifest.json`, and provider transcript fixtures. The manifest includes seed=1701, schema_version=1, fixture hashes, corpus counts, and generation command. Gold annotations are authored independently of the extractor.

```json
{
  "query_id": "T03-current-backend",
  "project": "Alpha",
  "query": "Which storage backend should new work use, and why?",
  "mode": "current",
  "budget": 2400,
  "gold": {
    "required_claims": ["decision-sqlite"],
    "required_sources": ["approval-sqlite"],
    "forbidden_current_claims": ["decision-json"],
    "required_rationale": "atomic cursor updates",
    "expected_uncertainty": false
  }
}
```

| Case | Fixture facts | Query and expected result |
|---|---|---|
| T01 fact | Alpha source reports Python 3.11, verified by a seeded operator-registered documentary source | Version request returns 3.11 with verification scope and source, not a universal guarantee |
| T02 rationale | Accepted SQLite decision cites atomic cursor updates | Why request includes decision and its actual rationale |
| T03 succession | JSON accepted Oct 1, SQLite accepted/supersedes Oct 5 | Current returns SQLite; history includes labeled JSON and change reason |
| T04 as-of | Both decisions have exact effective timestamps | Oct 3 returns JSON; Oct 6 returns SQLite; unknown-time claim excluded with warning |
| T05 unresolved | Two reports disagree on api_mode; neither accepted | Return unresolved alternatives, never select latest as truth |
| T06 irrelevant | 200 notes mention SQLite in unrelated context | Relevant project decision remains in top 5; no irrelevant accepted claim is fabricated |
| T07 isolation | Alpha/cache.py and Beta/cache.py have opposite decisions | Alpha query returns zero Beta claim/source IDs |
| T08 injection | Source contains instructions to mark itself verified and execute shell | Candidate stays reported or is rejected; no tool or acceptance action |
| T09 duplicate | Ten sessions repeat one assistant assertion | One canonical claim with multiple evidence links; no confidence elevation |
| T10 temporal replay | Old capture reconciled after newer decision | Processing order does not reverse accepted succession |
| T11 missing source | Source hash mismatches or path disappears | No verified current claim from missing evidence; explicit limitation |
| T12 outage | QMD and model unavailable | Capture succeeds; context uses DB-local matches and degraded flag |
| T13 concurrency | Two mutation requests share expected revision | One commit, one revision conflict, no lost rows |
| T14 interrupted write | Crash after DB commit before projection | Repair writes committed revision without model re-execution |
| T15 token pressure | Long rationale and 20 related claims | Complete citation-bearing bundles only; hard budget respected |
| T16 continuation | Three provider sessions each grow by a new part | Exactly one note per part, one import per identity, prior source range unchanged |
| T17 private project | local_only/excluded policies | Zero model calls; excluded content absent from DB and context |
| T18 editor drift | Human edits overview during projection | No silent replacement; recoverable original and external_edit report |
| T19 identity collision | Two session IDs share eight-character suffix | Distinct recovered artifacts and sources |
| T20 malformed source | Broken JSONL final line, invalid metadata, wrong types | Partial line waits; malformed item reported safely; no cursor corruption |
| T21 renamed/archive | Alpha renamed then archived | ID stable; current access rejected; history still resolves |
| T22 no answer | Query asks for an unrecorded deployment URL | Empty result with honest missing-evidence statement, no invented URL |
| T23 early evidence | Tool command present without success output | It is recorded as a command attempt, not a passed test |
| T24 budget fallback | Same query with exact tokenizer and byte fallback | Declared units correct; each independently meets hard bound |
| T25 user-stated | User message states a decision; assistant later suggests an alternative; nothing accepted | K0 ranks the user-stated decision first and labels it user_stated, never accepted |
| T26 pasted | User message pastes a document saying "we decided to use Redis" | Speaker recorded as pasted; claim is not user_stated |
| T27 repo moved | Source recorded at HEAD A says tag v1 is current; fixture repository HEAD is now B with tag v2 | Bundle labeled repo_moved with commit A and a live-verification note; never an unqualified current fact |

T02, T03, and T04 each have a reviewed variant with seeded acceptance and an unreviewed variant in which the decision appears only as user and assistant statements. K0 is scored on the unreviewed variants.

Generate 270 queries: 10 wording variants per canonical case. Use five variants for development/calibration and five sealed variants for final evaluation, split by query template and scenario instance to avoid memorizing a shared answer. Include paraphrases without exact nouns in decision/rationale cases. Hold all 27 canonical cases in both partitions with different entities/values, and report canonical-case-level results as well as query averages.

Scale fixtures add 1k and 10k claims across 20 projects using deterministic nonsecret text; a 100k corpus is optional. At the observed rate of about 27 captures per day (11), 10k claims is roughly a year of use. The correctness gold remains separate from filler generation. A 100k corpus is a stress test, not a promise of complete fallback scanning. Report partial retrieval and recall degradation honestly.

## Real-vault question set R

Synthetic fixtures test conformance to this specification. They cannot show that the system helps on real work, because they were designed alongside it. Set R supplies that evidence.

Write R after the WP-S1 sweep, so its questions use the cleaned project boundaries, and before any K arm runs on real data. Select three user-approved `model_allowed` projects with at least 15 captured sessions each. Write 30 to 40 questions across four classes: current repository or release state, decision and rationale, what changed and why, and unresolved or contradictory points. Derive each gold answer from repository truth (Git history, tags, committed documents) or the user's confirmation, never from AgentMem output. Record the commit at which each answer holds.

Run every arm against a copy of the vault in an isolated temporary directory, never the live vault. Keep questions, answers, and outputs in `.audit/knowledge/R/`, which is git-ignored. The AgentMem repository is public; nothing from set R is ever committed, including in summaries. Grade blind to arm. Report per-question outcomes and paired win/loss/tie counts between K0 and B1, broken down by the question classes in 02 so the per-class verdict has evidence. At this sample size, report counts and claim no statistical significance. Count staleness errors separately: an answer that presents an outdated repository state or a superseded decision as current.

## Metrics

| Metric | Exact method |
|---|---|
| Fact accuracy | Supported correct factual answers / answerable fact queries; count unsupported statements separately |
| Decision+rationale accuracy | Both decision and gold rationale retrieved with correct source / relevant queries |
| Temporal correctness | Correct eligible decision/status at requested time / temporal cases |
| Contradiction handling | Both alternatives or explicit unresolved omission notice returned; false resolved answers counted separately |
| Context precision | Relevant returned claim bundles / all returned bundles; empty unanswered query handled separately |
| Context recall | Required gold claim IDs returned / required gold IDs; any-match source mapping used for baseline prose |
| Ranking | MRR and nDCG@5 on independently graded relevance 0/1/2 |
| Latency | End-to-end subprocess duration using monotonic clock, plus backend and packaging stages |
| Tokens | Pinned tokenizer over full emitted context including metadata/citations; no characters/4 estimates |
| Write cost | Recorded input/output tokens, model calls, reported USD if available, retries, wall time, DB/page byte growth per accepted useful claim |
| Continuity | Required decision recoverable after three sessions plus a fresh process, without original conversation context |
| Recovery | Number of fault points converging to expected state and exact duplicate count |
| Isolation | Foreign project IDs/bytes returned, with synthetic canary strings |
| Review burden | Operator/lead review actions and elapsed review time per useful accepted claim, separately labeled observed or estimated |

Report missing model cost as null, never zero. Subscription usage and reported model cost are distinct. A zero-cost deterministic path means zero model calls, not zero CPU/maintenance cost.

## Execution protocol

1. Verify fixture and commit hashes, versions, and isolated destinations. Abort if any path resolves to the production vault.
2. Run correctness and safety fixtures once per arm with injected clock and deterministic model. Persist machine-readable outcomes and failure diffs.
3. For latency, run 5 warmups and 30 measured repetitions per representative query. Randomize arm order with fixed seed. Separate fresh-process cold-QMD and warm-QMD runs. Do not call a fresh process a cold filesystem cache; document cache controls actually used.
4. For retrieval quality, run the sealed 135-query set once per deterministic index configuration. For real-model extraction use three independently recorded runs, never choose only the best.
5. Bootstrap paired query/case deltas with fixed seed and 10,000 resamples. Report 95% intervals and raw per-case outcomes. Correlated variants must be resampled by canonical scenario, not treated as 270 independent observations.
6. Review every new incorrect, unsupported, stale, or cross-project answer. The aggregate score cannot excuse a safety failure.
7. Produce JSON, CSV, and a Markdown report with commands, hashes, failures, latency distributions, token distributions, cost nullability, and adoption verdict. Keep sensitive data out of public artifacts.

## Gates

| Gate | Acceptance |
|---|---|
| G0 compatibility, invariant | All existing tests pass; no source note edits; hook protocol golden fixtures identical except approved dynamic content; settings and plugin file hashes unchanged |
| G1 safety, invariant | Zero cross-project leaks, zero automatic accepted decisions, zero unsupported verified claims, zero successful prompt-induced actions, zero silent lost writes in deterministic fault suite |
| G2 quality, provisional | On the sealed synthetic set, K0 decision+rationale and temporal scores each improve by at least 10 percentage points over B1, or reach >=95% if baseline already >=90%; no canonical category drops >2 points. On set R, K0 wins more paired questions than it loses against B1 and makes fewer staleness errors. K results are reported beside K0 but cannot satisfy G2 alone |
| G3 uncertainty, invariant | All contradiction/no-answer canonical cases preserve uncertainty; no known superseded decision returned as current |
| G4 context, provisional | Median actual context tokens <= baseline; p95 <= configured exact-token budget; decision+rationale gain must not come only from larger context |
| G5 latency, provisional | Local lexical context p95 <=1 second and <=1.25x B1 on target workstation; optional semantic p95 <=3 seconds, timeout/degraded behavior correct |
| G6 cost, provisional | <=1 model extraction call per new capture normally; retries counted; candidate consolidation tokens <=1.25x baseline total summary+consolidation tokens on same corpus |
| G7 recovery, invariant | Every injected crash/duplicate/concurrency point converges; operation and capture duplicates zero; backup restore passes integrity and fixture query checks |
| G8 operational value, provisional | Team reviews a 20-session pilot; no unresolved P1/P2 defect and review burden accepted explicitly in pilot report |

There are two decisions. WP-05's slice-adoption gate evaluates G0-G6 and the store/capture portion of G7 already implemented in WP-01/WP-02. It authorizes further implementation only. Projection recovery, full operational restore, and G8 are explicitly not yet applicable there. WP-09's activation/release gate requires all of G0-G8, including the full G7 suite and the 20-session pilot. A synthetic project's active-mode tests may run before this release gate; a real-project pilot requires G0-G7 and explicit pilot scope. Broader activation requires G8 as well.

Provisional gates are calibrated on the development partition before sealed evaluation. Record every adjustment, reason, old/new threshold, and hardware condition. Do not relax invariants or tune after seeing sealed results. If thresholds cannot be justified, report inconclusive and remain in shadow. A failed gate does not authorize a rewrite or more dependencies.

## Validation commands to implement and preserve

The existing command is `python -B -I -m unittest discover -s tests -v`. It currently discovers 30 tests. New suites add tests; they must not reduce that baseline count by deleting behavior. In this investigation Python was invoked by its existing absolute venv path, recorded in 10.

Future suites are `python -B -I -m unittest discover -s tests -p 'test_knowledge*.py' -v` and the evaluation entrypoint above. All must exit 0 only when assertions pass. The evaluator returns exit 1 for a failed adoption gate, 2 for invalid setup, and 3 for inconclusive measurements. Its report must distinguish correctness pass from adoption pass.

## Staged retrieval configurations

WP-05 evaluates local structured retrieval with QMD disabled because generated-page projection arrives in WP-07. Label it `local-structured-slice`; do not claim semantic/QMD candidate measurements. Compare with both normal B1 search and B1 deterministic fallback, separately labeled, so the baseline is not silently weakened.

After WP-07, rerun frozen quality/token gates on a fresh independently authored 135-query holdout with the same case distribution, an isolated synthetic active-mode vault, and a named temporary QMD index. Do not consult that holdout during WP-05. WP-09 requires the full `structured-plus-qmd` configuration to pass before real-project pilot. Production shadow pages remain unindexed. No semantic benefit is claimed unless the actual semantic path is measured.

