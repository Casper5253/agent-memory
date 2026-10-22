# WP-00 measured baseline

This is one completed synthetic correctness run of B0. It is not an adoption or performance result. The AgentMem application, runner and integrations have no diff from `03764f8`; only the evaluation harness and tests changed.

```text
python -B -I -m unittest discover -s tests -v
python -B -I tools/evaluate_knowledge.py --arm baseline --fixtures tests/fixtures/knowledge --output .audit/b0-wp00c-final
python -B -I .audit/review-WP-00/reproduce_fixed.py
```

The full suite passed 90 tests. The completed evaluator returned exit 1 with verdict `fail`. Correctness and adoption are both false. The parent commit recorded during the run is `f94e0daa652e03d4480b52eb97aa076f1002dc9b`; the evaluator SHA-256 is `4b93ed201cf01b93c777ce72a8d4b269167c0a2a6ce7c6bab471400451a40a76`. The final WP-00c commit contains this exact evaluator source. Raw machine outputs are retained locally in `.audit/b0-wp00c-final/` and are not committed.

Observed versions were Python 3.11.9, SQLite 3.45.1 and qmd 2.8.3 (facd35e). QMD indexing returned `{"collections_added": ["vault-projects", "vault-sessions", "vault-areas", "vault-resources", "vault-archive"], "update": 0}` and its isolated SQLite database contained 1211 active documents before temporary-state cleanup.

## Query formulation and baseline behavior

