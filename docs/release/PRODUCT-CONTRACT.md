# AgentMem product contract (release 2026-10)

AgentMem is a project-continuity system. When a coding agent starts work in a project, it already knows where the project stands. When the agent stops, what mattered is kept, and the authoritative project status is updated. AgentMem is not primarily a question-answering or RAG product.

This contract is fixed for the release. Testing checks it. Testing does not extend it.

## Scope

- Agents: Claude Code, Codex and OpenCode, through hooks or a plugin installed by `install.py`.
- Team runtime: HX and Herdr run the agents. AgentMem observes their sessions like any other.
- Store: the local Obsidian vault. Project pages are in `1-Projects`, session notes in `0-Sessions`.

## Acceptance criteria

Each criterion is observable. "Live" means shown on the installed system and the real vault. A test suite alone does not satisfy it.

| # | Responsibility | Acceptance criterion |
|---|---|---|
| C1 | Recognize the project | A session started in a repository or one of its worktrees is attributed to that repository's project, never to a fragment or another project. Live, for at least two projects. |
| C2 | Supply current context | At session start, the agent receives the project's status page without being asked. It fits the startup cap with every status section present, and nothing is cut silently. Live, by inspecting the injected text. |
| C3 | Resume without reconstruction | A fresh agent given only the injected context correctly states current work, open items and the next step for a normal task. Live, judged on the agent's answer. |
| C4 | Capture meaningful sessions | A session with real work (at least 2 prompts and an edit, commit or decision) is captured after a normal end, a kill, or a session with no end event (eventual recovery after the settle time). Live. |
| C5 | No redundant notes | One logical session yields one session note. A note gains parts only for new content past the cursor. Trivial sessions write no note, and the reason is recorded. Live, by listing the files created. |
| C6 | One authoritative status | Each capture reconciles into that project's single status page. Items cite their session evidence. Updates that are rejected or add nothing are logged and stay visibly pending, never silently counted as done. Live. |
| C7 | History without stale "current" | A superseded or resolved item leaves the page and is kept in History.md with its reason. Only a compatible newer cited item can displace it, so a decision is retired only by a decision. Older evidence never displaces newer evidence. Live, for one decision reversal. |
| C8 | Retrieve older knowledge | A decision or rationale no longer on the startup page is found by `mem search` and labelled HISTORICAL. The status page is labelled CURRENT. Live. |
| C9 | Autonomous and reliable | No manual step is needed in normal use. Repeated runs are idempotent. Interrupted status writes recover from their journal. Failures appear in the startup health line. Tests plus live health inspection. |
| C10 | Understandable and maintainable | One mechanism per responsibility. The disabled knowledge store is not on any path that writes current state. The architecture document matches the code. Review. |

## Out of scope for this release

- The LLMWiki knowledge store stays disabled (`knowledge_mode: "off"`). See `docs/llmwiki-evaluation/WHY-NOT-LLMWIKI.md`.
- Re-summarizing old sessions that predate cited summaries. They stay visible as held backlog.
- Merging old multi-part session notes and stray fragment project folders.

## Severity

- **P0.** Data loss, corruption, cross-project contamination, or broken continuity.
- **P1.** A core workflow fails: context delivery, capture, reconciliation or historical retention.
- **P2.** An edge case with a workaround. Document it and defer it.
- **P3.** Enhancement. Defer it.
