---
name: obsidian
description: Sync the current agent session into the Obsidian vault now (agentmem). Use for /obsidian, "sync to obsidian", "save this session", "update the vault", or before ending a session whose work should be searchable right away.
---

# /obsidian

Capture is automatic (session end, or 20 idle minutes). `/obsidian` flushes the session that's running **now**.

1. Run:
   ```
   mem capture --current
   ```
   It finds this session (Claude: `CLAUDE_CODE_SESSION_ID`; Codex: `CODEX_THREAD_ID`; OpenCode: newest session for this folder; force one with `--agent`). It writes the immutable session note to `0-Sessions/YYYY/MM/` and reconciles the project's current state. The project is routed automatically from the folder, the files edited and the first prompt.
2. Report the JSON result in one line: project, `note` path, or `skip_reason` if it was too small to keep (trivial sessions are skipped by design). `status: captured` with the same note as last time means nothing new happened since the previous sync.
3. Syncing again later is safe: only new work is captured, as a `-p2`, `-p3` part note. Nothing is duplicated.

To save one specific fact or decision rather than the whole session, use `/remember`.
Troubleshooting: `mem stats`, `mem doctor`. Capture paused? `mem resume`.
