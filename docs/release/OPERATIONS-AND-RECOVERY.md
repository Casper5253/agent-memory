# Operations and recovery

This is for the person who runs AgentMem. It covers normal running, checking that it works, and recovering from failures. The design is in `SYSTEM-ARCHITECTURE.md`. The contract is in `PRODUCT-CONTRACT.md`.

## How it runs normally

No action is needed. The installed integrations do the work.

- **Claude Code and Codex** call `mem hook <agent>` on SessionStart, Stop, PreCompact and SessionEnd. For turn and end events it queues work and starts a detached worker.
- **OpenCode** uses a plugin file at `~/.config/opencode/plugins/agentmem.js`. It sends the start event and the idle and compacted events. OpenCode has no end event, so its sessions are captured once they have settled.
- **The worker** drains the queue. It captures a session when the session ends, or when its transcript has had no new bytes for `settle_minutes` (20 by default).
- **The hook** does not capture anything itself. It records the event, starts the detached worker and returns.
- **A killed session** has no end event. It is captured after it settles, on a later hook or worker run.

Install or re-wire everything with:

```
python -I install.py            # app, config, mem shim, hooks, plugin, skills, QMD index
python -I install.py --no-hooks # skip the agent hooks
python -I install.py --no-index # skip the QMD index step
python -I install.py --uninstall
```

Uninstall removes hooks, the plugin and the shims. It leaves the vault, the app and the data in place.

## What a new session gets

At SessionStart the hook builds the startup context. It has the project's status page with compact citations, the live git branch and working-tree state, a capture health line, and the three most recent sessions. It is capped at 4,000 characters. Every status section should fit.

To see exactly what a session would receive, simulate SessionStart. Build the payload with Python, not with `printf`, because Windows backslashes break in shell strings:

```
python -I -c "import json, os; print(json.dumps({'hook_event_name': 'SessionStart', 'session_id': 'sim-0001', 'cwd': os.getcwd()}))" | mem hook claude
```

The output is the JSON that the agent receives. The context is in `hookSpecificOutput.additionalContext`. The simulated call also records a start event for `sim-0001`, so it counts as an unended session in the health line. Use a made-up ID, and expect that one entry until it is cleared.

## How capture and status updates happen

1. The pipeline builds a digest of the session and skips trivial sessions. It records the skip reason and writes no note.
2. It writes the session note, or adds a part to it when the session grew. Notes are in `0-Sessions`.
3. It asks the model for a cited summary. A claim is kept only when its quote is in the digest.
4. It locks the project page, re-reads it and reconciles the summary into the page under code rules.
5. The page and any displaced items are written through a journal. Displaced items go to `History.md`.

Each reconciliation outcome is logged in `_meta/logs/capture.jsonl`.

## Check that memory is working

| Command | What it tells you |
|---|---|
| `mem doctor` | Vault exists, manifest directory is writable, `claude` and `opencode` CLIs and QMD are found, pause state, stale locks (older than a day), and queue backlog |
| `mem stats` | Capture and index counts, unconsolidated notes, recent errors |
| `mem sessions --project <name> -n 20` | Recent captured sessions for one project |
| `mem projects` | Active projects and their current state. `--all` lists every project. |
| `mem search <terms>` | Ranked search. Results are labelled CURRENT (status page) or HISTORICAL. Use `--layer current` or `--layer history` to narrow. |
| `mem note <name> --section <heading>` | One section of a note or page |

The startup health line shows what is wrong. It can say that sessions are not yet captured or are retrying, that a capture is awaiting consolidation, that a status update is pending, or that a status replay was refused.

Events in `_meta/logs/capture.jsonl` that matter:

- `captured`: a session was captured.
- `status-item`: one status item was accepted, rejected, refused or restored. The `action` field gives which.
- `llm-fallback`: the model was unavailable or its output failed checks. The capture falls back and stays pending.
- `pending-retry-held`: a retry was held, either because the summary is uncited or because no new item was accepted.
- `pending-retry-failed`: a retry failed for another reason.
- `status-replay-refused`: a journal conflicted with a manual edit and was set aside.
- `error`: a capture failed. The session goes to the error state.

`pending-order-conflict` is no longer written. Older captures that wait on a newer page are reconciled instead.

## Recovery

**A failed capture.** Failed sessions retry with backoff. To reset them now:

```
mem retry                     # every session in error
mem retry --session <id>      # one session
```

`mem retry` also runs the worker.

**Pending reconciliation.** A pending update is shown on the project page as `status update pending`. The page shows the 10 most recent pending lines and a count; older ones are in the project's `History.md`. Re-run reconciliation for all pending captures:

```
mem consolidate --limit 20
```

**Held captures.** A hold is set when a retry can never pass. There are two reasons:

- `uncited summary`: the stored summary has no evidence citations.
- `no new item accepted`: the model answered, but nothing new was accepted.

A held capture is not retried automatically. It stays in the backlog and in the health line, and its note and receipt are untouched. **No command clears a hold yet.** Do not edit the manifest by hand. Report the held captures to the release owner.

**An interrupted status write.** The change is written to a journal first. On the next access, the journal is finished: history first, then the page. If the journal conflicts with a manual edit, it is renamed to `<journal>.refused-<timestamp>` and the update stays pending. It is never applied over your edit. Compare the page with the `.refused-` file, apply the change by hand if it is still wanted, then delete the `.refused-` file.

**Pause and resume capture.**

```
mem pause     # writes _meta/PAUSE; hooks record nothing
mem resume    # removes the pause
```

## Duplicate notes and wrong project attribution

Projects are matched from the repository root. Linked worktrees resolve to their parent repository. The mapping is in `_meta/projects.json`.

- To see which sessions were attributed to a project, run `mem sessions --project <name>`. Add `--json` for machine-readable output.
- To list the projects AgentMem knows, run `mem projects --all`.
- If a session landed in the wrong project, check that its repository root is in `_meta/projects.json` under the right name. Then re-capture the session with `mem capture --session <id> --cwd <path>` if it has no note yet.
- If one logical session has more than one note, check `mem sessions` for the same session ID. Continuation parts should append to the same note. Report any duplicate pair with both paths.

## Troubleshooting each integration

**Claude Code.** Hooks live in `~/.claude/settings.json`. Check that the SessionStart, Stop, PreCompact and SessionEnd entries call `mem hook claude`. Re-wire them with `python -I install.py`. Run the simulation above to confirm the hook answers.

**Codex.** Hooks live in `~/.codex/hooks.json`. After any change to that file, Codex asks you to review and trust the hooks again. If you skip that prompt, the new hooks do not run. Re-wire with `python -I install.py`, then accept the trust prompt in Codex.

**OpenCode.** The plugin is `~/.config/opencode/plugins/agentmem.js`. Check the resolved configuration with `opencode debug config`, and check the skills with `opencode debug skill`. Re-install the plugin with `python -I install.py`.

**Any integration.** Run `mem doctor`. Then re-wire with `python -I install.py`.
