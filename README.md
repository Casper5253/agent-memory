# agent-memory (agentmem)

Persistent, searchable memory for Claude Code, Codex and OpenCode in one Obsidian vault.

> Sessions are evidence. Project notes are current knowledge. Search is the bridge between them.

- Vault: `C:\Users\caspe\Documents\Notes\FDE Knowledge` (rules: its `AGENTS.md`)
- Deployed runtime: `~/.agents/memory/` (`app/`, `config.json`, `tmp/`, `backups/`); CLI shim `~/.local/bin/mem(.cmd)`
- Architecture: [docs/release/SYSTEM-ARCHITECTURE.md](docs/release/SYSTEM-ARCHITECTURE.md). Operations and recovery: [docs/release/OPERATIONS-AND-RECOVERY.md](docs/release/OPERATIONS-AND-RECOVERY.md). Release evidence and decision: [docs/release/](docs/release/)
- Earlier design notes: [docs/ASSESSMENT.md](docs/ASSESSMENT.md). The LLMWiki knowledge-store plan is archived in [docs/archive/](docs/archive/) and the store is disabled ([why](docs/llmwiki-evaluation/WHY-NOT-LLMWIKI.md)).

## How it works

0. **Start.** At SessionStart the agent receives its project's status page (every section, compact citations), live git state and a capture health line, capped at 4,000 characters. Linked worktrees resolve to their parent repository's project.
1. **Capture.** Claude Code and Codex hooks (`Stop`, `PreCompact`, `SessionEnd`) and the OpenCode plugin (`session.idle`) only enqueue a tiny job and spawn a detached worker. Each takes about 1 s, and a failure is never surfaced to the agent.
2. **Settle.** A session is processed after `SessionEnd`, or after 20 idle minutes. When unsettled sessions remain, one sleeper process waits out the window, so there's no daemon.
3. **Process.** The adapter reads the transcript from the start of uncaptured content, then the pipeline runs: redact → digest (prompts, final answers, files, commands; never tool output) → quality gate (content judged trivial is kept and re-judged with what follows) → Haiku summary where every claim quotes the digest → one note per logical session in `0-Sessions/YYYY/MM/` with numbered evidence anchors → reconcile into the project's status page → manifest → `qmd update`/`embed`.
4. **Reconcile.** The status page in `1-Projects/<P>/<P>.md` is the only current state. Items cite session evidence. Code, not the model, decides what may leave the page: superseded or resolved only by a compatible newer cited item, never by older evidence. Displaced items go to `History.md`. Updates that add nothing or are rejected are logged and stay visibly pending. Agent process chatter is refused.
5. **Retrieve.** `mem search` (QMD BM25, `--semantic` for vectors) labels every hit CURRENT (status page), PENDING, RECENT_SESSION, HISTORICAL (sessions, history), REFERENCE, ARCHIVED, or UNVERIFIED (an indexed excerpt no longer found on the page), with its on-disk path.

Safeguards: `mem pause` switch; dedupe on `(agent, session_id)` plus a byte cursor; recursion guard (`AGENTMEM_CAPTURE`, and the summarizer runs `claude -p --setting-sources "" --no-session-persistence --tools ""`); atomic and create-once writes; per-session, per-project and worker file locks; SQLite WAL manifest; retries with backoff; JSONL log in `_meta/logs`; secret redaction before any model call or write.

## Compatibility (built 2026-10-14; release verification in [docs/release/ACCEPTANCE-EVIDENCE.md](docs/release/ACCEPTANCE-EVIDENCE.md))

| Agent | Trigger | Transcript | Verified |
|---|---|---|---|
| Claude Code 2.1.299 | `~/.claude/settings.json` hooks: SessionStart (recall pointer), Stop, PreCompact, SessionEnd | `~/.claude/projects/<slug>/<id>.jsonl` | real hook command + this repo's build session → note + reconciled project |
| Codex 0.162 | `~/.codex/hooks.json`, same four events (Herdr hook untouched) | `~/.codex/sessions/**/rollout-*.jsonl` | real hook command + 2 real rollouts; legacy session not duplicated |
| OpenCode 1.18.27 | plugin `~/.config/opencode/plugins/agentmem.js` (`session.idle`, `session.compacted`) | `opencode export <id> --pure` | plugin's exact enqueue command + real session → note |

New hooks load when an agent session starts. **Codex asks once to trust the changed hooks**: approve them.

## Commands

```
mem search <terms> [--layer current|history|reference|archive] [--semantic] [-n N] [--json]
mem sessions [--project P] [--agent A] [--since YYYY-MM-DD] [--until ...]
mem note <path|name> [--section "Active decisions"]
mem projects [--all]
mem stats | mem doctor
mem index [--no-embed]            # create collections, update index, embed
mem capture [--agent A --session ID --transcript PATH]
mem capture --current             # /obsidian: sync the running session now
mem remember "<sentence>" --kind fact|decision|preference|next [--why ...] [--project P]   # /remember
mem consolidate                   # apply pending reconciliation (after a model outage)
mem retry [--session ID]
mem pause | mem resume
```

## Develop

```
python -I -m unittest discover -s tests       # about 600 tests, no network; prove capture or reconciliation changes with a live session too
python -I install.py                           # redeploy app, hooks, plugin, skills, index (idempotent, backs up configs)
```

Config: `~/.agents/memory/config.json` keys `summarizer` (`claude-haiku` or `none`), `settle_minutes`, `embeddings`, `min_user_prompts`, `digest_char_cap`, `excluded_roots`, `qmd_index`. Project mapping: `<vault>/_meta/projects.json`. Routing: the working folder decides when it maps to a project. Generic folders (home, `Documents\Projects` itself, Documents, Codex scratch folders) are resolved from the edited files' locations, then paths or project names in the first prompt; no signal means *Unsorted* (home falls back to Agent Environment). Later parts of a session keep the first part's project. Unknown folders under `Documents\Projects` become projects automatically, except linked worktrees, which join their parent repository's project.

Slash commands (shared skills, all agents): `/obsidian` → `mem capture --current`; `/remember` → `mem remember` (immutable evidence note + reconciliation; no-project items go to `3-Resources/Remembered.md`).

## Rollback

- Disable capture now: `mem pause`.
- Remove integrations: `python -I install.py --uninstall` removes the hooks, plugin and shim. Configs are backed up in `~/.agents/memory/backups/<stamp>/`.
- Restore the old Codex capture: it's archived in `~/.codex/backups/agentmem-20261014/` (`obsidian-session-sync`, `skill-obsidian`); copy back and restore `~/.codex/hooks.json` from the backup.
- Vault: `git -C "<vault>" log` shows `Snapshot before PARA reorganization` (4dcfd8e) and the reorg commit. A full pre-reorg copy is at `Documents\Notes\_backups\FDE Knowledge-pre-PARA-20261014`, and the raw transcript spool is at `Documents\Notes\_backups\obsidian-sync-raw-spool-20261014`.
- Skills: remove `~/.agents/pstack/local/{memory,qmd}` and run `python -I ~/.agents/pstack/sync.py`.
- QMD: `npm uninstall -g @tobilu/qmd`; index and models live in `~/.cache/qmd/`.
