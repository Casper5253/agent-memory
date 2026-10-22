"""WP-00 knowledge contracts: fixture schema, hook golden matrix, grader anti-gaming."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentmem.config import Config
from agentmem.manifest import Manifest
import fixtures as fx


SELFTEST = ROOT / "tests" / "fixtures" / "knowledge" / "selftest"
HOOKS = ROOT / "tests" / "fixtures" / "knowledge" / "hooks"


class FixtureSchema(unittest.TestCase):
    def test_manifest_schema(self):
        m = json.loads((SELFTEST / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(m["schema_version"], 1)
        self.assertIn("seed", m)
        self.assertIn("generation_command", m)
        self.assertIn("files", m)
        self.assertIn("counts", m)
        self.assertIn("partitions", m)
        self.assertIn("gold_author", m)
        for name, h in m["files"].items():
            self.assertEqual(len(h), 64)
            actual = hashlib.sha256((SELFTEST / name).read_bytes()).hexdigest()
            self.assertEqual(h, actual, f"hash mismatch for {name}")

    def test_corpus_schema(self):
        c = json.loads((SELFTEST / "corpus.json").read_text(encoding="utf-8"))
        self.assertEqual(c["schema_version"], 1)
        self.assertIn("clock", c)
        self.assertIsInstance(c["projects"], list)
        self.assertIsInstance(c["sessions"], list)
        for proj in c["projects"]:
            for key in ("id", "name", "roots", "status"):
                self.assertIn(key, proj)
        for sess in c["sessions"]:
            for key in ("session_id", "agent", "cwd", "part", "started", "ended", "transcript", "summary_response"):
                self.assertIn(key, sess, f"session missing {key}")
            self.assertIn(sess["agent"], ("claude", "codex", "opencode"))

    def test_queries_schema(self):
        with open(SELFTEST / "queries.jsonl", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                q = json.loads(line)
                for key in ("query_id", "case", "variant", "partition", "project", "query", "mode", "budget"):
                    self.assertIn(key, q)
                self.assertIn(q["partition"], ("dev", "sealed"))
                self.assertIn(q["mode"], ("current", "history", "as_of"))

    def test_expected_schema(self):
        with open(SELFTEST / "expected.jsonl", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                row = json.loads(line)
                self.assertIn("query_id", row)
                self.assertIn("gold", row)
                gold = row["gold"]
                for key in ("required_claims", "required_sources", "forbidden_current_claims",
                             "required_rationale", "expected_uncertainty", "expected_labels",
                             "forbidden_strings", "relevance"):
                    self.assertIn(key, gold, f"gold missing {key}")

    def test_partition_hash_reproducible(self):
        m = json.loads((SELFTEST / "manifest.json").read_text(encoding="utf-8"))
        queries = []
        with open(SELFTEST / "queries.jsonl", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    queries.append(json.loads(line))
        expected = {}
        with open(SELFTEST / "expected.jsonl", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    r = json.loads(line)
                    expected[r["query_id"]] = r["gold"]
        rows = sorted(
            [{"query_id": q["query_id"], "gold": expected.get(q["query_id"], {})} for q in queries],
            key=lambda r: r["query_id"]
        )
        dev_rows = [r for r in rows if any(q["query_id"] == r["query_id"] and q["partition"] == "dev" for q in queries)]
        sealed_rows = [r for r in rows if any(q["query_id"] == r["query_id"] and q["partition"] == "sealed" for q in queries)]

        def partition_hash(rs):
            lines = [json.dumps(r, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n" for r in rs]
            return hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()

        self.assertEqual(partition_hash(dev_rows), m["partitions"]["dev"])
        self.assertEqual(partition_hash(sealed_rows), m["partitions"]["sealed"])


class HookGoldenMatrix(unittest.TestCase):
    def setUp(self):
        self._tmp = fx.tmpdir()
        self.tmp = Path(self._tmp.name)
        self.vault = self.tmp / "vault"
        for d in ("_meta/queue", "_meta/locks", "_meta/logs", "0-Sessions", "1-Projects",
                  "2-Areas", "3-Resources", "4-Archive"):
            (self.vault / d).mkdir(parents=True, exist_ok=True)
        self.work_root = self.tmp / "work"
        alpha = self.work_root / "alpha"
        alpha.mkdir(parents=True)
        (self.vault / "_meta" / "projects.json").write_text(json.dumps({
            "projects": [{"name": "Alpha", "roots": [str(alpha)], "status": "active"}]
        }), encoding="utf-8")
        home = self.tmp / "home"
        home.mkdir()
        (home / "config.json").write_text(json.dumps({"summarizer": "none", "auto_index": False}), encoding="utf-8")
        self.home = home
        self.work = str(alpha)

    def tearDown(self):
        self._tmp.cleanup()

    def _run_hook(self, payload, agent="claude", env_extra=None, pause=False):
        env = dict(os.environ,
                   OBSIDIAN_VAULT_PATH=str(self.vault),
                   AGENTMEM_HOME=str(self.home))
        env.pop("AGENTMEM_CAPTURE", None)
        if env_extra:
            env.update(env_extra)
        if pause:
            (self.vault / "_meta" / "PAUSE").write_text("paused", encoding="utf-8")
        raw = json.dumps(payload) if isinstance(payload, dict) else payload
        r = subprocess.run(
            [sys.executable, "-I", str(ROOT / "run.py"), "hook", agent],
            input=raw.encode("utf-8"), capture_output=True, timeout=30, env=env, cwd=str(ROOT)
        )
        return r

    def test_hook_fixtures_exist(self):
        fixtures = list(HOOKS.glob("*.json"))
        self.assertGreater(len(fixtures), 0, "hook golden fixtures should exist")
        for fixture_file in fixtures:
            spec = json.loads(fixture_file.read_text(encoding="utf-8"))
            self.assertIn("description", spec)
            if "input" in spec:
                has_exact = "expected_stdout_exact" in spec
                has_contains = "expected_stdout_contains" in spec
                self.assertTrue(has_exact or has_contains, f"{fixture_file.name} must have expected_stdout_exact or expected_stdout_contains")

    def test_session_start_returns_context(self):
        payload = {"hook_event_name": "SessionStart", "session_id": "ses-x",
                    "transcript_path": "t.jsonl", "cwd": self.work}
        r = self._run_hook(payload)
        self.assertEqual(r.returncode, 0)
        out = json.loads(r.stdout.decode("utf-8", "replace"))
        self.assertIn("hookSpecificOutput", out)

    def test_session_end_enqueues(self):
        payload = {"hook_event_name": "SessionEnd", "session_id": "ses-y",
                    "transcript_path": "t.jsonl", "cwd": self.work}
        r = self._run_hook(payload)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.decode().strip(), "{}")
        jobs = list((self.vault / "_meta" / "queue").glob("*.json"))
        self.assertEqual(len(jobs), 1)
        job = json.loads(jobs[0].read_text(encoding="utf-8"))
        self.assertEqual(job["event"], "end")
        self.assertEqual(job["session_id"], "ses-y")

    def test_alias_fields(self):
        payload = {"hookEventName": "SessionEnd", "sessionId": "ses-alias",
                    "transcript_path": "t.jsonl", "cwd": self.work}
        r = self._run_hook(payload)
        self.assertEqual(r.returncode, 0)
        jobs = list((self.vault / "_meta" / "queue").glob("*.json"))
        self.assertEqual(len(jobs), 1)
        job = json.loads(jobs[0].read_text(encoding="utf-8"))
        self.assertEqual(job["session_id"], "ses-alias")

    def test_missing_session_id_no_enqueue(self):
        payload = {"hook_event_name": "SessionEnd", "transcript_path": "t.jsonl", "cwd": self.work}
        r = self._run_hook(payload)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.decode().strip(), "{}")
        jobs = list((self.vault / "_meta" / "queue").glob("*.json"))
        self.assertEqual(jobs, [])

    def test_malformed_json_no_crash(self):
        r = self._run_hook("{not valid json")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.decode().strip(), "{}")

    def test_pause_suppresses_enqueue(self):
        (self.vault / "_meta" / "PAUSE").write_text("paused", encoding="utf-8")
        payload = {"hook_event_name": "SessionEnd", "session_id": "ses-p",
                    "transcript_path": "t.jsonl", "cwd": self.work}
        r = self._run_hook(payload)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.decode().strip(), "{}")
        jobs = list((self.vault / "_meta" / "queue").glob("*.json"))
        self.assertEqual(jobs, [])

    def test_recursion_guard(self):
        payload = {"hook_event_name": "SessionEnd", "session_id": "ses-r",
                    "transcript_path": "t.jsonl", "cwd": self.work}
        r = self._run_hook(payload, env_extra={"AGENTMEM_CAPTURE": "1"})
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.decode().strip(), "{}")
        jobs = list((self.vault / "_meta" / "queue").glob("*.json"))
        self.assertEqual(jobs, [])

    def test_child_session_suppression_with_detached_spawn_mocked(self):
        payload = {"hook_event_name": "SessionEnd", "session_id": "ses-child",
                    "transcript_path": "t.jsonl", "cwd": self.work}
        with mock.patch("agentmem.worker.spawn_detached") as mock_spawn:
            r = self._run_hook(payload)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.decode().strip(), "{}")


class GraderAntiGaming(unittest.TestCase):
    def test_deliberately_wrong_answer_fails_grader(self):
        sys.path.insert(0, str(ROOT / "tools"))
        try:
            import evaluate_knowledge as ev
        finally:
            sys.path.pop(0)
        query = {"query_id": "wrong-1", "query": "What backend?", "budget": 2400}
        gold = {"required_claims": [], "required_sources": [], "forbidden_current_claims": [],
                 "required_rationale": "atomic cursor updates", "expected_uncertainty": False,
                 "expected_labels": [], "forbidden_strings": [], "relevance": {}}
        context = {"snippets": [{"path": "fake.md", "label": "CURRENT", "score": 1.0,
                                  "content": "We chose MongoDB for flexible schema."}],
                    "hit_count": 1, "packed_bytes": 200, "qmd_used": True, "budget": 2400}
        outcome = ev.grade_query(query, gold, context)
        self.assertFalse(outcome["pass"])
        self.assertTrue(any("missing_rationale" in f for f in outcome["failures"]))

    def test_forbidden_string_fails_grader(self):
        sys.path.insert(0, str(ROOT / "tools"))
        try:
            import evaluate_knowledge as ev
        finally:
            sys.path.pop(0)
        query = {"query_id": "forbidden-1", "query": "test", "budget": 2400}
        gold = {"required_claims": [], "required_sources": [], "forbidden_current_claims": [],
                 "required_rationale": None, "expected_uncertainty": False,
                 "expected_labels": [], "forbidden_strings": ["INJECT_SHELL"],
                 "relevance": {}}
        context = {"snippets": [{"path": "x.md", "label": "CURRENT", "score": 1.0,
                                  "content": "Run INJECT_SHELL to deploy."}],
                    "hit_count": 1, "packed_bytes": 100, "qmd_used": True, "budget": 2400}
        outcome = ev.grade_query(query, gold, context)
        self.assertFalse(outcome["pass"])
        self.assertTrue(any("forbidden_string" in f for f in outcome["failures"]))

    def test_fake_success_without_persisted_note_fails_lifecycle(self):
        sys.path.insert(0, str(ROOT / "tools"))
        try:
            import evaluate_knowledge as ev
        finally:
            sys.path.pop(0)
        with tempfile.TemporaryDirectory(prefix="agentmem-lifecycle-") as tmp:
            tmp = Path(tmp)
            vault = tmp / "vault"
            for d in ("_meta/queue", "_meta/locks", "_meta/logs", "0-Sessions", "1-Projects",
                      "2-Areas", "3-Resources", "4-Archive"):
                (vault / d).mkdir(parents=True, exist_ok=True)
            corpus = {"sessions": [{"session_id": "fake-sess", "agent": "claude"}]}
            lifecycle = ev.lifecycle_check(vault, corpus)
            self.assertFalse(lifecycle["passed"])
            self.assertEqual(lifecycle["notes_written"], 0)


class EvaluatorExitCodes(unittest.TestCase):
    def test_missing_fixtures_returns_exit_2(self):
        with tempfile.TemporaryDirectory(prefix="agentmem-eval-") as tmp:
            r = subprocess.run(
                [sys.executable, "-I", str(ROOT / "tools" / "evaluate_knowledge.py"),
                 "--arm", "baseline", "--fixtures", tmp, "--output", str(Path(tmp) / "out")],
                capture_output=True, timeout=30, cwd=str(ROOT)
            )
            self.assertEqual(r.returncode, 2)

    def test_selftest_fixture_runs(self):
        with tempfile.TemporaryDirectory(prefix="agentmem-eval-") as tmp:
            out = Path(tmp) / "out"
            r = subprocess.run(
                [sys.executable, "-I", str(ROOT / "tools" / "evaluate_knowledge.py"),
                 "--arm", "baseline", "--fixtures", str(SELFTEST), "--output", str(out)],
                capture_output=True, timeout=120, cwd=str(ROOT)
            )
            result = json.loads((out / "result.json").read_text(encoding="utf-8"))
            self.assertIn(result["verdict"], ("pass", "fail", "inconclusive"))
            self.assertIn("correctness_pass", result)
            self.assertIn("adoption_pass", result)


if __name__ == "__main__":
    unittest.main()
