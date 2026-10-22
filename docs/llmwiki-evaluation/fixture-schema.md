# WP-00 fixture interface

This file fixes the interface between the synthetic fixtures (`tests/fixtures/knowledge/`) and the evaluator (`tools/evaluate_knowledge.py`), so that two people can build them in parallel. The blueprint is normative: [06](../llmwiki-blueprint/06-BENCHMARK-AND-TEST-PLAN.md) owns cases, metrics and gates, and [04](../llmwiki-blueprint/04-DATA-AND-INTERFACE-CONTRACTS.md) owns machine contracts. If this file disagrees with either, the blueprint wins and this file is amended.

All fixture content is synthetic. It must contain no vault content, real session titles, real project names or personal paths.

## Files

| File | Content |
|---|---|
| `manifest.json` | `schema_version: 1`, `seed: 1701`, `generation_command`, `files: {name: sha256}`, `counts: {projects, sessions, queries_dev, queries_sealed}`, `partitions: {dev: sha256, sealed: sha256}` (hash of the sorted query IDs plus their expected rows), `gold_author` |
| `corpus.json` | Projects and sessions (below) |
| `queries.jsonl` | One query per line, no gold |
| `expected.jsonl` | One gold row per query ID |
| `transcripts/<agent>/...` | Provider-format transcripts referenced by the corpus |

## corpus.json

```json
{
  "schema_version": 1,
  "clock": "2026-10-01T00:00:00Z",
  "projects": [
    {"id": "alpha", "name": "Alpha", "roots": ["{WORK}/alpha"], "status": "active", "policy": "model_allowed",
     "repo": {"head": "<40-hex B>", "tags": {"v2": "<40-hex B>"}}}
  ],
  "sessions": [
    {"session_id": "…", "agent": "claude|codex|opencode", "cwd": "{WORK}/alpha", "part": 1,
     "started": "…Z", "ended": "…Z", "repo_head": "<40-hex A or null>",
     "transcript": "transcripts/claude/<file>.jsonl",
     "summary_response": "<exact model response string the real summarizer prompt expects>",
     "case_ids": ["T03"]}
  ],
  "filler": {"generator": "deterministic", "seed": 1701, "count": 200}
}
```

`{WORK}` and `{VAULT}` are the only path placeholders. The evaluator replaces them with isolated temporary directories.

`summary_response` is a frozen model output. The baseline arms do not call a model: the evaluator installs a replay model that returns this string for that session and part. A missing response is a setup failure (exit 2), never an empty summary. Consolidation responses, if the pipeline needs them, follow the same rule through an optional `consolidation_responses` list keyed by project and capture order.

## queries.jsonl

```json
{"query_id": "T03-v01", "case": "T03", "variant": "reviewed|unreviewed|null", "partition": "dev|sealed",
 "project": "alpha", "query": "…", "mode": "current|history|as_of", "as_of": "…Z or null", "budget": 2400}
```

There are 27 cases with 10 variants each: five `dev` and five `sealed`. Each case appears in both partitions with different entities and values (06).

## Query formulation

Every arm uses `tools.knowledge_grader.formulate_query` version 1 for its primary search. It lowercases the question, tokenizes words while retaining internal hyphens and apostrophes, drops the fixed `QUERY_STOPWORDS` set, and keeps the first four remaining words in order. The rule never consults gold, project metadata, or fixture entity patterns. The full stopword set and the exact primary terms are recorded in the result. An unchanged full-question search is measured separately for every query. Neither form retries with different terms. Raw-CLI positive diagnostics are reported separately and do not contribute to quality scores.

## expected.jsonl

```json
{"query_id": "T03-v01", "gold": {"required_claims": [], "required_sources": [], "forbidden_current_claims": [],
 "required_rationale": "… or null", "expected_uncertainty": false, "expected_labels": [], "forbidden_strings": [],
 "relevance": {"<source or claim id>": 2}}}
```

Claim and source IDs are fixture-local stable strings. For baseline prose, the grader maps a source ID to a session note by `session_id` and part (any-match mapping, 06 Metrics). `forbidden_strings` holds isolation canaries and injected text that must never appear in a context package.

Unreviewed same-slot cases grade honest uncertainty with disputed alternatives, not a picked winner; as-of queries include only temporally eligible alternatives.

## Evaluator output

`--output DIR` receives `result.json`, `per_query.csv` and `report.md`. `result.json` holds the commit, Python and SQLite versions, QMD status (`unavailable`, `empty`, or `ok` with version), tokenizer ID or `byte-fallback`, fixture hashes, counters per metric, costs (`null` when unknown, never `0`), per-query outcomes, and the verdict. Correctness pass and adoption pass are separate fields. The exit codes are 0 for pass, 1 for a failed adoption gate, 2 for invalid setup and 3 for inconclusive (06).
