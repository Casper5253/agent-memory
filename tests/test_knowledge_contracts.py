"""WP-00 knowledge contracts: fixture schema, hook golden matrix, grader anti-gaming, evaluator isolation."""
import hashlib
import io
import json
import os
import shutil
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


def _repo_tmp():
    tmp = ROOT / ".audit" / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    return tmp


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
    def grade(self, ev, query, gold, context):
        with tempfile.TemporaryDirectory(dir=str(_repo_tmp())) as directory:
            vault = Path(directory)
            manifest = Manifest(vault / "_meta/manifest.sqlite")
            manifest.close()
            return ev.grade_query(dict(query, project="alpha"), gold, context,
                                  corpus={"projects": [{"id": "alpha", "name": "Alpha"}]}, vault=vault)

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
        outcome = self.grade(ev, query, gold, context)
        self.assertFalse(outcome["pass"])
        self.assertIn("missing_supported_rationale", outcome["failures"])

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
        outcome = self.grade(ev, query, gold, context)
        self.assertFalse(outcome["pass"])
        self.assertTrue(any("forbidden_string" in f for f in outcome["failures"]))

    def test_fake_success_without_persisted_note_fails_lifecycle(self):
        sys.path.insert(0, str(ROOT / "tools"))
        try:
            import evaluate_knowledge as ev
        finally:
            sys.path.pop(0)
        with tempfile.TemporaryDirectory(prefix="agentmem-lifecycle-", dir=str(_repo_tmp())) as tmp:
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
        with tempfile.TemporaryDirectory(prefix="agentmem-lifecycle-", dir=str(_repo_tmp())) as tmp:
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
            self.assertEqual(lifecycle["notes_written"], 0)
            self.assertEqual(lifecycle["captures_recorded"], 1)
            self.assertEqual(lifecycle["missing_note_files"], 1)

    def test_colliding_note_paths_fail_lifecycle(self):
        sys.path.insert(0, str(ROOT / "tools"))
        try:
            import evaluate_knowledge as ev
        finally:
            sys.path.pop(0)
        with tempfile.TemporaryDirectory(prefix="agentmem-lifecycle-", dir=str(_repo_tmp())) as tmp:
            vault = Path(tmp) / "vault"
            (vault / "0-Sessions").mkdir(parents=True)
            (vault / "_meta").mkdir()
            (vault / "0-Sessions" / "shared.md").write_text("note", encoding="utf-8")
            man = Manifest(vault / "_meta" / "manifest.sqlite")
            try:
                for sid in ("aaaa-1234abcd", "bbbb-1234abcd"):
                    man.db.execute("INSERT INTO captures(agent, session_id, part, note_path, project, title, date, summarizer, consolidated, captured_at) VALUES (?,?,1,'0-Sessions/shared.md','Alpha','t','2026-10-01','none',1,?)", ("claude", sid, time.time()))
            finally:
                man.close()
            corpus = {"sessions": [{"session_id": "aaaa-1234abcd", "agent": "claude"},
                                   {"session_id": "bbbb-1234abcd", "agent": "claude"}]}
            lifecycle = ev.lifecycle_check(vault, corpus)
            self.assertFalse(lifecycle["passed"])
            self.assertEqual(lifecycle["notes_written"], 0)
            self.assertEqual(lifecycle["colliding_captures"], 1)
            self.assertEqual(lifecycle["expected_sessions"], 2)


class EvaluatorIsolation(unittest.TestCase):
    def test_temp_qmd_index_and_subprocess_env(self):
        from agentmem.config import Config as AgentMemConfig
        sys.path.insert(0, str(ROOT / "tools"))
        try:
            import evaluate_knowledge as ev
        finally:
            sys.path.pop(0)
        with tempfile.TemporaryDirectory(prefix="agentmem-eval-iso-", dir=str(_repo_tmp())) as tmp:
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

            # run_mem_command must send only temp paths, drop the recursion guard, and
            # keep QMD's named index cache inside the temp dir.
            captured = {}
            def fake_run(cmd, **kwargs):
                captured["env"] = kwargs.get("env", {})
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=b"[]", stderr=b"")

            with mock.patch("subprocess.run", fake_run):
                ev.run_mem_command(vault, home, ["search", "test", "--json", "-n", "8"],
                                   cwd=tmp, cache_dir=tmp / "cache")
            env = captured["env"]
            self.assertIn(str(tmp), env["OBSIDIAN_VAULT_PATH"])
            self.assertIn(str(tmp), env["AGENTMEM_HOME"])
            self.assertNotIn("AGENTMEM_CAPTURE", env)
            self.assertEqual(env.get("XDG_CACHE_HOME"), str(tmp / "cache"))
            self.assertEqual(env.get("GIT_CEILING_DIRECTORIES"), str(tmp / "work"))
            from agentmem.projects import repository_root
            self.assertEqual(Path(repository_root(work)), work)


