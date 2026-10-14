---
name: remember
description: Save something to persistent memory (the shared Obsidian vault) right now. Use for /remember, "remember that …", "note for next time", "don't forget …", "save this decision", or when the user states a durable fact, decision, preference or follow-up worth keeping. With no specific item, it syncs the current session instead.
---

# /remember

Memory is captured automatically at session end. `/remember` is for making something durable **now**, routed to the right place automatically.

## With something to remember

Distil it into one self-contained sentence that will make sense months later without this chat (names, paths, versions; no "this" or "it"). Pick the kind, then run:

```
mem remember "<sentence>" --kind fact|decision|preference|next [--why "<rationale>"]
```

- `decision` for a choice that was made (add `--why`); `fact` for something verified (config value, path, behaviour); `preference` for how the user likes things done; `next` for a follow-up.
- Routing is automatic. The project comes from the working directory. When that's generic (home, the Projects root, Codex scratch folders), it comes from paths or project names in the sentence. Mention the project or a path if the context is ambiguous, or force it with `--project "<Name>"`. Items with no project go to `3-Resources/Remembered.md`.
- Several items: run the command once per item.
- Never include secrets (they're redacted anyway) or personal journal content.

The command writes an immutable evidence note (`0-Sessions/…-remember-…md`) and reconciles the item into the project's current state. If it contradicts an earlier decision, the old one moves to *Superseded decisions*. Report back the project and note path it printed. If `consolidated` is false, the model was unavailable and the item sits in the project's *Pending reconciliation* block; `mem consolidate` applies it later.

## With nothing specific ("/remember" alone, or "remember this session")

```
mem remember --session
```

This captures the running session into the vault now, the same as `/obsidian`. Say which note it produced, or why it was skipped (for example, too little happened yet).

## Check what's remembered

Use the `memory` skill (`mem search`, `mem projects`) to find things later.
