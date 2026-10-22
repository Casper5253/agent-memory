"""Real Claude/Codex hook contracts, including mutation checks of the harness."""
import io
import json
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agentmem import hook, worker

FIXTURES = ROOT / "tests/fixtures/knowledge/hook_golden"
CASES = frozenset({"session_start", "session_end", "stop", "precompact", "aliases", "missing_id",
                   "malformed", "pause", "recursion", "child"})
PROVIDERS = ("claude", "codex")


def replace(value, vault, work):
    if isinstance(value, str):
        return value.replace("{VAULT}", str(vault)).replace("{WORK}", str(work))
    if isinstance(value, list):
        return [replace(v, vault, work) for v in value]
    if isinstance(value, dict):
        return {k: replace(v, vault, work) for k, v in value.items()}
    return value


def normalize(value, vault, work):
    if isinstance(value, str):
        return value.replace(str(vault), "{VAULT}").replace(str(work), "{WORK}").replace("\\", "/")
    if isinstance(value, list):
        return [normalize(v, vault, work) for v in value]
    if isinstance(value, dict):
        return {k: normalize(v, vault, work) for k, v in value.items()}
    return value


def load_cases(directory=FIXTURES):
    rows = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(directory.glob("*.json"))]
    keys = [(row["provider"], row["case"]) for row in rows]
    required = {(provider, case) for provider in PROVIDERS for case in CASES}
    if len(keys) != len(required) or set(keys) != required:
        raise AssertionError("missing, duplicate or unexpected required hook cases")
    return rows


class KnowledgeHooks(unittest.TestCase):
    def spec(self, case, provider="claude"):
        return next(row for row in load_cases() if row["case"] == case and row["provider"] == provider)

    def assert_hook(self, spec, entry=hook.main):
        scratch = ROOT / ".audit/hook-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as folder:
            base = Path(folder)
            vault, work, home = base / "vault", base / "work", base / "home"
            for directory in (vault / "_meta/queue", vault / "_meta/locks", vault / "_meta/logs", vault / "0-Sessions",
                              vault / "1-Projects", work / "alpha", home):
                directory.mkdir(parents=True, exist_ok=True)
            (vault / "_meta/projects.json").write_text(json.dumps({"projects": [
                {"name": "Alpha", "roots": [str(work / "alpha")], "status": "active"}]}))
            (home / "config.json").write_text(json.dumps({"summarizer": "none", "auto_index": False}))
            if spec.get("pause"):
                (vault / "_meta/PAUSE").write_text("paused")
            environment = dict(os.environ, OBSIDIAN_VAULT_PATH=str(vault), AGENTMEM_HOME=str(home))
            environment.pop("AGENTMEM_CAPTURE", None)
            environment.update(spec.get("env", {}))
            raw = spec.get("input_raw")
            if raw is None:
                raw = json.dumps(replace(spec["input"], vault, work))
            output = io.StringIO()
            with mock.patch.dict(os.environ, environment, clear=True), mock.patch("sys.stdin", io.StringIO(raw)), \
                    mock.patch("sys.stdout", output), mock.patch("agentmem.worker.spawn_detached") as spawn:
                self.assertEqual(entry(spec["provider"]), 0)
            try:
                stdout = json.loads(output.getvalue())
            except ValueError:
                self.fail("hook stdout is not exactly one JSON result")
            self.assertEqual(normalize(stdout, vault, work), spec["golden"]["stdout"])
            self.assertEqual(spawn.call_count, spec["golden"]["spawn_calls"])
            if spawn.called:
                spawn.assert_called_once_with()
            jobs = []
            for path in sorted((vault / "_meta/queue").iterdir()):
                self.assertEqual(path.suffix, ".json")
                job = json.loads(path.read_text())
                self.assertEqual(set(job), {"event", "agent", "session_id", "source", "cwd", "ts"})
                self.assertIsInstance(job["ts"], (int, float))
                self.assertTrue(math.isfinite(job["ts"]))
                job["ts"] = "{TIME}"
                jobs.append(normalize(job, vault, work))
            self.assertEqual(jobs, spec["golden"]["queue"])

    def test_real_hook_stdout_queue_and_worker_contract_for_every_required_case(self):
        for spec in load_cases():
            with self.subTest(provider=spec["provider"], case=spec["case"]):
                self.assert_hook(spec)

    def test_required_case_set_cannot_shrink_to_an_inert_hook(self):
        scratch = ROOT / ".audit/hook-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            (Path(directory) / "child.json").write_text(json.dumps(self.spec("child")))
            with self.assertRaisesRegex(AssertionError, "required hook cases"):
                load_cases(Path(directory))

    def test_noop_hook_is_caught_for_both_providers(self):
        def inert(provider):
            sys.stdout.write("{}")
            return 0
        for provider in PROVIDERS:
            with self.subTest(provider=provider), self.assertRaises(AssertionError):
                self.assert_hook(self.spec("session_end", provider), inert)

    def test_corrupted_queue_source_and_cwd_are_caught(self):
        enqueue = worker.enqueue
        def corrupt(cfg, agent, sid, *, event, source="", cwd=""):
            return enqueue(cfg, agent, sid, event=event, source="", cwd="")
        for provider in PROVIDERS:
            with self.subTest(provider=provider), mock.patch("agentmem.worker.enqueue", corrupt), self.assertRaises(AssertionError):
                self.assert_hook(self.spec("session_end", provider))

    def test_missing_worker_launch_is_caught(self):
        source = Path(hook.__file__).read_text(encoding="utf-8")
        self.assertIn("        worker.spawn_detached()", source)
        namespace = {"__name__": "agentmem.hook", "__package__": "agentmem"}
        exec(compile(source.replace("        worker.spawn_detached()", "        pass"), "hook-without-worker", "exec"), namespace)
        for provider in PROVIDERS:
            with self.subTest(provider=provider), self.assertRaises(AssertionError):
                self.assert_hook(self.spec("session_end", provider), namespace["main"])

    def test_disabled_codex_route_is_caught(self):
        def no_codex(provider):
            if provider == "codex":
                sys.stdout.write("{}")
                return 0
            return hook.main(provider)
        with self.assertRaises(AssertionError):
            self.assert_hook(self.spec("session_end", "codex"), no_codex)

    def test_disabled_child_suppression_is_caught(self):
        def captures_child(provider):
            raw = sys.stdin.read().replace("SubagentStop", "SessionEnd")
            with mock.patch("sys.stdin", io.StringIO(raw)):
                return hook.main(provider)
        for provider in PROVIDERS:
            with self.subTest(provider=provider), self.assertRaises(AssertionError):
                self.assert_hook(self.spec("child", provider), captures_child)

    def test_disabled_pause_and_recursion_guards_are_caught(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider, guard="pause"), mock.patch("agentmem.config.Config.paused", return_value=False), self.assertRaises(AssertionError):
                self.assert_hook(self.spec("pause", provider))
            def recurses(agent):
                with mock.patch.dict(os.environ, {"AGENTMEM_CAPTURE": ""}):
                    return hook.main(agent)
            with self.subTest(provider=provider, guard="recursion"), self.assertRaises(AssertionError):
                self.assert_hook(self.spec("recursion", provider), recurses)


if __name__ == "__main__":
    unittest.main()
