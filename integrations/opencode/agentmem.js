// agentmem capture plugin for OpenCode (installed by agent-memory/install.py; reinstalling overwrites this file).
// OpenCode has no session-end hook: each `session.idle` (end of a turn) records activity, and agentmem
// captures the session once it has been idle for the settle window. The worker exports the transcript
// with `opencode export --pure`, so this plugin never loads inside the capture process (no recursion).

import { spawn } from "node:child_process";

const PYTHON = "__PYTHON__";
const RUNNER = "__RUNNER__";
const childSessions = new Set();

function enqueue(sessionID, directory) {
  if (process.env.AGENTMEM_CAPTURE) return; // inside our own pipeline
  try {
    const child = spawn(
      PYTHON,
      ["-I", RUNNER, "enqueue", "--agent", "opencode", "--session", sessionID, "--event", "turn", "--cwd", directory || process.cwd()],
      { detached: true, stdio: "ignore", windowsHide: true },
    );
    child.on("error", () => {});
    child.unref();
  } catch {
    // memory capture must never disturb the agent
  }
}

export const AgentMemPlugin = async ({ directory } = {}) => ({
  event: async ({ event }) => {
    const props = event?.properties ?? {};
    const info = props.info;
    if (info?.id && info.parentID) childSessions.add(info.id); // subagent sessions belong to their parent
    if (event?.type !== "session.idle" && event?.type !== "session.compacted") return;
    const sessionID = props.sessionID;
    if (typeof sessionID !== "string" || !sessionID || childSessions.has(sessionID)) return;
    enqueue(sessionID, directory);
  },
});

export default {
  id: "agentmem.capture",
  server: AgentMemPlugin,
  setup() {},
};
