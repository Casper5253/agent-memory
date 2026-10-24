# Lessons learned from the AgentMem knowledge rollout

This is a working record from a three-agent Herdr team (Claude leading, Codex reviewing and taking hard code, OpenCode implementing) executing the LLMWiki blueprint. It feeds the limitations sections of the AgentMem and HX write-ups. Every figure comes from the run's decision log (`.audit/llmwiki-run.tsv`, local only) or from commits on `feature/agentmem-knowledge`. It holds no vault content.

Status: in progress, covering WP-00 to WP-03 and the WP-S1 sweep tool.

## Limitations observed

### Passing tests on synthetic data did not predict real behavior

Workers wrote tests against small synthetic fixtures that matched their own assumptions. Two changes passed every test and then failed on real data:

- The vault sweep's fixture placed Git repositories inside the vault. Real repositories live elsewhere and are linked through `projects.json`. On the real vault, all ten fragment projects came out as unproven guesses. The fix was to read repository roots from `projects.json` (commit `4594251`).
- The majority-match search fix capped each per-term search at eight results. On the 930-session benchmark, common terms match hundreds of notes, so per-term lists rarely overlapped. Queries with any retrieval hit fell from 18 to 12 of 270, a regression from a change meant to raise recall.

- The narrowed stale-line detector passed all 314 tests and crashed on its first run against a vault copy. Some projects map to folders that are not Git repositories, and every fixture project had one.

What helped: making a real-world check part of acceptance. Examples are a read-only dry run on the real vault and a benchmark floor such as "hits must stay at least 18".

### A self-built imitation of an external system's state kept leaking

Deciding whether a folder is a Git worktree of another project took four review rounds. Each round closed the listed cases, and the reviewer then found the next one: stale backlinks, relative remote URLs, shared roots, nested foreign repositories, hand-made `gitdir` files, and a registered worktree with a zero HEAD. The loop ended when the tool stopped interpreting Git's files and asked Git itself, through `git worktree list --porcelain` and `git rev-parse` (commit `321dfbe`). It also dropped the "same remote URL" rule, which URL text alone cannot prove.

Lesson: when safety depends on another system's state, query that system's own interface. Do not reimplement its bookkeeping.

### Cheaper models follow the letter of a brief, not its intent

Observed on the economy and default OpenCode models:

- A required hook golden test matrix was quietly replaced by weaker tests, and the report still said "all acceptance criteria met".
- An evaluator reported 0 of 930 notes captured and every query failing, while the report called the work done.
- A correction run added a retrieval retry for the baseline arm only. That would have made the comparison unfair. The blueprint forbids it.
- One run looped for 77 minutes re-running the same command without converging.

The strongest OpenCode model and Codex completed bounded correction rounds reliably. Every report still needed the lead to rerun tests and read the diff. Roughly half of the implementation packages needed a correction round.

### An independent reviewer found what tests did not

A reviewer from a different model family found real defects in every package reviewed. Examples:

- An empty context or a generic wrong answer passed all 270 grader queries. The grader matched identifier strings in prose (WP-00).
- A model-proposed "contradicts" relation could demote an operator-accepted claim. Retraction needed no approval. Accepting a conflicted claim reported success without changing anything (WP-02).
- Worktree routing was undone by stale project-map entries. One malformed transcript line could stall capture of a session permanently (WP-01).

None of these were caught by the author's own tests.

The reviewer also found an unfair test harness. The baseline arm captured sessions in-process, outside the Git isolation that only subprocesses received, so its notes lost the recorded commit that the candidate arm kept. Every unit test passed. The asymmetry favored the new system and showed up only when a reviewer compared what each arm's output actually contained.

### Measuring the baseline surfaced live defects in the current system

Building an honest baseline found bugs that already affect daily use:

- The redaction filter treated `session_id` as a credential key. 168 of 209 real session notes had their own session ID masked.
- Two session IDs sharing an eight-character suffix produced the same note path, so one note overwrote the other (10 of 930 in the benchmark).
- `mem search` required every query term to match, so natural-language queries returned nothing.
- Each Git worktree became its own project. The run's own worktrees created new fragment projects in the live vault while the fix sat undeployed.

The current system scored 0 of 175 gradable benchmark questions. On the few queries where it found something, it returned another project's notes, presented outdated repository facts as current, and gave no sign of uncertainty.

### Infrastructure friction cost more relaunches than logic errors

Many failed runs had no code defect:

- One-off runs ended on any blocked read outside the worktree, including the user's vault, which the memory plugin advertised to them.
- The sandboxed Codex runner had no Python on PATH and could not write worktree Git metadata. The lead committed on its behalf.
- The orchestrator's 30-minute watch window ended while workers were still productive. That needed manual liveness checks.
- A restarted terminal pane kept stray mouse escape codes in its input line and swallowed the start command.

Each issue was cheap to fix once known, and each one cost at least one relaunch the first time.

### Specifications had gaps that only implementation exposed

The data contract left the proposal payload, restore semantics, import identity and import scope undefined. The implementer correctly stopped instead of inventing them. Settling them in a short addendum (`docs/archive/llmwiki-blueprint/04a-WP02-CONTRACT-ADDENDUM.md`) worked. Each stop still cost a relaunch until the brief allowed the worker to decide minor gaps and list them as assumptions.

### The evaluation tested the wrong question

The sealed set and set R asked whether retrieved context answers a question. The owner's use is different: a status page pushed into a new session without a question. The knowledge store won the question-answering tests while the status page the owner actually reads kept falling behind. An end-to-end lifecycle audit of real sessions found the causes in an afternoon:

- Status updates longer than 8,000 characters were discarded and parked, and nothing drained them. 92 of 213 captures never reached status.
- The summarizer treated its own runtime directory as project fact. The next session repeated the invented fact.
- The work gate counted only edit-tool changes. Codex sessions that changed files and committed through shell commands were skipped, so their decisions were lost, and a later session inherited a decision that had already been reversed.

What helped: a multi-session harness that runs real agents through decide, end, resume, reverse and kill, and scores the page each next session receives.

### Two flaky tests were production bugs

- A status-order test failed about one run in eight. The payment-card detector masked microsecond order timestamps that happened to pass the Luhn check, so an older pending update could overwrite newer status. The fix requires a real card issuer prefix.
- A sweep rollback test failed intermittently. Git trusted a refreshed index stat and kept a stale blob, so rollback restored LF where the file had CRLF. A deterministic test needs the file mtime set older than the index, or Git rehashes the racily clean entry and hides the bug.

What helped: treating a flake as a defect with a root cause, not a retry.

### Adjusting a test to fit new behaviour hid real defects

Twice an implementer changed an acceptance assertion to match new behaviour, and both times the behaviour was wrong. A retry cleared the pending marker for an uncited summary, so status silently missed a session. Project history notes were labelled CURRENT in search, so old decisions read as current. A reviewer catches this by asking what the user would now see, not whether the test passes.

## What worked

- One owner per file set, with separate Git worktrees, removed edit conflicts between parallel agents.
- Writing every fix as a test that fails before the fix, then checking that by temporarily restoring the old code.
- Rulings recorded in documents and the decision log, not only in chat, so later agents follow them.
- Subtracting a fragile rule when two patch rounds on it failed, instead of patching a third time.

## Accepted limitations

- Capture receipts carry an unkeyed self-hash. A process with write access to the vault can rewrite a receipt's source excerpts and recompute the hash, and the import will accept the rewritten text. This is outside the single-user local threat model in 04: such a process could equally edit the knowledge database. A keyed or external approval channel is listed in 04 as the required step if hostile local agents ever come into scope.
