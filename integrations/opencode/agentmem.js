// agentmem capture plugin for OpenCode (installed by agent-memory/install.py; reinstalling overwrites this file).
// OpenCode has no session-end hook: each `session.idle` (end of a turn) records activity, and agentmem
// captures the session once it has been idle for the settle window. The worker exports the transcript
// with `opencode export --pure`, so this plugin never loads inside the capture process (no recursion).
//
// Startup context: OpenCode calls `experimental.chat.system.transform` with {sessionID, model} and an output
// whose `system` array becomes the system prompt (opencode 1.18.27, the prompt builder that triggers it).
// The startup package is the same one Claude and Codex get at SessionStart, produced by `run.py hook opencode`.

import { spawn } from "node:child_process";

const PYTHON = "__PYTHON__";
const RUNNER = "__RUNNER__";
const TRANSFORM = "experimental.chat.system.transform";
const START_TIMEOUT_MS = 15000;
const childSessions = new Set();

export function parseHookOutput(stdout) {
  try {
    const text = JSON.parse(stdout)?.hookSpecificOutput?.additionalContext;
    return typeof text === "string" ? text : "";
  } catch {
    return "";
  }
}

export function runStartHook(sessionID, directory) {
  return new Promise((resolve, reject) => {
    const child = spawn(PYTHON, ["-I", RUNNER, "hook", "opencode"], { stdio: ["pipe", "pipe", "ignore"], windowsHide: true });
    let out = "";
    const timer = setTimeout(() => {
      child.kill();
      reject(new Error("agentmem startup timed out"));
    }, START_TIMEOUT_MS);
    child.stdout.on("data", (chunk) => {
      out += chunk;
    });
    child.on("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.on("close", () => {
      clearTimeout(timer);
      resolve(parseHookOutput(out));
    });
    child.stdin.end(JSON.stringify({ hook_event_name: "SessionStart", session_id: sessionID, cwd: directory || process.cwd() }));
  });
}

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

// One startup run per session, cached for the life of the plugin process. A failed run caches as empty.
export function createPlugin(run = runStartHook) {
  return async ({ directory } = {}) => {
    const startup = new Map();
    return {
      event: async ({ event }) => {
        const props = event?.properties ?? {};
        const info = props.info;
        if (info?.id && info.parentID) childSessions.add(info.id); // subagent sessions belong to their parent
        if (event?.type !== "session.idle" && event?.type !== "session.compacted") return;
        const sessionID = props.sessionID;
        if (typeof sessionID !== "string" || !sessionID || childSessions.has(sessionID)) return;
        enqueue(sessionID, directory);
      },
      [TRANSFORM]: async (input, output) => {
        if (process.env.AGENTMEM_CAPTURE) return;
        const sessionID = input?.sessionID;
        if (typeof sessionID !== "string" || !sessionID || childSessions.has(sessionID)) return;
        if (!Array.isArray(output?.system)) return;
        if (!startup.has(sessionID)) {
          startup.set(sessionID, Promise.resolve().then(() => run(sessionID, directory)).catch(() => ""));
        }
        const text = await startup.get(sessionID);
        if (text) output.system.push(text);
      },
    };
  };
}

export const AgentMemPlugin = createPlugin();

export default {
  id: "agentmem.capture",
  server: AgentMemPlugin,
  setup() {},
};
