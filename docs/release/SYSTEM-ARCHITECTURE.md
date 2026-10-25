# AgentMem system architecture

This describes the code as released, at the commit named in `RELEASE-DECISION.md`. It is not a design proposal.

## What AgentMem does

AgentMem gives coding agents project continuity. At session start, the agent receives the current status of the project it is working in. At session end, AgentMem stores the session as a note and reconciles what changed into that project's single status page. Older facts move to the project history, where search can still find them.

It runs without user action. Agent lifecycle hooks trigger it, and a detached background worker does the slow work.

## Where things live

| What | Where | Role |
|---|---|---|
| Status page | `1-Projects/<Project>/<Project>.md`, managed block `<!-- agentmem:status:v2 -->` | The only authoritative current state of a project |
| Project history | `1-Projects/<Project>/History.md` | Displaced status items with reason and source. Search labels it HISTORICAL. |
| Session notes | `0-Sessions/YYYY/MM/<date>-<agent>-<title>-<id>.md` | One note per logical session, with numbered evidence anchors. Write-once, then appended by continuation parts. HISTORICAL. |
| Manifest | `_meta/manifest.sqlite` | Capture bookkeeping: sessions, cursors, captures, retry holds. Never holds project facts. |
| Logs | `_meta/logs/capture.jsonl` | Every capture, rejection, hold and error |
| Project map | `_meta/projects.json` | Repository roots to projects |
| Runtime | `~/.agents/memory/app` (code), `config.json` | Installed by `install.py` |
| Knowledge store | `_meta/knowledge.sqlite`, code in `agentmem/knowledge/` | Built and **disabled** (`knowledge_mode: "off"`). Not on any path that writes current state. |

## Components

```
  Claude Code / Codex hooks          OpenCode plugin (agentmem.js)
  SessionStart, Stop, PreCompact,    chat.system.transform (start),
  SessionEnd                         session.idle / compacted
            \                              /
             v                            v
        run.py hook <agent>   (agentmem/hook.py: fast, never fails the agent)
          |  SessionStart: build startup context     \  other events: enqueue,
          |  (status page + live git + health)        \  spawn detached worker
          v                                            v
   injected into the agent                 worker (agentmem/worker.py)
                                              |  drain queue, discover settled
                                              |  transcripts, capture
                                              v
                                   pipeline (agentmem/pipeline.py)
            adapters -> extract (digest, redaction, gate) -> llm.summarize (Haiku)
              -> session note (notes.py) -> reconcile into status page (status.py)
              -> displaced items to History.md -> index refresh (QMD)
                                              |
                     mem search / mem context (agentmem/search.py, cli.py)
                     CURRENT = status page, HISTORICAL = sessions and history
```

| Component | Responsibility | Code |
|---|---|---|
| Hook entry | Map agent events to "start", "turn" and "end". Return valid hook JSON. Must not block or fail the agent. | `agentmem/hook.py`, `integrations/opencode/agentmem.js` |
| Worker | Drain the queue. Capture sessions that ended or have settled (no new transcript bytes for `settle_minutes`, 20 by default). Retry failed consolidations, except held ones. | `agentmem/worker.py` |
| Adapters | Read Claude, Codex and OpenCode transcripts into one event shape | `agentmem/adapters.py` |
| Extract | Build a bounded digest: prompts, answers, commands, edits, git evidence. Redact secrets. Gate out trivial sessions (fewer than 2 prompts and no real work). | `agentmem/extract.py`, `agentmem/redact.py` |
| Summarizer | Haiku turns the digest into a cited summary. Each claim carries a quote, and a claim is kept only if the quote is in the digest (`agentmem/grounding.py`). | `agentmem/llm.py` |
| Notes | Write the session note and its evidence anchors. Continuation parts append to the same note. | `agentmem/notes.py` |
| Status reconciliation | Merge new cited items into the page, under code-owned rules (below). Write through a journal. | `agentmem/status.py`, `agentmem/pipeline.py` |
| Project identity | Attribute a session to a project from its repository. Linked worktrees resolve to the parent repository. | `agentmem/projects.py` |
| Search | QMD-backed search. Labels results CURRENT or HISTORICAL by where the text lives. | `agentmem/search.py` |
| Remember | `mem remember` records an explicit fact, decision or next step through the same reconciliation path | `agentmem/remember.py` |

### HX and Herdr

