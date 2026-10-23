// Run: node --test tests/opencode/agentmem-plugin.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { createPlugin, parseHookOutput } from "../../integrations/opencode/agentmem.js";

const TRANSFORM = "experimental.chat.system.transform";

async function hooksWith(run) {
  return createPlugin(run)({ directory: "C:/work/alpha" });
}

test("startup context is pushed into the system prompt once per session", async () => {
  let calls = 0;
  const hooks = await hooksWith(async () => {
    calls += 1;
    return "Agent memory: startup for alpha";
  });
  const output = { system: ["base prompt"] };
  await hooks[TRANSFORM]({ sessionID: "ses-1", model: {} }, output);
  await hooks[TRANSFORM]({ sessionID: "ses-1", model: {} }, output);
  assert.equal(calls, 1);
  assert.deepEqual(output.system, ["base prompt", "Agent memory: startup for alpha", "Agent memory: startup for alpha"]);
});

test("each session gets its own startup run", async () => {
  const seen = [];
  const hooks = await hooksWith(async (sid) => {
    seen.push(sid);
    return `context for ${sid}`;
  });
  const a = { system: [] };
  const b = { system: [] };
  await hooks[TRANSFORM]({ sessionID: "ses-a", model: {} }, a);
  await hooks[TRANSFORM]({ sessionID: "ses-b", model: {} }, b);
  assert.deepEqual(seen, ["ses-a", "ses-b"]);
  assert.deepEqual(a.system, ["context for ses-a"]);
  assert.deepEqual(b.system, ["context for ses-b"]);
});

test("an empty package adds nothing", async () => {
  const hooks = await hooksWith(async () => "");
  const output = { system: ["base"] };
  await hooks[TRANSFORM]({ sessionID: "ses-empty", model: {} }, output);
  assert.deepEqual(output.system, ["base"]);
});

test("a failing startup run never throws and adds nothing", async () => {
  let calls = 0;
  const hooks = await hooksWith(async () => {
    calls += 1;
    throw new Error("python missing");
  });
  const output = { system: ["base"] };
  await hooks[TRANSFORM]({ sessionID: "ses-bad", model: {} }, output);
  await hooks[TRANSFORM]({ sessionID: "ses-bad", model: {} }, output);
  assert.deepEqual(output.system, ["base"]);
  assert.equal(calls, 1);
});

test("subagent sessions get no startup context", async () => {
  let calls = 0;
  const hooks = await hooksWith(async () => {
    calls += 1;
    return "should not run";
  });
  await hooks.event({ event: { type: "session.created", properties: { info: { id: "child-1", parentID: "ses-p" } } } });
  const output = { system: [] };
  await hooks[TRANSFORM]({ sessionID: "child-1", model: {} }, output);
  assert.equal(calls, 0);
  assert.deepEqual(output.system, []);
});

test("the capture process never injects context", async () => {
  let calls = 0;
  const hooks = await hooksWith(async () => {
    calls += 1;
    return "should not run";
  });
  const previous = process.env.AGENTMEM_CAPTURE;
  process.env.AGENTMEM_CAPTURE = "1";
  try {
    const output = { system: [] };
    await hooks[TRANSFORM]({ sessionID: "ses-capture", model: {} }, output);
    assert.equal(calls, 0);
    assert.deepEqual(output.system, []);
  } finally {
    if (previous === undefined) delete process.env.AGENTMEM_CAPTURE;
    else process.env.AGENTMEM_CAPTURE = previous;
  }
});

test("parseHookOutput returns the additionalContext of a SessionStart result only", () => {
  assert.equal(parseHookOutput('{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"hi"}}'), "hi");
  assert.equal(parseHookOutput("{}"), "");
  assert.equal(parseHookOutput("not json"), "");
  assert.equal(parseHookOutput(""), "");
});
