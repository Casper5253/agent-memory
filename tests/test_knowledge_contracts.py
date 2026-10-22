"""WP-00 knowledge contracts: fixture schema, hook golden matrix, grader anti-gaming, evaluator isolation."""
import hashlib
import io
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
from agentmem.hook import main as hook_main
from agentmem.manifest import Manifest
import fixtures as fx


SELFTEST = ROOT / "tests" / "fixtures" / "knowledge" / "selftest"
HOOKS = ROOT / "tests" / "fixtures" / "knowledge" / "hooks"


def _replace_paths(obj, old, new):
    if isinstance(obj, str):
        return obj.replace(old, new)
    if isinstance(obj, list):
        return [_replace_paths(x, old, new) for x in obj]
    if isinstance(obj, dict):
        return {k: _replace_paths(v, old, new) for k, v in obj.items()}
    return obj


def _normalize_separators(obj):
    if isinstance(obj, str):
        return obj.replace("\\", "/")
    if isinstance(obj, list):
        return [_normalize_separators(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _normalize_separators(v) for k, v in obj.items()}
    return obj


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

    def _run_hook_in_process(self, spec):
        env = dict(os.environ, OBSIDIAN_VAULT_PATH=str(self.vault), AGENTMEM_HOME=str(self.home))
        env.pop("AGENTMEM_CAPTURE", None)
        env.update(spec.get("env", {}))
        if spec.get("pause"):
            (self.vault / "_meta" / "PAUSE").write_text("paused", encoding="utf-8")

        if "input_raw" in spec:
            raw = spec["input_raw"]
        else:
            payload = dict(spec.get("input", {}))
            if "cwd" in payload and isinstance(payload["cwd"], str):
                payload["cwd"] = payload["cwd"].replace("{WORK}", self.work)
            raw = json.dumps(payload)

        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch("agentmem.worker.spawn_detached"):
                with mock.patch("sys.stdin", io.StringIO(raw)):
                    out = io.StringIO()
                    with mock.patch("sys.stdout", out):
                        rc = hook_main("claude")
        self.assertEqual(rc, 0)
        actual_stdout = json.loads(out.getvalue())
        actual_stdout = _replace_paths(actual_stdout, str(self.vault), "{VAULT}")
        actual_stdout = _replace_paths(actual_stdout, str(self.work_root), "{WORK}")
        actual_stdout = _normalize_separators(actual_stdout)
        jobs = []
        for job in sorted((self.vault / "_meta" / "queue").glob("*.json")):
            j = json.loads(job.read_text(encoding="utf-8"))
            jobs.append({"event": j["event"], "session_id": j["session_id"], "agent": j["agent"]})
        return actual_stdout, jobs

    def test_hook_fixtures_exist(self):
        fixtures = list(HOOKS.glob("*.json"))
        self.assertGreater(len(fixtures), 0, "hook golden fixtures should exist")
        for fixture_file in fixtures:
            spec = json.loads(fixture_file.read_text(encoding="utf-8"))
            self.assertIn("description", spec)
            self.assertIn("golden", spec, f"{fixture_file.name} must have a golden output")

    def test_hook_golden_matrix(self):
        for fixture_file in sorted(HOOKS.glob("*.json")):
            with self.subTest(fixture=fixture_file.name):
                # Each fixture runs against a clean queue and pause switch.
                for job in (self.vault / "_meta" / "queue").glob("*.json"):
                    job.unlink()
                pause_file = self.vault / "_meta" / "PAUSE"
                if pause_file.exists():
                    pause_file.unlink()
                spec = json.loads(fixture_file.read_text(encoding="utf-8"))
                golden = spec["golden"]
                actual_stdout, jobs = self._run_hook_in_process(spec)
                self.assertEqual(actual_stdout, golden["stdout"],
                                 f"stdout mismatch for {fixture_file.name}")
                self.assertEqual(jobs, golden["queue"],
                                 f"queue mismatch for {fixture_file.name}")


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

    def test_partial_note_count_fails_lifecycle(self):
        sys.path.insert(0, str(ROOT / "tools"))
        try:
            import evaluate_knowledge as ev
        finally:
            sys.path.pop(0)
        with tempfile.TemporaryDirectory(prefix="agentmem-lifecycle-") as tmp:
            tmp = Path(tmp)
            vault = tmp / "vault"
            for d in ("_meta/queue", "_meta/locks", "_meta/logs", "0-Sessions", "1-Projects"):
                (vault / d).mkdir(parents=True, exist_ok=True)
            # Record one capture while the corpus expects two session parts.
            man = Manifest(vault / "_meta" / "manifest.sqlite")
            try:
                man.db.execute("INSERT INTO captures(agent, session_id, part, note_path, project, title, date, summarizer, consolidated, captured_at) VALUES (?,?,1,'0-Sessions/fake.md','Alpha','fake','2026-10-01','none',1,?)", ("claude", "fake-sess", time.time()))
            finally:
                man.close()
            corpus = {"sessions": [
                {"session_id": "fake-sess", "agent": "claude"},
                {"session_id": "fake-sess2", "agent": "claude"},
            ]}
            lifecycle = ev.lifecycle_check(vault, corpus)
            self.assertFalse(lifecycle["passed"])
            self.assertEqual(lifecycle["notes_written"], 1)
            self.assertEqual(lifecycle["expected_sessions"], 2)


class EvaluatorIsolation(unittest.TestCase):
    def test_temp_qmd_index_and_subprocess_env(self):
        from agentmem.config import Config as AgentMemConfig
        sys.path.insert(0, str(ROOT / "tools"))
        try:
            import evaluate_knowledge as ev
        finally:
            sys.path.pop(0)
        with tempfile.TemporaryDirectory(prefix="agentmem-eval-iso-") as tmp:
            tmp = Path(tmp)
            corpus = {"projects": [{"id": "alpha", "name": "Alpha", "roots": ["{WORK}/alpha"],
                                    "status": "active", "policy": "model_allowed"}],
                      "sessions": []}
            qmd_index = tmp / "qmd_index"
            vault, work, home, _ = ev.make_temp_vault(tmp, corpus, qmd_index=qmd_index)

            # In-process Config must use the temp QMD index, not the shared default.
            cfg = AgentMemConfig(vault=vault, summarizer="none", settle_minutes=0,
                                 auto_index=False, embeddings=False, qmd_index=str(qmd_index))
            self.assertTrue(Path(cfg.qmd_index).resolve().is_relative_to(tmp.resolve()))
            self.assertNotEqual(cfg.qmd_index, "")

            # Subprocess config.json must point at the temp index too.
            config = json.loads((home / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(config.get("qmd_index"), str(qmd_index))

            # run_mem_command must send only temp paths and drop the recursion guard.
            captured = {}
            def fake_run(cmd, **kwargs):
                captured["env"] = kwargs.get("env", {})
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=b"[]", stderr=b"")

            with mock.patch("subprocess.run", fake_run):
                ev.run_mem_command(vault, home, ["search", "test", "--json", "-n", "8"])
            env = captured["env"]
            self.assertIn(str(tmp), env["OBSIDIAN_VAULT_PATH"])
            self.assertIn(str(tmp), env["AGENTMEM_HOME"])
            self.assertNotIn("AGENTMEM_CAPTURE", env)


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