Herdr runs agents in terminal panes. HX routes work between them. Neither calls AgentMem to read or write project state. Each agent's own hooks fire as they would for any session, so a team session is captured like a solo one. HX tells agents to record durable decisions with `mem remember`, which goes through the same reconciliation path. There are no circular dependencies: AgentMem does not depend on HX or Herdr.

## Lifecycle

1. **Start.** The hook resolves the project from the working directory and builds the startup context. That is the status page with compact citations, live git branch, HEAD and working-tree state, a capture health line, and recent sessions, capped at 4,000 characters. Git facts are read live and never stored as status.
2. **During the session.** Stop and PreCompact (Claude and Codex) and `session.idle` (OpenCode) enqueue a "turn" and start the worker. The worker captures only new transcript content past the session's cursor.
3. **End.** SessionEnd enqueues "end". OpenCode has no end event, so its sessions are captured once they have settled.
4. **Capture.** The pipeline builds the digest. A trivial session is marked skipped, with its reason in the manifest, and writes no note. Otherwise it writes or extends the single session note and records the capture with a receipt, which freezes the note text and source range.
5. **Reconcile.** The page is locked and re-read. The model receives the current page and the new cited summary. It returns which items to keep, which new items to add, and which to displace, with evidence. Code then enforces the rules below and writes the page and history through a journal.

## Critical invariants

These are enforced in code, not left to the model:

- **One authority.** Only the status page states current project state. Session notes, history and the manifest never do.
- **Evidence.** Every new item cites a session evidence anchor. Uncited summaries never change the page.
- **Displacement needs a reason.** Items leave the page only as superseded, resolved or outdated by live git, or as trimmed when a section is over its cap. Displaced items go to History.md with their reason.
- **Compatible replacement.** A decision is superseded only by a decision. A question, next step or active item is replaced or resolved only by a compatible cited item.
- **Chronology.** Each item remembers the order of its source evidence. Older evidence cannot displace, resolve or move a newer item. When a section is over its cap, the oldest evidence goes first. Overlapping sessions reconcile instead of being parked.
- **Bounded pages.** The pending list shows at most 10 lines and a count. Older pending lines move verbatim to History.md, and retries still find their captures.
- **No silent no-ops.** An update that adds nothing, or whose new items are all rejected, is logged with the reason and stays visibly pending. Updates that can never pass (uncited, or no new item accepted) are held from automatic retry but stay counted in the health line.
- **Project facts only.** Agent process items ("idle", "awaiting review", "a run in progress") are refused and logged.
- **The `updated` date only moves forward.** Failed retries never touch it.

## Failure recovery

- **Interrupted status writes.** The page change is written to a journal first. On the next access, `recover_status_update` finishes it: history first, then the page. A journal that conflicts with a manual edit is set aside as `.refused-<ts>` and recorded as pending. It is never applied over the edit.
- **Capture failures** are retried with backoff and shown in the startup health line. `mem retry` requeues errored sessions.
- **Killed sessions** have no end event. They are captured once the transcript has settled and a later hook or worker run discovers them. Recovery is eventual and depends on that next run (see `RELEASE-DECISION.md`).
- **Legacy pages** that predate the v2 format are shown read-only, with a visible "page not migrated" pending line. `tools/migrate_status.py` converts them deterministically, with dry run, backup and hash checks.

## Why LLMWiki stays disabled

The knowledge store was planned as a second, claim-structured memory. Its goals (evidence-linked claims, decision succession, stale-fact detection) are now enforced by the status page itself. On questions built from real repository history, today's search is level with it or ahead, and it failed its own safety gate by showing superseded facts as current. A second store holding current state would compete with the status page. It stays off and is not on any write path. The full argument is in `docs/llmwiki-evaluation/WHY-NOT-LLMWIKI.md`.

## Maintaining and extending

- **New agent.** Add a transcript reader in `adapters.py` and wire its lifecycle events to `run.py hook <agent>` in `install.py`. Everything downstream is shared.
- **Changing what reaches the page.** Change the prompt in `llm.py` and the code rules in `status.py` together. Code rules are the safety net, so never rely on the prompt alone.
- **Changing sections or caps.** `status.SECTIONS`. The startup order is `status.STARTUP_ORDER`.
- **Tests.** `tests/` (run `python -m unittest discover -s tests`). Tests are necessary but not sufficient. Prove changes to capture or reconciliation with a live session on the real vault, as described in `OPERATIONS-AND-RECOVERY.md`.