Primary searches use the frozen version 1 rule in `tools/knowledge_grader.py`: lowercase, remove the fixed stopword set, and retain the first four remaining words. The same rule is specified for every comparison arm. It does not inspect gold, project metadata or fixture entity patterns. The exact terms and full stopword set are in `result.json`. Each query also receives one unchanged full-question search. Neither form retries or substitutes terms. See [the fixture interface](fixture-schema.md#query-formulation).

Installed QMD 2.8.3 joins positive lexical terms with AND. A separate isolated probe returned hits for `SQLite storage` and an entity keyword, but no hit for a full natural-language question. This is a real B0 limitation and a direction A candidate for later work. This package does not change `agentmem/search.py` or replace the real CLI's retrieval/fallback algorithm.

| Search form | Queries | QMD calls | Queries with QMD hits | Fallback calls | Queries with fallback hits | Search errors | Quality passed / measured | Unmeasured |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Four-word primary | 270 | 270 | 18 | 0 | 0 | 0 | 0 / 175 | 95 |
| Raw full question | 270 | 270 | 0 | 0 | 0 | 0 | 0 / 175 | 95 |

QMD/fallback provenance comes from observing the real CLI's `_qmd` return or exception without changing its inputs or result. Successful empty QMD results remain empty; the evaluator does not invoke an additional fallback. QMD hit counts describe retrieval, not correct answers or complete packed snippets.

The following raw-CLI controls only prove that indexing and note retrieval work. They do not contribute to quality scores.

| Diagnostic terms | Backend | Hits | Packed snippets |
|---|---|---:|---:|
| `historical` | qmd | 8 | 2 |
| `storage` | qmd | 8 | 2 |

## Per-case counts

Every canonical case has ten queries, five development and five sealed. Fault execution is unmeasured, rather than inferred from labels in retrieved prose. T21 has five current-access cases unmeasured and five historical retrieval cases measured.

| Case | Primary quality passed / measured | Unmeasured | Primary QMD hit queries | Raw QMD hit queries | Primary / raw fallback calls |
|---|---:|---:|---:|---:|---:|
| T01 | 0 / 10 | 0 | 0 | 0 | 0 / 0 |
| T02 | 0 / 10 | 0 | 0 | 0 | 0 / 0 |
| T03 | 0 / 10 | 0 | 0 | 0 | 0 / 0 |
| T04 | 0 / 10 | 0 | 0 | 0 | 0 / 0 |
| T05 | 0 / 10 | 0 | 0 | 0 | 0 / 0 |
| T06 | 0 / 10 | 0 | 0 | 0 | 0 / 0 |
| T07 | 0 / 10 | 0 | 0 | 0 | 0 / 0 |
| T08 | 0 / 10 | 0 | 0 | 0 | 0 / 0 |
| T09 | 0 / 10 | 0 | 0 | 0 | 0 / 0 |
| T10 | 0 / 10 | 0 | 0 | 0 | 0 / 0 |
| T11 | 0 / 10 | 0 | 0 | 0 | 0 / 0 |
| T12 | 0 / 0 | 10 | 0 | 0 | 0 / 0 |
| T13 | 0 / 0 | 10 | 0 | 0 | 0 / 0 |
| T14 | 0 / 0 | 10 | 0 | 0 | 0 / 0 |
| T15 | 0 / 10 | 0 | 0 | 0 | 0 / 0 |
| T16 | 0 / 0 | 10 | 0 | 0 | 0 / 0 |
| T17 | 0 / 0 | 10 | 0 | 0 | 0 / 0 |
| T18 | 0 / 0 | 10 | 0 | 0 | 0 / 0 |
| T19 | 0 / 0 | 10 | 0 | 0 | 0 / 0 |
| T20 | 0 / 0 | 10 | 0 | 0 | 0 / 0 |
| T21 | 0 / 5 | 5 | 3 | 0 | 0 / 0 |
| T22 | 0 / 10 | 0 | 0 | 0 | 0 / 0 |
| T23 | 0 / 10 | 0 | 5 | 0 | 0 / 0 |
| T24 | 0 / 0 | 10 | 0 | 0 | 0 / 0 |
| T25 | 0 / 10 | 0 | 5 | 0 | 0 / 0 |
| T26 | 0 / 10 | 0 | 0 | 0 | 0 / 0 |
| T27 | 0 / 10 | 0 | 5 | 0 | 0 / 0 |

## Lifecycle and evidence limits

Lifecycle passed is `False`. The manifest recorded 930 captures; 910 capture rows had readable notes with matching identity and noncolliding paths, with 10 colliding capture paths and 0 missing files. There were 920 eligible expected session parts. Lifecycle failure remains separate and does not overwrite per-query grades. The full error list is in the machine result.

The run observed 930 capture-row references whose note header contained the exact `[REDACTED]` session-ID marker. This count includes repeated references to colliding note paths. B0's credential-key filter in `agentmem/redact.py` treats `session_id` as sensitive, so capture masks its own identity metadata. This is a measured B0 defect deferred to WP-01. The grader accepts only that exact marker and binds through a unique manifest row, matching agent/part/project, and supported content; real ID mismatches, altered markers, unsupported text and colliding rows still fail. No real vault content or counts were used.

The anti-gaming replay returned zero passing adversarial contexts across 270 queries, zero empty-context passes and zero lifecycle false positives. It caught 13 hook mutations plus removal of the required hook matrix. Positive grader controls include a real rendered canonical claim whose ID differs from the session ID, alternate supporting sources and whitespace-normalized truncation.

The grader checks supported visible content, rationale, required labels and honest uncertainty through the cited note's manifest identity. Baseline prose cannot represent all structured fields; these remain failed or unmeasured. The 800-character snippet cap and conservative byte packaging were fixed before this run and were not tuned after inspecting sealed results. Some retrieved notes therefore do not produce a complete supported bundle.

Frozen replay summaries and no-op consolidation exclude real model extraction and normal consolidation quality. There were zero real model calls by construction, but model cost is null, not an invented zero-dollar observation. Fault injection, semantic retrieval, exact-token measurements, latency distributions, adoption gains and real-vault set R remain unmeasured. One exploratory run was stopped after a Windows cleanup handle defect was found. An earlier completed diagnostic run rejected every masked header before the operator approved the bounded identity mapping above. Neither run contributes counts to this final table; the search rule, queries and quality thresholds were not tuned between runs.

## Isolation and fixture identity

The harness sets process-local `INDEX_PATH`, QMD config, XDG config/cache/data, home, temporary and Node cache paths under its temporary root. The explicit index name is `index`; its actual SQLite path is checked under that root. This also avoids named-index URL suffixes that the current `mem note` path mapper cannot resolve. UTF-8 mode preserves exact note text on Windows. Python bytecode writes are disabled.

A live integration test poisoned inherited profile/cache/config/index paths with a separate synthetic outside directory, ran real `mem index --no-embed`, `mem search` and `mem note`, and verified the outside sentinel directory was unchanged. It checked the actual SQLite and QMD YAML files under the isolated root. This is bounded evidence for those configured write locations, not a whole-machine filesystem audit. No installed application, provider settings, real vault or global QMD state was changed.

All declared fixture file hashes were verified before execution. The main file hashes below pin the corpus and gold; `manifest.json` additionally pins every provider transcript.

| File | SHA-256 |
|---|---|
| `manifest.json` | `2852100447a11cc63b138bd2513a2ea63c1bc5d58a39b82dab99ec9450efdbad` |
| `corpus.json` | `a3d1e9bb8f6d13ad0fcd0fa4a07685c42c6b26f2be90a53c40e482077b9ea97f` |
| `queries.jsonl` | `73b7364ca86d823101507234adf84f1d64d62a8e757d429cd39e6070d8b00780` |
| `expected.jsonl` | `effe923e563284a0a782008b73ad980205bf35c6cb4baa6698acb6eba049dfbd` |