class EvaluatorExitCodes(unittest.TestCase):
    def test_missing_fixtures_returns_exit_2(self):
        with tempfile.TemporaryDirectory(prefix="agentmem-eval-", dir=str(_repo_tmp())) as tmp:
            r = subprocess.run(
                [sys.executable, "-I", str(ROOT / "tools" / "evaluate_knowledge.py"),
                 "--arm", "baseline", "--fixtures", tmp, "--output", str(Path(tmp) / "out")],
                capture_output=True, timeout=30, cwd=str(ROOT)
            )
            self.assertEqual(r.returncode, 2)

    def test_selftest_fixture_runs(self):
        with tempfile.TemporaryDirectory(prefix="agentmem-eval-", dir=str(_repo_tmp())) as tmp:
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
            self.assertTrue(result["arm_source_commit"].startswith("03764f8"))
            self.assertNotEqual(result["arm_source_commit"], result["commit"])
            self.assertFalse(Path(result["arm_source_path"]).exists())


class EvaluatorQmdStatus(unittest.TestCase):
    def test_detect_qmd_never_returns_ok_without_version(self):
        sys.path.insert(0, str(ROOT / "tools"))
        try:
            import evaluate_knowledge as ev
        finally:
            sys.path.pop(0)

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=b"", stderr=b"")

        with mock.patch("subprocess.run", fake_run):
            status, version = ev.detect_qmd()
        self.assertEqual(status, "unavailable")
        self.assertEqual(version, "")

    def test_detect_qmd_unavailable_on_version_error(self):
        sys.path.insert(0, str(ROOT / "tools"))
        try:
            import evaluate_knowledge as ev
        finally:
            sys.path.pop(0)

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout=b"qmd 2.8.3", stderr=b"boom")

        with mock.patch("subprocess.run", fake_run):
            status, version = ev.detect_qmd()
        self.assertEqual(status, "unavailable")
        self.assertEqual(version, "qmd 2.8.3")


