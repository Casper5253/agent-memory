"""Set R package builder: refusal, blinding, shared budget, isolation. Synthetic vault and questions only."""
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tools import evaluate_knowledge as ek
from tools import evaluate_set_r as sr

QUESTIONS = [
    dict(id="SYN-01", project="Demo App", **{"class": "decision_and_rationale"},
         question="Why does the demo store its cursor in SQLite?", gold_answer="Atomic cursor writes.",
         gold_evidence="synthetic", stale_trap=False, as_of_commit="0000000"),
    dict(id="SYN-02", project="Demo App", **{"class": "current_repository_or_release_state"},
         question="What storage layer does the demo use now?", gold_answer="SQLite.",
         gold_evidence="synthetic", stale_trap=True, as_of_commit="0000000"),
    dict(id="SYN-03", project="Demo App", **{"class": "unresolved_or_contradictory"},
         question="Is the cursor write path still open for review?", gold_answer="Open.",
         gold_evidence="synthetic", stale_trap=False, as_of_commit="0000000"),
]


def scratch_dir():
    scratch = ROOT / ".audit" / "tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    return scratch


def write_synthetic_vault(vault):
    for folder in ("_meta/queue", "_meta/locks", "_meta/logs", "0-Sessions", "1-Projects", "2-Areas", "3-Resources", "4-Archive"):
        (vault / folder).mkdir(parents=True, exist_ok=True)
    (vault / "_meta" / "projects.json").write_text(json.dumps({"projects": [
        {"name": "Demo App", "roots": ["C:/nowhere/demo-app"], "status": "active"}]}), encoding="utf-8")
    (vault / "0-Sessions" / "session-one.md").write_text(
        "---\ntitle: Synthetic session one\nproject: Demo App\n---\n\n"
        "The demo chose SQLite for its cursor store because cursor updates must be atomic.\n", encoding="utf-8")
    (vault / "1-Projects" / "demo-app.md").write_text(
        "---\ntitle: Demo App\n---\n\nDemo App keeps its cursor in SQLite with atomic writes.\n", encoding="utf-8")


def tree_digest(root):
    digest = {}
    for path in sorted(Path(root).rglob("*")):
        if path.is_file():
            digest[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest


def write_questions(folder, rows=QUESTIONS, frozen_sha=None):
    folder.mkdir(parents=True, exist_ok=True)
    questions = folder / "questions.jsonl"
    questions.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    sha = frozen_sha or ek.sha256_file(questions)
    (folder / "FROZEN.sha256").write_text(f"{sha}  questions.jsonl  frozen for a test\n", encoding="utf-8")
    return questions


class RefusalAndInput(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=scratch_dir())
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)

    def test_refuses_when_questions_sha_does_not_match_frozen(self):
        questions = write_questions(self.tmp / "R", frozen_sha="0" * 64)
        with self.assertRaises(sr.SetupError) as raised:
            sr.check_frozen(questions)
        self.assertIn("does not match frozen", str(raised.exception))

    def test_refuses_when_frozen_file_is_missing(self):
        folder = self.tmp / "R"
        folder.mkdir()
        questions = folder / "questions.jsonl"
        questions.write_text(json.dumps(QUESTIONS[0]) + "\n", encoding="utf-8")
        with self.assertRaises(sr.SetupError):
            sr.check_frozen(questions)

    def test_main_exits_two_and_writes_nothing_on_sha_mismatch(self):
        questions = write_questions(self.tmp / "R", frozen_sha="f" * 64)
        vault = self.tmp / "vault"
        write_synthetic_vault(vault)
        out = self.tmp / "out"
        self.assertEqual(sr.main(["packages", "--vault", str(vault), "--questions", str(questions),
                                  "--arms", "b1", "--out", str(out)]), 2)
        self.assertFalse((out / "packages.jsonl").exists())

    def test_refuses_question_rows_missing_a_required_key(self):
        broken = dict(QUESTIONS[0])
        del broken["gold_evidence"]
        questions = write_questions(self.tmp / "R", rows=[broken])
        with self.assertRaises(sr.SetupError):
            sr.load_questions(questions)

    def test_k0_is_skipped_without_store(self):
        vault = self.tmp / "vault"
        write_synthetic_vault(vault)
        questions = write_questions(self.tmp / "R")
        out = self.tmp / "out"
        _, key_packages, arms = sr.run_packages(
            type("Args", (), dict(vault=str(vault), questions=str(questions), arms="k0,b1", store=None,
                                  out=str(out), key=None))())
        self.assertEqual(arms, ["b1"])
        self.assertEqual({entry["arm"] for entry in key_packages.values()}, {"b1"})


