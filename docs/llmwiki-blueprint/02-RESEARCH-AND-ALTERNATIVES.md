# Research and alternatives

Research accessed 2026-10-21. External links below are primary sources. Remote default branches are moving references, not pinned dependencies. No external implementation was installed or benchmarked. Before reusing code, the implementation team must pin a commit and inspect its license and relevant tests. The recommended release reuses concepts, not third-party code.

## What LLMWiki means here

No repository URL was supplied. The term names a pattern and several independent implementations. This specification examines the original pattern, a session-oriented Python implementation, a Rust retrieval implementation, and a broader compiler. It does not assume these projects are interchangeable.

Karpathy's [LLM Wiki idea file](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) describes ingest-time synthesis into linked Markdown, with source material retained separately. It advocates updating entities and topics and surfacing contradictory claims. It is a conceptual workflow, not a production consistency protocol. Its relevance is persistent synthesis and links. It does not establish benchmark gains for AgentMem.

[Pratiyush/llm-wiki architecture](https://pratiyush.github.io/llm-wiki/docs/architecture.html) documents raw session Markdown, agent-maintained wiki pages, and generated static presentation. The raw layer can contain turn-by-turn conversation. AgentMem deliberately avoids importing that broader source-copying policy. [Its CLI source](https://raw.githubusercontent.com/Pratiyush/llm-wiki/master/llmwiki/cli.py) exposes synthesis, candidate management, lint, graph, and export commands. [Synthesis CLI helpers](https://raw.githubusercontent.com/Pratiyush/llm-wiki/master/llmwiki/synth/cli_helpers.py) show pending synthesis completion. This also demonstrates documentation drift: architecture prose saying only agents write wiki content is not a complete description of the current CLI. Candidate review and lint are relevant; static publishing is not required for local agent memory.

[krakiun/llmwiki](https://github.com/krakiun/llmwiki) emphasizes local search over Markdown rather than loading a full index into context. [Its command source](https://raw.githubusercontent.com/krakiun/llmwiki/main/src/main.rs) includes keyword/semantic selection, bounded graph hops, supersession, stale views, and memory tiers. [Its search source](https://raw.githubusercontent.com/krakiun/llmwiki/main/src/search.rs) combines lexical and semantic ranks through reciprocal rank fusion. This is a useful implementation reference. Its scaling and token-cost claims are not independent measurements and are not adopted as facts about this workload. Numeric confidence decay is not adopted because age and truth are different properties.

[atomicstrata/llm-wiki-compiler](https://github.com/atomicstrata/llm-wiki-compiler) presents a broader source-to-wiki compiler with CLI and MCP tooling. Its README was inspected for scope, not used to certify its runtime guarantees. Replacing AgentMem with it would require a separate adapter, privacy, migration, and recovery assessment. There is no demonstrated need for that dependency in the proposed slice.

## Concepts mapped to AgentMem

| Concept | Existing capability | Proposed use | Rejected or deferred portion |
|---|---|---|---|
| Hierarchy | PARA folders and project overview | Project-local knowledge subfolders | Global taxonomy rewrite |
| Wiki links | Session and project links | Claim, decision, source, and entity links | Graph database |
| Entity extraction | Edited file paths, project routing | Exact file IDs; model-proposed concept aliases | Automatic semantic merging |
| Lexical retrieval | QMD search and scan fallback | Keep QMD; scope and validate results | Second full-corpus search engine |
| Semantic retrieval | Optional QMD vsearch | Optional candidate ranking input | Required embedding service |
| Temporal knowledge | Session dates, active/superseded headings | Recorded time, effective time, explicit successor | Inferring truth from newest timestamp |
| Provenance | Note-level source path and cursor | Claim-to-source locator and hash | Treating generated prose as proof |
| Consolidation | Model rewrite of state block | Proposal, deterministic validation, atomic apply | Unrestricted model file writes |
| Contradictions | Prompt asks model to move old decision | Exact-slot detection plus candidate semantic alerts | Claiming complete natural-language contradiction detection |
| Maintenance | Retry, pending, stats, doctor | Dirty projection queue, integrity lint, freshness views | Autonomous source deletion |
| Agent memory | Agent labels in captures | Evidence author remains metadata | Agent-private truth silos |
| Context construction | Small hook pointer, search snippets | New bounded context command | Full wiki injection at every turn |
| Compression | Digest trimming and summaries | Whole claim bundles under a tokenizer budget | Summaries of summaries accepted as new evidence |

QMD already offers local lexical, semantic, and richer query workflows in its [official repository](https://github.com/tobi/qmd). This supports keeping it as a search dependency. AgentMem's current wrapper does not use all of those modes. The new context command must explicitly combine only the modes it actually invokes.

## Comparison

The following is engineering judgment, not a measured benchmark. Low/medium/high are relative to the inspected AgentMem codebase.

| Criterion | A. Improve current AgentMem | B. Selected wiki capabilities | C. Substantial wiki architecture |
|---|---|---|---|
| Change complexity | Low to medium | Medium, explicit claim store and projection | High, replacement workflows and migration |
| Retrieval benefit hypothesis | Better filtering and fewer stale snippets | Better rationale/temporal answers from typed evidence | Better broad synthesis across many sources |
| Measured quality here | Unknown | Unknown | Unknown |
| Model operating cost | Existing calls; possible reductions | One bounded extraction call; optional review burden | More multi-page synthesis and maintenance calls |
| Read latency expectation | Comparable to baseline | Local filtering plus bounded QMD; measurable | Depends on graph/search/model implementation |
| Maintainability | Familiar modules | Small new package; explicit schema ownership | More dependencies and operational modes |
| Migration risk | Low | Medium; shadow and per-project activation | High; source/path/authority changes |
| Existing hooks | Direct compatibility | Direct compatibility required | Adapter work required |
| Privacy | Existing boundaries | Narrow sources, explicit policy gates | Raw-source copying must be reassessed |
| Recovery | Existing retries need hardening | Transactional claims and replayable projections | Must evaluate an entirely different failure protocol |

Choose B because the goals explicitly require decision rationale, relationships, contradiction handling, and low-context continuity. A alone does not give those concepts stable identities and enforceable semantics. Reject A as the complete target but retain it as the mandatory baseline and fallback. Reject C for this release because it adds broader synthesis and publication machinery before the local benefit is established.

## Current versus proposed: keep the best of both

The new system is added beside the current one, not instead of it. Each capability stays with whichever side does it better. Where the evaluation has not shown the new side to be better, the current side keeps it.

| Capability | Current setup does well | Current setup falls short | Proposed adds | Owner in version 1 |
|---|---|---|---|---|
| Capture | Fully automatic, robust, hooks unchanged | Retry and identity edge cases (01) | Nothing new; WP-01 fixes | Current, with fixes |
| Session notes | Readable, immutable, one per part | Statements are not individually citable | Exact quoted spans that cite the notes | Current; new side only references them |
| Project overview and SessionStart pointer | Zero effort, small, always shown at start | Goes stale; silently overwrites old decisions | Nothing in version 1; takeover deferred | Current |
| Broad search | Finds anything across the vault | Labels by folder, so pending text looks current | Nothing; `mem search` stays | Current, with A-2 label fix |
| Project questions ("what did we decide about X and why") | Answer is somewhere in the prose | Costly to assemble; history and rationale get lost | `mem context`: decisions with rationale, history, and sources in a fixed budget | Whichever arm wins in WP-05 |
| Contradictions | None detected | Newest rewrite wins silently | Conflicts shown side by side | Proposed, if it passes G3 |
| Stale repository facts | None | Old commit and tag lines read as current | `repo_moved` flag; A-1 and A-3 help the current side too | Both |
| Operating cost | One summary and one consolidation call per capture; no extra state | — | One extraction call per capture, a second database, backups | Must stay within G6 |

The WP-05 verdict is per question class (current state, decision and rationale, history, contradictions, broad lookup). `adopt-partial` ships the new side only for the classes it wins. For the rest, `mem context` returns a pointer to `mem search` instead of a weaker answer. A class where the arms tie stays with the current setup, because it costs less to run.

## Decisions deliberately not taken

No vector database, Neo4j, background daemon, MCP server, automatic answer filing, automatic importance decay, or self-improving prompt loop. No new cloud provider. No tool-output capture. A graph is a relation table and rendered links. Freshness is a separate status, not a probability multiplier. Natural-language equivalence requires review; exact identity is deterministic.

SQLite is retained for local transactional state. Its [WAL documentation](https://www.sqlite.org/wal.html) describes concurrent readers with a single writer and the local-host limitations. The implementation must use a local filesystem for live databases and online backup for snapshots. [FTS5](https://www.sqlite.org/fts5.html) is a possible future local index, not a release dependency. A simple bounded per-project claim scan suffices for fallback until measurements justify replacing it.