class EvaluatorFixes(unittest.TestCase):
    def _copy_selftest_fixture(self, dest):
        import shutil
        shutil.copytree(SELFTEST, dest, dirs_exist_ok=True)

    def test_lifecycle_failure_keeps_graded_results(self):
        sys.path.insert(0, str(ROOT / "tools"))
        try:
            import evaluate_knowledge as ev
        finally:
            sys.path.pop(0)
        with tempfile.TemporaryDirectory(prefix="agentmem-lifecycle-graded-", dir=str(_repo_tmp())) as tmp:
            tmp = Path(tmp)
            fixtures = tmp / "fixtures"
            out = tmp / "out"
            self._copy_selftest_fixture(fixtures)
            corpus = json.loads((fixtures / "corpus.json").read_text(encoding="utf-8"))
            first = corpus["sessions"][0]
            sanitized = "".join(c for c in first["session_id"] if c.isalnum())
            collision_id = "collide-" + sanitized[-8:]
            transcript_rel = f"transcripts/claude/{collision_id}.jsonl"
            shutil.copy(fixtures / "transcripts" / "claude" / "ses-selftest-001.jsonl",
                        fixtures / transcript_rel)
            corpus["sessions"].append({
                "session_id": collision_id,
                "agent": "claude",
                "cwd": first["cwd"],
                "part": 1,
                "started": "2026-10-01T10:00:00Z",
                "ended": "2026-10-01T11:00:00Z",
                "repo_head": first["repo_head"],
                "transcript": transcript_rel,
                "summary_response": first["summary_response"],
                "case_ids": ["T19"],
            })
            (fixtures / "corpus.json").write_text(json.dumps(corpus, indent=2), encoding="utf-8")

            manifest = json.loads((fixtures / "manifest.json").read_text(encoding="utf-8"))
            for name in list(manifest["files"].keys()):
                p = fixtures / name
                if p.is_file():
                    manifest["files"][name] = hashlib.sha256(p.read_bytes()).hexdigest()
            manifest["files"][transcript_rel] = hashlib.sha256((fixtures / transcript_rel).read_bytes()).hexdigest()
            manifest["counts"]["sessions"] = 2
            (fixtures / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

            def passed_grade(query, gold, context, **kwargs):
                return {"query_id": query["query_id"], "pass": True, "failures": [], "unmeasured": [],
                        "snippet_count": len(context["snippets"]), "packed_bytes": context["packed_bytes"]}
            with mock.patch.object(ev, "grade_query", passed_grade):
                exit_code = ev.run_baseline(str(fixtures), str(out))
            result = json.loads((out / "result.json").read_text(encoding="utf-8"))
            lifecycle = result["lifecycle"]
            self.assertTrue(lifecycle["colliding_captures"] >= 1 or lifecycle["notes_written"] < lifecycle["expected_sessions"],
                            f"expected lifecycle failure, got {lifecycle}")
            self.assertFalse(result["correctness_pass"])
            self.assertTrue(result["lifecycle_failure"])
            t01 = next((o for o in result["per_query"] if o["query_id"] == "selftest-T01-fact"), None)
            self.assertIsNotNone(t01)
            self.assertTrue(t01["pass"], "graded result must stay as passed")
            self.assertNotIn("lifecycle_grader:no_notes_persisted", t01.get("failures", []))

    def test_qmd_index_runs_inside_repo_tmp(self):
        sys.path.insert(0, str(ROOT / "tools"))
        try:
            import evaluate_knowledge as ev
        finally:
            sys.path.pop(0)
        with tempfile.TemporaryDirectory(prefix="agentmem-eval-qmd-", dir=str(_repo_tmp())) as tmp:
            tmp = Path(tmp)
            out = tmp / "out"
            r = subprocess.run(
                [sys.executable, "-I", str(ROOT / "tools" / "evaluate_knowledge.py"),
                 "--arm", "baseline", "--fixtures", str(SELFTEST), "--output", str(out)],
                capture_output=True, timeout=120, cwd=str(ROOT)
            )
            self.assertIn(r.returncode, (0, 1, 3), r.stderr)
            result = json.loads((out / "result.json").read_text(encoding="utf-8"))
            # The eval must use a temp dir under the repo scratch area.
            self.assertTrue(result["temp_dir"].startswith(str(ROOT / ".audit" / "tmp")))
            # Per-query provenance is recorded.
            self.assertTrue(all("qmd_used" in o for o in result["per_query"]))
            if result["qmd_status"] == "ok":
                # The named QMD index path must be inside that temp dir, not the shared cache.
                qmd_sqlite = Path(result["qmd_index_path"])
                self.assertTrue(qmd_sqlite.resolve().is_relative_to(Path(result["temp_dir"]).resolve()))


class BaselineSearchContract(unittest.TestCase):
    def evaluator(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import evaluate_knowledge
        return evaluate_knowledge

    def test_current_capture_isolates_git_and_replays_recorded_head(self):
        ev = self.evaluator()
        from agentmem import projects, worker
        fixtures = ROOT / "tests/fixtures/knowledge"
        corpus = json.loads((fixtures / "corpus.json").read_text(encoding="utf-8"))
        session = next(s for s in corpus["sessions"] if s["agent"] == "claude" and "T27" in s["case_ids"])
        corpus["sessions"] = [session]
        corpus["projects"] = [p for p in corpus["projects"] if p["id"] == session["project_id"]]
        corpus["_transcripts_dir"] = str(fixtures)
        original_head = projects.repo_head
        original_run = worker.run
        ceilings = []
        with tempfile.TemporaryDirectory(dir=str(_repo_tmp())) as directory:
            base = Path(directory)
            vault, work, home, roots = ev.make_temp_vault(base, corpus)
            expected_ceiling = ev.isolated_environment(vault, home)["GIT_CEILING_DIRECTORIES"]

            def capture(*args, **kwargs):
                ceilings.append(os.environ.get("GIT_CEILING_DIRECTORIES"))
                return original_run(*args, **kwargs)

            with mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": "caller-ceiling"}):
                with mock.patch.object(worker, "run", side_effect=capture):
                    cfg = ev.run_pipeline_for_corpus(vault, work, home, corpus, roots, "", knowledge=False)
                self.assertEqual(os.environ["GIT_CEILING_DIRECTORIES"], "caller-ceiling")
            self.assertIs(projects.repo_head, original_head)
            with self.subTest("isolated capture Git ceiling"):
                self.assertEqual(ceilings, [expected_ceiling])
            manifest = Manifest(cfg.manifest_path)
            try:
                note = manifest.capture("claude", session["session_id"], session["part"])
            finally:
                manifest.close()
            self.assertIsNotNone(note)
            text = (vault / note["note_path"]).read_text(encoding="utf-8")
            with self.subTest("recorded source head survives capture"):
                self.assertIn("repo_head: " + json.dumps(session["repo_head"]), text)

    def test_frozen_formulation_uses_no_entity_or_gold_lookup(self):
        ev = self.evaluator()
        self.assertEqual(ev.formulate_query("Which ALPHA backend should we use for storage now?"), "alpha backend use storage")
        self.assertEqual(ev.formulate_query("What is the lattice-01-01 storage choice and rationale?"), "lattice-01-01 storage choice rationale")

    def test_redacted_session_id_binds_only_to_a_unique_supported_manifest_identity(self):
        from tools import knowledge_grader as grader
        with tempfile.TemporaryDirectory(dir=str(_repo_tmp())) as directory:
            vault = Path(directory)
            path = vault / "0-Sessions/note.md"
            path.parent.mkdir()
            text = '---\ntype: session\nagent: claude\nsession_id: "[REDACTED]"\npart: 1\nproject: "Alpha"\n---\nUse SQLite.\nStatus: reported.\n'
            path.write_text(text, encoding="utf-8")
            manifest = Manifest(vault / "_meta/manifest.sqlite")
            try:
                manifest.db.execute("INSERT INTO captures(agent,session_id,part,note_path,project,start_offset,end_offset) VALUES ('claude','real-id',1,'0-Sessions/note.md','Alpha',0,100)")
                corpus = {"projects": [{"id": "alpha", "name": "Alpha"}],
                          "sessions": [{"agent": "claude", "session_id": "real-id", "part": 1, "project_id": "alpha"}],
                          "sources": [{"id": "s", "agent": "claude", "session_id": "real-id", "part": 1, "project_id": "alpha"}],
                          "claims": [{"id": "c", "project_id": "alpha", "source_ids": ["s"], "statement": "Use SQLite."}]}
                query = {"query_id": "masked", "project": "alpha", "budget": 2400}
                gold = {"required_claims": ["c"], "required_sources": ["s"], "expected_labels": ["reported"]}
                def grade():
                    package = {"snippets": [{"path": "0-Sessions/note.md", "content": path.read_text(encoding="utf-8")}], "packed_bytes": 0}
                    return grader.grade_query(query, gold, package, corpus=corpus, vault=vault)
                self.assertTrue(grade()["pass"])
                self.assertTrue(grader.lifecycle_check(vault, corpus)["passed"])
                self.assertEqual(grader.lifecycle_check(vault, corpus)["redacted_session_ids"], 1)
                for replacement in ("[redacted]", "[REDACTED ]", "different-id"):
                    path.write_text(text.replace("[REDACTED]", replacement), encoding="utf-8")
                    self.assertFalse(grade()["pass"])
                    self.assertFalse(grader.lifecycle_check(vault, corpus)["passed"])
                path.write_text(text.replace("Use SQLite.", "Use MongoDB."), encoding="utf-8")
                self.assertFalse(grade()["pass"])
                self.assertFalse(grader.lifecycle_check(vault, corpus)["passed"])
                path.write_text(text.replace("agent: claude", "agent: codex"), encoding="utf-8")
                self.assertFalse(grade()["pass"])
                path.write_text(text, encoding="utf-8")
                manifest.db.execute("INSERT INTO captures(agent,session_id,part,note_path,project,start_offset,end_offset) VALUES ('claude','alien',1,'0-Sessions/note.md','Alpha',0,100)")
                self.assertFalse(grade()["pass"])
                self.assertFalse(grader.lifecycle_check(vault, corpus)["passed"])
            finally:
                manifest.close()

    def test_search_uses_exactly_one_cli_request_and_preserves_empty_success(self):
        ev = self.evaluator()
        with tempfile.TemporaryDirectory(dir=str(_repo_tmp())) as directory:
            root = Path(directory)
            with mock.patch.object(ev, "run_mem_command", return_value=(0, "[]", 'EVAL_QMD:{"returncode":0}\n')) as command:
                result = ev.package_baseline_context(root / "vault", root / "home", {"query": "full arbitrary question?", "budget": 2400})
            self.assertEqual(command.call_count, 1)
            self.assertEqual(command.call_args.args[2], ["search", "full arbitrary question?", "--json", "-n", "8"])
            self.assertEqual(result["backend"], "qmd")
            self.assertEqual(result["hit_count"], 0)

    def test_empty_cli_fallback_is_observed_from_the_actual_backend_trace(self):
        ev = self.evaluator()
        with tempfile.TemporaryDirectory(dir=str(_repo_tmp())) as directory:
            root = Path(directory)
            with mock.patch.object(ev, "run_mem_command", return_value=(0, "[]", 'EVAL_QMD:{"returncode":1}\n')):
                result = ev.package_baseline_context(root / "vault", root / "home", {"query": "no matches", "budget": 2400})
            self.assertEqual(result["backend"], "fallback")
            self.assertFalse(result["qmd_used"])

    def test_real_qmd_index_search_and_note_stay_in_isolated_state(self):
        ev = self.evaluator()
        from agentmem.search import qmd_cmd
        if qmd_cmd() is None:
            self.skipTest("QMD unavailable")
        with tempfile.TemporaryDirectory(dir=str(_repo_tmp())) as directory:
            outer = Path(directory)
            outside = outer / "outside"
            outside.mkdir()
            sentinel = outside / "unchanged.txt"
            sentinel.write_bytes(b"unchanged")
            base = outer / "isolated"
            base.mkdir()
            vault, work, home, _ = ev.make_temp_vault(base, {"projects": [], "sessions": []}, qmd_index="index")
            note = vault / "0-Sessions/synthetic.md"
            note.write_text("# Synthetic storage\nSQLite storage supports atomic cursor updates.\n", encoding="utf-8")
            poison = {key: str(outside) for key in ("HOME", "USERPROFILE", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "QMD_CONFIG_DIR", "TEMP", "TMP")}
            poison["INDEX_PATH"] = str(outside / "escaped.sqlite")
            poison["NODE_OPTIONS"] = "--require forbidden-preload"
            with mock.patch.dict(os.environ, poison):
                rc, stdout, stderr = ev.run_mem_command(vault, home, ["index", "--no-embed"], cwd=base)
                self.assertEqual(rc, 0, stderr)
                self.assertEqual(json.loads(stdout)["update"], 0)
                package = ev.package_baseline_context(vault, home, {"query": "storage", "budget": 2400}, cwd=base)
            self.assertEqual(package["backend"], "qmd")
            self.assertGreater(package["hit_count"], 0)
            self.assertGreater(len(package["snippets"]), 0)
            self.assertEqual(package["snippets"][0]["path"], "0-Sessions/synthetic.md")
            self.assertIn(package["snippets"][0]["content"], note.read_text(encoding="utf-8"))
            self.assertEqual(set(outside.iterdir()), {sentinel})
            self.assertEqual(sentinel.read_bytes(), b"unchanged")
            database = base / "cache/qmd/index.sqlite"
            self.assertTrue(database.is_file())
            self.assertTrue(database.resolve().is_relative_to(base.resolve()))
            self.assertTrue((base / "config/qmd/index.yml").is_file())


if __name__ == "__main__":
    unittest.main()
