# agent-memory (agentmem)

Persistent, searchable memory for Claude Code, Codex and OpenCode in one Obsidian vault.

> Sessions are evidence. Project notes are current knowledge. Search is the bridge between them.

- Vault: `C:\Users\caspe\Documents\Notes\FDE Knowledge` (rules: its `AGENTS.md`)
- Deployed runtime: `~/.agents/memory/` (`app/`, `config.json`, `tmp/`, `backups/`); CLI shim `~/.local/bin/mem(.cmd)`
- Design and trade-offs: [docs/ASSESSMENT.md](docs/ASSESSMENT.md)

## How it works

1. **Capture.** Claude Code and Codex hooks (`Stop`, `PreCompact`, `SessionEnd`) and the OpenCode plugin (`session.idle`) only enqueue a tiny job and spawn a detached worker. Each takes about 1 s, and a failure is never surfaced to the agent.
2. **Settle.** A session is processed after `SessionEnd`, or after 20 idle minutes. When unsettled sessions remain, one sleeper process waits out the window, so there's no daemon.
3. **Process.** The adapter reads only new transcript bytes, then the pipeline runs: redact → digest (prompts, final answers, files, commands; never tool output) → quality gate → Haiku summary (deterministic fallback) → immutable note in `0-Sessions/YYYY/MM/` → reconcile into `1-Projects/<P>/<P>.md` (fallback: *Pending reconciliation*) → manifest → `qmd update`/`embed`.
4. **Retrieve.** `mem search` (QMD BM25, `--semantic` for vectors) labels every hit CURRENT / HISTORICAL / REFERENCE / ARCHIVED, with its on-disk path.

Safeguards: `mem pause` switch; dedupe on `(agent, session_id)` plus a byte cursor; recursion guard (`AGENTMEM_CAPTURE`, and the summarizer runs `claude -p --setting-sources "" --no-session-persistence --tools ""`); atomic and create-once writes; per-session, per-project and worker file locks; SQLite WAL manifest; retries with backoff; JSONL log in `_meta/logs`; secret redaction before any model call or write.

## Compatibility (as built and verified 2026-10-14)

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
python -I -m unittest discover -s tests -v     # 17 tests: the 12 required scenarios + safeguards, no network
python -I install.py                           # redeploy app, hooks, plugin, skills, index (idempotent, backs up configs)
python -I tools/migrate_vault.py VAULT         # (one-off, already applied) PARA migration, dry run by default
```

Config: `~/.agents/memory/config.json` keys `summarizer` (`claude-haiku` or `none`), `settle_minutes`, `embeddings`, `min_user_prompts`, `digest_char_cap`, `excluded_roots`, `qmd_index`. Project mapping: `<vault>/_meta/projects.json`. Routing: the working folder decides when it maps to a project. Generic folders (home, `Documents\Projects` itself, Documents, Codex scratch folders) are resolved from the edited files' locations, then paths or project names in the first prompt; no signal means *Unsorted* (home falls back to Agent Environment). Later parts of a session keep the first part's project. Unknown folders under `Documents\Projects` become projects automatically.

Slash commands (shared skills, all agents): `/obsidian` → `mem capture --current`; `/remember` → `mem remember` (immutable evidence note + reconciliation; no-project items go to `3-Resources/Remembered.md`).

## Rollback

- Disable capture now: `mem pause`.
- Remove integrations: `python -I install.py --uninstall` removes the hooks, plugin and shim. Configs are backed up in `~/.agents/memory/backups/<stamp>/`.
- Restore the old Codex capture: it's archived in `~/.codex/backups/agentmem-20261014/` (`obsidian-session-sync`, `skill-obsidian`); copy back and restore `~/.codex/hooks.json` from the backup.
- Vault: `git -C "<vault>" log` shows `Snapshot before PARA reorganization` (4dcfd8e) and the reorg commit. A full pre-reorg copy is at `Documents\Notes\_backups\FDE Knowledge-pre-PARA-20261014`, and the raw transcript spool is at `Documents\Notes\_backups\obsidian-sync-raw-spool-20261014`.
- Skills: remove `~/.agents/pstack/local/{memory,qmd}` and run `python -I ~/.agents/pstack/sync.py`.
- QMD: `npm uninstall -g @tobilu/qmd`; index and models live in `~/.cache/qmd/`.