class BlindedPackages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which("qmd") is None:
            raise unittest.SkipTest("qmd is required for the baseline arms")
        temp = tempfile.TemporaryDirectory(dir=scratch_dir())
        cls.addClassCleanup(temp.cleanup)
        cls.tmp = Path(temp.name)
        cls.vault = cls.tmp / "vault"
        write_synthetic_vault(cls.vault)
        cls.source_digest = tree_digest(cls.vault)
        cls.questions = write_questions(cls.tmp / "R")
        cls.out = cls.tmp / "out"
        sr.main(["packages", "--vault", str(cls.vault), "--questions", str(cls.questions),
                 "--arms", "b0,b1", "--out", str(cls.out)])
        cls.lines = (cls.out / "packages.jsonl").read_text(encoding="utf-8").splitlines()
        cls.packages = [json.loads(line) for line in cls.lines]
        cls.key = json.loads((cls.out / "key.json").read_text(encoding="utf-8"))

    def test_one_package_per_question_and_arm(self):
        self.assertEqual(len(self.packages), len(QUESTIONS) * 2)
        pairs = {(entry["question_id"], entry["arm"]) for entry in self.key["packages"].values()}
        self.assertEqual(pairs, {(q["id"], arm) for q in QUESTIONS for arm in ("b0", "b1")})

    def test_package_fields_are_exactly_the_blind_shape(self):
        for package in self.packages:
            self.assertEqual(set(package), set(sr.PACKAGE_KEYS))

    def test_packages_file_holds_no_arm_name_or_key_entry(self):
        text = (self.out / "packages.jsonl").read_text(encoding="utf-8").lower()
        for leak in ("b0", "b1", "k0", "baseline", "reliability", "candidate", "question_id"):
            self.assertNotIn(leak, text)
        self.assertEqual({p["package_id"] for p in self.packages}, set(self.key["packages"]))

    def test_order_is_shuffled_away_from_question_then_arm(self):
        grouped = [(self.key["packages"][p["package_id"]]["question_id"], self.key["packages"][p["package_id"]]["arm"])
                   for p in self.packages]
        ordered = [(q["id"], arm) for q in QUESTIONS for arm in ("b0", "b1")]
        self.assertNotEqual(grouped, ordered)

    def test_every_arm_stays_within_the_same_budget_and_tokenizer(self):
        self.assertEqual(self.key["budget_tokens"], sr.BUDGET_TOKENS)
        self.assertEqual(self.key["tokenizer"], "ceil(utf8_bytes/3)")
        for package in self.packages:
            self.assertLessEqual(package["tokens"], sr.BUDGET_TOKENS)
            self.assertEqual(package["tokens"], sr.tokens(package["context_text"]))
        for arm in ("b0", "b1"):
            self.assertTrue(any(p["context_text"] for p in self.packages
                                if self.key["packages"][p["package_id"]]["arm"] == arm))

    def test_live_vault_is_never_opened_and_source_is_unchanged(self):
        calls = []
        original = ek.run_mem_command

        def recording(vault, home, args, **kwargs):
            calls.append(dict(vault=str(vault), env=ek.isolated_environment(vault, home, kwargs.get("cache_dir"))))
            return original(vault, home, args, **kwargs)

        with mock.patch.object(ek, "run_mem_command", side_effect=recording):
            sr.run_packages(type("Args", (), dict(vault=str(self.vault), questions=str(self.questions),
                arms="b1", store=None, out=str(self.tmp / "isolated"), key=None))())
        self.assertTrue(calls)
        for call in calls:
            self.assertNotEqual(Path(call["vault"]).resolve(), self.vault.resolve())
            self.assertEqual(call["env"]["OBSIDIAN_VAULT_PATH"], call["vault"])
            self.assertFalse(Path(call["vault"]).resolve().is_relative_to(self.vault.resolve()))
        self.assertEqual(tree_digest(self.vault), self.source_digest)


class KnowledgeArm(unittest.TestCase):
    def test_k0_packages_come_from_the_store_for_the_question_project(self):
        from test_knowledge_context import ContextCase
        fixture = ContextCase("test_decision_rationale_labels_citation_and_no_writes")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.claim()
        fixture.store.close()
        questions = write_questions(self.tmp_root(fixture), rows=[dict(QUESTIONS[0], project=fixture.pid,
            question="What storage does the project use?")])
        out = self.tmp_root(fixture) / "out"
        _, key_packages, arms = sr.run_packages(type("Args", (), dict(vault=str(fixture.vault), questions=str(questions),
            arms="k0", store=str(fixture.path), out=str(out), key=None))())
        self.assertEqual(arms, ["k0"])
        entry = next(iter(key_packages.values()))
        self.assertEqual(entry["arm"], "k0")
        self.assertEqual(entry["search_error"], "")
        self.assertGreaterEqual(entry["kept_items"], 1)
        package = json.loads((out / "packages.jsonl").read_text(encoding="utf-8").splitlines()[0])
        self.assertIn("SQLite", package["context_text"])
        self.assertLessEqual(package["tokens"], sr.BUDGET_TOKENS)

    def tmp_root(self, fixture):
        return fixture.vault / "R-run"


if __name__ == "__main__":
    unittest.main()
