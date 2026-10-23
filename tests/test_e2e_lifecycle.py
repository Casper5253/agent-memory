import copy
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import e2e_lifecycle as e2e


def page(truth):
    return ("<!-- agentmem:state:begin -->\n### Current state\nCurrent storage format: " + truth.storage
            + "; rationale: " + truth.reason + ".\n### Active decisions\nUse " + truth.storage + " storage.\n"
            + "### Superseded decisions\n- prior historical JSON choice\n### Open questions and next steps\n"
            + truth.next + "\n" + "\n".join(truth.open) + "\n<!-- agentmem:state:end -->\n")


def answer(truth):
    return json.dumps(dict(current_storage=truth.storage, rationale=truth.reason,
                           next_action=truth.next, open_work=truth.open, prior_decisions=truth.previous))


class FakeSandbox:
    def __init__(self, output, scenario):
        self.output = output
        self.project = output / "project"
        self.project.mkdir(parents=True)
        self.code_root = output / "unused"
        self.scenario = scenario
        self.truth = e2e.Truth(**scenario["initial"])
        self.memory = copy.deepcopy(self.truth)
        self.ids = set()
        self.notes = {"1-Projects/Test.md": page(self.memory)}
        self.rows = dict(sessions=[], captures=[])
        self.head = 0
        e2e.write_json(self.project / "storage.json", dict(backend=self.truth.storage, reason=self.truth.reason))

    def snapshot(self):
        return copy.deepcopy(dict(notes=self.notes, status=page(self.memory), manifest=self.rows,
                                 repo=dict(head=str(self.head), branch="fixture", dirty="", storage=e2e.read_json(self.project / "storage.json"),
                                           files={p.name:p.read_text() for p in self.project.glob("*.txt")})))

    def settle(self, directory, timeout=120):
        return dict(completed=True, timeout=False)

    def commit_step(self, step):
        self.head += 1

    def drain(self, directory):
        return dict(returncode=0, time_aged=False)


class FakeAgent:
    def __init__(self, empty_context=False):
        self.calls = 0
        self.empty_context = empty_context

    def run(self, sandbox, step, agent, directory):
        directory.mkdir(parents=True)
        self.calls += 1
        sid = "synthetic-" + str(self.calls)
        context = "" if self.empty_context else page(sandbox.memory)
        events = [dict(event="SessionStart", status="forwarded", payload=dict(session_id=sid))]
        response = answer(sandbox.memory) if context else "{}"
        if step["kind"] == "work":
            target = copy.deepcopy(sandbox.truth)
            target.advance(step)
            e2e.write_json(sandbox.project / "storage.json", dict(backend=target.storage, reason=target.reason))
            (sandbox.project / "decision.txt").write_text(target.reason)
            (sandbox.project / "open-work.txt").write_text("\n".join([target.next,*target.open]))
            response = answer(target)
            end = step.get("delivery") != "no_end"
            events += [dict(event="Stop", status="forwarded"), dict(event="SessionEnd", status="forwarded" if end else "withheld")]
            sandbox.rows["sessions"].append(dict(agent=agent, session_id=sid, status="captured" if end else "pending", end_seen=0))
            if end:
                old = copy.deepcopy(sandbox.memory)
                sandbox.memory = target
                sandbox.notes["1-Projects/Test.md"] = page(target)
                path = "0-Sessions/" + sid + ".md"
                sandbox.notes[path] = page(target) + "\nPrior: " + answer(old)
                sandbox.rows["captures"].append(dict(agent=agent, session_id=sid, part=1, note_path=path))
        if step["kind"] == "kill":
            for name in ("interrupted-status.txt", "interrupted-decision.txt", "interrupted-next.txt"):
                (sandbox.project / name).write_text("interrupted test-plan work remains unfinished")
            response = ""
        return e2e.Observation(agent, sid, context, response, events, -9 if step["kind"] == "kill" else 0,
                               killed=step["kind"] == "kill")


