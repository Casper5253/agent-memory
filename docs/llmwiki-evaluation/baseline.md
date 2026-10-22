# WP-00 baseline method

## Method

The baseline arm (B0) measures retrieval quality using the existing `mem search --json` and `mem note` CLI over the vault's QMD index (or fallback scan when QMD is unavailable). No structured knowledge store is involved. No model calls are made.

### Pipeline

1. Load synthetic fixtures from `tests/fixtures/knowledge/{selftest,hooks}/`.
2. Create an isolated temporary vault and runtime (`AGENTMEM_HOME`).
3. Replay each corpus session through the capture pipeline with a deterministic summarizer that returns the frozen `summary_response` from the corpus.
4. For each query, run `mem search --json` with the query terms, then `mem note` for each hit to retrieve note content.
5. Package snippets into a fixed-byte-budget context (same budget as the query specifies).
6. Grade against gold expectations: required claims/sources present, forbidden strings absent, rationale matched, uncertainty signaled where expected.
7. Lifecycle check: verify that the pipeline actually persisted session notes (not just returned search results).

### Grader rules

- `required_claims`: substring must appear in any retrieved snippet.
- `required_sources`: any-match mapping from source ID to session note by `(session_id, part)`.
- `forbidden_current_claims`: substring must NOT appear in CURRENT-labeled snippets only (allows retrieval of disputed alternatives as historical context).
- `forbidden_strings`: substring must NOT appear in any snippet.
- `required_rationale`: substring must appear if non-null.
- `expected_uncertainty`: at least one of `unresolved`, `contradict`, `uncertain`, `disagree` must appear if true.

### QMD status

The evaluator distinguishes three QMD states:
- `unavailable`: QMD not installed; baseline uses fallback scan.
- `empty`: QMD installed but index empty.
- `ok`: QMD installed and index populated, with version recorded.

### Exit codes

- 0: correctness pass (all queries pass grader and lifecycle check).
- 1: failed adoption gate (some queries fail grader or lifecycle check).
- 2: invalid setup (missing fixtures, hash mismatch).
- 3: inconclusive (no queries to evaluate).

### Output artifacts

`--output DIR` receives:
- `result.json`: commit, Python/SQLite versions, QMD status, fixture hashes, per-metric counters, costs (null when unknown), verdict, limitations.
- `per_query.csv`: one row per query with pass/fail, snippet count, packed bytes, failure reasons.
- `report.md`: human-readable summary with commands, hashes, failures, and limitations.

### Limitations

The baseline does not measure:
- Structured claim retrieval (no knowledge store exists yet).
- Temporal reasoning (no effective-date tracking).
- Contradiction resolution (no authority model).
- Token-level budget enforcement (uses byte fallback).
- Real model extraction quality (uses replay model).

These limitations are explicit. The baseline establishes the floor; later work packages add structure and measure improvement against it.

## Commands

```bash
# Run baseline evaluation
python -I tools/evaluate_knowledge.py --arm baseline --fixtures tests/fixtures/knowledge/selftest --output .audit/b0

# Run full test suite (30 old + 19 new)
python -B -I -m unittest discover -s tests -v
```

## No invented numbers

This document describes the method only. No baseline measurements, latency numbers, or quality scores are reported here. Those belong in the evaluation report after running the harness against the full fixture set.
