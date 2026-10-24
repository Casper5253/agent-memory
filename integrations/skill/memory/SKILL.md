---
name: memory
description: Persistent agent memory in the shared Obsidian vault (agentmem). Use when the user asks what was previously decided, implemented, configured or discussed; when resuming work that needs prior project context; when asked to remember, capture, save or record something; or to check memory health. Search the vault instead of guessing about past work.
---

# Memory (agentmem)

One vault for every agent: `C:\Users\caspe\Documents\Notes\FDE Knowledge` (`OBSIDIAN_VAULT_PATH`). Rules live in its `AGENTS.md`.
Sessions are evidence. Project notes are current knowledge. Search is the bridge.

| Layer | Folder | Use it for |
|---|---|---|
| CURRENT | `1-Projects/<P>/<P>.md` (status page in the state block) | what is true now; every item cites its session evidence |
| HISTORICAL | `0-Sessions/YYYY/MM/`, `1-Projects/<P>/History.md` | what a past session did or decided, and items retired from the status page with their reason |
| PENDING / UNVERIFIED | status page pending block / stale index excerpt | awaiting reconciliation / run `mem index` and search again |
| REFERENCE / ARCHIVED | `2-Areas`, `3-Resources` / `4-Archive` | reusable knowledge / retired projects |

## Retrieve (do this before answering any "what did we decide / how did we set up / why" question)

1. `mem search "<specific terms>"`. Use names, file names, tools and error strings. Add `--layer current` or `--layer history` to narrow, `--semantic` for paraphrased recall.
2. Read the source: `mem note "<path or name>" --section "Key decisions (decision, reason)"` (or the whole note). Snippets are leads, not evidence.
3. If a HISTORICAL hit conflicts with the CURRENT project state, the project state wins. Say that the older decision was superseded, and when.
4. Answer with the source path(s). If nothing is found, say so; never invent prior decisions.

Other views: `mem projects` (active projects and their current state), `mem sessions --project "<P>" [--agent codex] [--since 2026-10-01]`.
QMD can be used directly too (see the `qmd` skill): collections `vault-projects`, `vault-sessions`, `vault-areas`, `vault-resources`, `vault-archive`.

## Capture

Slash commands: `/remember` saves one item now (`mem remember "…" --kind …`); `/obsidian` syncs the running session now (`mem capture --current`). Both route to the right project automatically: the working folder first, then edited files and the first prompt's paths or project names when the folder is generic.

Capture is automatic. Claude Code and Codex hooks, plus the OpenCode plugin, record each session. It's captured after it ends, or after 20 idle minutes. Trivial sessions are skipped.
- "Remember this" / "save this to memory": use `/remember` (`mem remember "<self-contained sentence>" --kind fact|decision|preference|next`). Prefer `/remember` to change current state. A manual edit goes **inside** the project note's `agentmem:state` block and must keep the status-page format (seven sections, one cited item per line). Never edit `0-Sessions/` notes: they are immutable.
- Force a capture of everything that's ready: `mem capture`. One specific session: `mem capture --agent claude --session <id> --transcript <path>`.
- Privacy: `mem pause` stops all capture; `mem resume` restarts it.

## Consolidate and recover

- `mem stats` shows counts, errors, unconsolidated captures, queue and index health. `mem doctor` gives quick checks.
- Failed captures retry automatically with backoff; force with `mem retry`.
- Captures made while the model was down sit in a project's *Pending reconciliation* block; apply them with `mem consolidate`.
- Search index stale or missing: `mem index` (`--no-embed` for keyword-only).

Never write secrets, `.env` contents or personal journal material into the vault. Never touch `Documents\Notes\Personal Vault`.