class LifecycleHarness(unittest.TestCase):
    def setUp(self):
        base = ROOT / ".audit/tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=base, prefix="p4-test-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.scenario = e2e.read_json(ROOT / "tests/fixtures/e2e/lifecycle.json")

    def test_fake_full_scenario_runs_without_models_and_records_failure_cases(self):
        sandbox, agent = FakeSandbox(self.path, self.scenario), FakeAgent()
        result = e2e.run_scenario(sandbox, agent, ["claude", "codex"])
        self.assertTrue(result["complete"])
        self.assertEqual((agent.calls, result["primary_invocations"], result["capture_llm_starts"]), (6, 6, 0))
        scores = {s["step"]:s for s in result["scores"]}
        self.assertTrue(scores["01-start"]["reply"]["status_correct"])
        self.assertTrue(scores["04-inherit"]["reply"]["status_correct"])
        self.assertTrue(scores["02-decision"]["commit_created"])
        self.assertTrue(scores["05-reverse"]["decision_retained"])
        self.assertTrue(scores["07-before-stop"]["killed_before_first_stop"])
        self.assertFalse(scores["07-before-stop"]["session_registered"])
        self.assertFalse(scores["08-unaged-drain"]["unaged_no_end_recovered"])
        self.assertFalse(e2e.read_json(self.path / "08-unaged-drain/drain.json")["time_aged"])
        self.assertEqual(len(list(self.path.glob("*/score.json"))), 8)
        self.assertTrue((self.path / "06-unfinished/status.diff").exists())

    def test_empty_delivered_context_is_quality_failure_not_setup_error(self):
        scenario = dict(self.scenario, steps=[self.scenario["steps"][0]])
        result = e2e.run_scenario(FakeSandbox(self.path, scenario), FakeAgent(empty_context=True), ["claude"])
        self.assertFalse(result["scores"][0]["reply"]["status_correct"])
        self.assertFalse(result["scores"][0]["injected"])

    def test_empty_wrong_stale_and_uncertain_answers_fail(self):
        truth = e2e.Truth("sqlite", "atomic updates", "add malformed-input coverage", previous=[dict(storage="json", reason="readable fixtures")])
        self.assertTrue(e2e.score_reply(answer(truth), truth)["status_correct"])
        for text in ("", "{}", json.dumps(dict(current_storage="unknown")), answer(e2e.Truth("json", "readable fixtures", "choose transactional storage"))):
            self.assertFalse(e2e.score_reply(text, truth)["status_correct"])
        self.assertEqual(e2e.score_reply(answer(e2e.Truth("json", "readable fixtures", "old")), truth)["stale_as_current"], 1)

    def test_current_scoring_ignores_superseded_but_keeps_next_steps(self):
        truth = e2e.Truth("sqlite", "atomic updates", "add malformed-input coverage")
        self.assertTrue(e2e.score_status(page(truth), truth)["status_correct"])
        self.assertEqual(e2e.score_status(page(truth), truth)["stale_as_current"], 0)

    def test_filename_mention_does_not_prove_storage_choice(self):
        truth = e2e.Truth("json", "readable fixtures", "choose transactional storage")
        score = e2e.score_status("A file storage.json exists. readable fixtures. choose transactional storage.", truth)
        self.assertFalse(score["status_correct"])
        self.assertIn("current_storage", score["missing_items"])

    def test_hook_shell_paths_survive_bash_and_keep_spaces_quoted(self):
        command = e2e.hook_command([r"C:\Program Files\Python\python.exe", "-I", r"C:\sandbox\runner.py"])
        self.assertEqual(command, '"C:/Program Files/Python/python.exe" "-I" "C:/sandbox/runner.py"')

    def test_claude_prompt_is_not_consumed_by_variadic_tools_option(self):
        sandbox = FakeSandbox(self.path, self.scenario)
        sandbox.environment = lambda *args: {}
        process = mock.Mock()
        process.poll.return_value = 0
        process.wait.return_value = 0
        backend = e2e.NativeAgent(claude="fake")
        with mock.patch.object(e2e.subprocess, "Popen", return_value=process) as popen:
            observation = backend.run(sandbox, self.scenario["steps"][0], "claude", self.path / "session")
        command = popen.call_args.args[0]
        self.assertGreater(command.index("--permission-mode"), command.index("--tools"))
        self.assertEqual(command[-1], e2e.prompt_for(self.scenario["steps"][0], sandbox.truth))

    def test_work_requires_a_real_edit_and_all_obligations(self):
        sandbox = FakeSandbox(self.path, self.scenario)
        step = self.scenario["steps"][1]
        before = sandbox.snapshot()
        self.assertFalse(e2e.work_completed(step, sandbox.truth, before, before))
        FakeAgent().run(sandbox, step, "codex", self.path / "work")
        self.assertTrue(e2e.work_completed(step, sandbox.truth, before, sandbox.snapshot()))
        (sandbox.project / "open-work.txt").write_text("nothing done")
        self.assertFalse(e2e.work_completed(step, sandbox.truth, before, sandbox.snapshot()))

    def test_work_tools_allow_read_before_overwrite(self):
        sandbox = FakeSandbox(self.path, self.scenario)
        sandbox.environment = lambda *args: {}
        process = mock.Mock()
        process.poll.return_value = 0
        process.wait.return_value = 0
        with mock.patch.object(e2e.subprocess, "Popen", return_value=process) as popen:
            e2e.NativeAgent(claude="fake").run(sandbox, self.scenario["steps"][1], "claude", self.path / "session")
        command = popen.call_args.args[0]
        self.assertEqual(command[command.index("--tools")+1], "Read,Write")

    def test_active_old_storage_is_a_staleness_error(self):
        truth = e2e.Truth("sqlite", "atomic updates", "add malformed-input coverage")
        score = e2e.score_status(page(truth).replace("### Active decisions", "Use JSON storage.\n### Active decisions"), truth)
        self.assertFalse(score["status_correct"])
        self.assertEqual(score["stale_as_current"], 1)

    def test_negated_or_hypothetical_storage_does_not_earn_credit(self):
        truth = e2e.Truth("sqlite", "atomic updates", "add malformed-input coverage")
        for claim in ("We do not use SQLite storage", "We may use SQLite storage", "We might use SQLite storage"):
            score = e2e.score_status(claim + ". atomic updates. add malformed-input coverage.", truth)
            self.assertFalse(score["status_correct"], claim)
        obj = json.loads(answer(truth))
        obj["rationale"] = "not atomic updates"
        self.assertFalse(e2e.score_reply(json.dumps(obj), truth)["status_correct"])

    def test_kill_after_stop_is_not_credited_as_before_stop(self):
        sandbox = FakeSandbox(self.path, self.scenario)
        snap = sandbox.snapshot()
        obs = e2e.Observation("codex", "test", "context", "", [dict(event="Stop")], -9, killed=True)
        score = e2e.evaluate_step(dict(kind="kill"), sandbox.truth, obs, snap, snap)
        self.assertFalse(score["killed_before_first_stop"])

    def test_live_guard_reads_only_synthetic_fixture_and_finds_contamination(self):
        vault = self.path / "live"
        (vault / "_meta").mkdir(parents=True)
        with closing(sqlite3.connect(vault / "_meta/manifest.sqlite")) as db:
            db.execute("CREATE TABLE sessions(session_id TEXT,cwd TEXT)")
            db.execute("CREATE TABLE captures(session_id TEXT)")
            db.execute("INSERT INTO sessions VALUES ('probe-session','somewhere')")
            db.commit()
        (vault / "one.md").write_text("probe-session")
        config = self.path / "runtime/config.json"
        e2e.write_json(config, dict(vault=str(vault)))
        result = e2e.live_guard({"probe-session"}, self.path / "project", config)
        self.assertEqual((result["matching_rows"], result["matching_notes"]), (1, 1))
        self.assertNotIn("probe-session", json.dumps(result["metadata"]))

    def fake_app(self):
        root = self.path / "code"
        package = root / "agentmem"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (package / "llm.py").write_text("def _run(*args, **kwargs): return {'same': 'value'}\n")
        (package / "worker.py").write_text("def spawn_detached(extra_args=()): raise AssertionError('not used')\n")
        (package / "cli.py").write_text('''import json,os,sys
from pathlib import Path
from . import llm
def main(argv):
    if argv[0]=='worker':
        assert llm._run('system','prompt','fake',1)=={'same':'value'}
        print('{}');return 0
    payload=json.loads(sys.stdin.read())
    assert 'AGENTMEM_CAPTURE' not in os.environ
    print(json.dumps({'hookSpecificOutput':{'hookEventName':payload['hook_event_name'],'additionalContext':str(Path(__file__).parent.parent)}}))
    return 0
''')
        return root

    def sandbox_env(self, **overrides):
        return dict(os.environ, E2E_ROOT=str(self.path), AGENTMEM_HOME=str(self.path / "sandbox/runtime"),
                    OBSIDIAN_VAULT_PATH=str(self.path / "sandbox/vault"), **overrides)

    def test_internal_mode_cannot_default_to_live_configuration(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(e2e, "app_entry") as app:
            with self.assertRaises(SystemExit):
                e2e.main(["app", "--code-root", str(self.path), "worker"])
        app.assert_not_called()

    def test_native_proxy_forwards_selected_root_and_retains_payload(self):
        code = self.fake_app()
        step = self.path / "step"
        payload = dict(hook_event_name="SessionStart", session_id="synthetic", cwd=str(self.path))
        env = self.sandbox_env(E2E_STEP=str(step), AGENTMEM_CAPTURE="1")
        run = subprocess.run([sys.executable, "-B", "-I", str(ROOT / "tools/e2e_lifecycle.py"), "hook", "--code-root", str(code), "--agent", "codex"], input=json.dumps(payload).encode(), capture_output=True, env=env)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)["hookSpecificOutput"]["additionalContext"], str(code))
        record = e2e.read_json(next((step / "hooks").glob("*.json")))
        self.assertEqual(record["payload"], payload)
        self.assertEqual(record["status"], "forwarded")

    def test_no_end_withheld_explicitly_without_forging_clock(self):
        code = self.fake_app()
        step = self.path / "step"
        payload = dict(hook_event_name="SessionEnd", session_id="synthetic")
        env = self.sandbox_env(E2E_STEP=str(step), E2E_DELIVERY="no_end")
        run = subprocess.run([sys.executable, "-B", "-I", str(ROOT / "tools/e2e_lifecycle.py"), "hook", "--code-root", str(code), "--agent", "claude"], input=json.dumps(payload).encode(), capture_output=True, env=env)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout), {})
        record = e2e.read_json(next((step / "hooks").glob("*.json")))
        self.assertEqual(record["status"], "withheld")
        self.assertEqual(record["payload"], payload)

    def test_capture_model_observer_counts_calls_without_changing_return_value(self):
        code = self.fake_app()
        env = self.sandbox_env()
        run = subprocess.run([sys.executable, "-B", "-I", str(ROOT / "tools/e2e_lifecycle.py"), "app", "--code-root", str(code), "worker"], capture_output=True, env=env)
        self.assertEqual(run.returncode, 0, run.stderr)
        calls = [r for p in (self.path / "model-calls").glob("*.jsonl") for r in e2e.records(p)]
        self.assertEqual([r["kind"] for r in calls], ["start", "complete"])
        self.assertEqual(calls[0]["id"], calls[1]["id"])

    def test_real_sandbox_config_and_git_commit_without_any_agent(self):
        sandbox = e2e.Sandbox(self.path / "real", ROOT, self.scenario)
        sandbox.prepare("none")
        before = sandbox.snapshot()
        step = self.scenario["steps"][1]
        e2e.write_json(sandbox.project / "storage.json", dict(backend="sqlite", reason="atomic updates"))
        (sandbox.project / "decision.txt").write_text("Use SQLite for atomic updates.")
        (sandbox.project / "open-work.txt").write_text("add malformed-input coverage")
        sandbox.commit_step(step)
        after = sandbox.snapshot()
        self.assertNotEqual(before["repo"]["head"], after["repo"]["head"])
        self.assertEqual(after["repo"]["storage"]["backend"], "sqlite")
        self.assertFalse(after["repo"]["dirty"])


if __name__ == "__main__":
    unittest.main()
