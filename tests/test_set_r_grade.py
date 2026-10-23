import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "set_r_grade.py"
ARMS = ("b0", "b1", "k0")


def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def question(qid, klass="current-state"):
    return {
        "id": qid,
        "class": klass,
        "question": f"Which storage state applies for {qid}?",
        "project": f"Project {qid}",
        "gold_answer": "SQLite is current because updates are atomic.",
        "gold_evidence": [f"source-{qid}"],
        "stale_trap": "Do not report JSON as current.",
        "as_of_commit": f"commit-{qid}",
    }


def seed_inputs(root, count=4):
    questions = [question(f"q{i}", "current-state" if i % 2 else "decision") for i in range(1, count + 1)]
    questions_path = root / "questions.jsonl"
    write_jsonl(questions_path, questions)
    package_rows = []
    key_packages = {}
    for item in questions:
        for arm_index, arm in enumerate(ARMS):
            package_id = "p" + f"{(int(item['id'][1:]) - 1) * len(ARMS) + arm_index + 1:016x}"
            package_rows.append({
                "package_id": package_id,
                "question": item["question"],
                "project": item["project"],
                "context_text": "SQLite provides atomic cursor updates.",
                "citations": [{"path": "source.md", "label": "source-q"}],
                "tokens": 12,
            })
            key_packages[package_id] = {"question_id": item["id"], "arm": arm}
    packages_path = root / "packages.jsonl"
    write_jsonl(packages_path, package_rows)
    key_path = root / "key.json"
    key_path.write_text(json.dumps({"packages": key_packages}), encoding="utf-8")
    answers = root / "answers"
    answers.mkdir()
    for package in package_rows:
        (answers / f"{package['package_id']}.json").write_text(
            json.dumps({"answer": "SQLite is current because updates are atomic."}), encoding="utf-8")
    return questions, questions_path, package_rows, packages_path, key_packages, key_path, root


def run_cli(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], cwd=ROOT,
                          text=True, capture_output=True, check=False)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


class SetRGradeTests(unittest.TestCase):
    def test_answer_tasks_hide_package_and_arm_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, _, packages, package_path, _, _, _ = seed_inputs(root, count=1)
            output = root / "answer-run"
            result = run_cli("answer-tasks", "--packages", package_path, "--out", output)
            self.assertEqual(result.returncode, 0, result.stderr)
            task_path = next((output / "tasks").glob("*.json"))
            payload = read_json(task_path)
            encoded = json.dumps(payload)
            for package in packages:
                self.assertNotIn(package["package_id"], encoded)
            for arm in ARMS:
                self.assertNotIn(arm, encoded)
            self.assertNotIn('"arm"', encoded)
            self.assertNotIn('"key"', encoded)
            self.assertEqual(set(payload), {"prompt", "question", "project", "context_text", "citations"})
            self.assertIn("not in context", payload["prompt"])
            self.assertIn("source ids", payload["prompt"].lower())

    def test_grade_tasks_are_blind_seeded_and_balanced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, questions_path, packages, package_path, key_packages, key_path, answers = seed_inputs(root, count=5)
            outputs = [root / "grade-a", root / "grade-b"]
            for output in outputs:
                result = run_cli("grade-tasks", "--answers", answers, "--packages", package_path,
                                 "--key", key_path, "--questions", questions_path, "--out", output, "--seed", "17")
                self.assertEqual(result.returncode, 0, result.stderr)
            maps = [read_json(output / "map.json") for output in outputs]
            self.assertEqual(maps[0], maps[1])
            self.assertEqual(sorted(path.name for path in (outputs[0] / "tasks").glob("*.json")),
                             sorted(path.name for path in (outputs[1] / "tasks").glob("*.json")))
            singles = [row for row in maps[0]["tasks"] if row["kind"] == "single"]
            pairs = [row for row in maps[0]["tasks"] if row["kind"] == "pairwise"]
            self.assertEqual(len(singles), 15)
            self.assertEqual(len(pairs), 5)
            k0_as_a = sum(key_packages[row["package_a"]]["arm"] == "k0" for row in pairs)
            self.assertLessEqual(abs(k0_as_a - (len(pairs) / 2)), 0.5)
            task_ids = {row["task_id"] for row in maps[0]["tasks"]}
            self.assertTrue(all((outputs[0] / "tasks" / f"{task_id}.json").is_file() for task_id in task_ids))
            for path in (outputs[0] / "tasks").glob("*.json"):
                encoded = path.read_text(encoding="utf-8")
                for package in packages:
                    self.assertNotIn(package["package_id"], encoded)
                for arm in ARMS:
                    self.assertNotIn(arm, encoded)
                self.assertNotIn('"arm"', encoded)
                self.assertNotIn('"package_id"', encoded)
                self.assertNotIn('"key"', encoded)

    def test_tally_counts_pair_and_single_grades_without_exposing_answers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            questions, questions_path, _, _, key_packages, key_path, _ = seed_inputs(root, count=2)
            grades = root / "grades"
            (grades / "grades").mkdir(parents=True)
            mapping = []
            task_number = 0

            def add(kind, qindex, grade, package_a=None, package_b=None, package_id=None):
                nonlocal task_number
                task_number += 1
                task_id = f"task-{task_number:06d}"
                row = {"task_id": task_id, "kind": kind}
                if kind == "pairwise":
                    row.update(package_a=package_a, package_b=package_b)
                else:
                    row["package_id"] = package_id
                mapping.append(row)
                (grades / "grades" / f"{task_id}.json").write_text(json.dumps(grade), encoding="utf-8")
                return task_id

            for qindex, item in enumerate(questions, 1):
                by_arm = {entry["arm"]: package_id for package_id, entry in key_packages.items()
                          if entry["question_id"] == item["id"]}
                a, b = (by_arm["k0"], by_arm["b1"]) if qindex == 1 else (by_arm["b1"], by_arm["k0"])
                add("pairwise", qindex, {"winner": "A" if qindex == 1 else "B"}, package_a=a, package_b=b)
                for arm in ARMS:
                    verdict = "correct" if arm == "k0" else "partial" if arm == "b1" else "wrong"
                    stale = arm == "b0" and qindex == 1
                    add("single", qindex, {"verdict": verdict, "stale": stale}, package_id=by_arm[arm])
            map_path = grades / "map.json"
            map_path.write_text(json.dumps({"schema_version": 1, "tasks": mapping}), encoding="utf-8")
            report_path = root / "report.md"
            result = run_cli("tally", "--grades", grades, "--map", map_path, "--key", key_path,
                             "--questions", questions_path, "--out", report_path)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = report_path.read_text(encoding="utf-8")
            self.assertIn("K0 wins | B1 wins | Ties", report)
            self.assertIn("2 | 0 | 0", report)
            self.assertIn("| current-state | 1 | 0 | 0 |", report)
            self.assertIn("| decision | 1 | 0 | 0 |", report)
            self.assertIn("| b0 | 0 | 0 | 2 | 0 | 1 | 1 |", report)
            self.assertIn("| b1 | 0 | 2 | 0 | 0 | 0 | 2 |", report)
            self.assertIn("| k0 | 2 | 0 | 0 | 0 | 0 | 2 |", report)
            for item in questions:
                self.assertIn(item["id"], report)
                self.assertNotIn(item["question"], report)
                self.assertNotIn(item["gold_answer"], report)

    def test_tally_lists_missing_and_malformed_grades_without_imputation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            questions, questions_path, _, _, key_packages, key_path, _ = seed_inputs(root, count=1)
            grades = root / "grades"
            (grades / "grades").mkdir(parents=True)
            rows = []
            by_arm = {entry["arm"]: package_id for package_id, entry in key_packages.items()}
            rows.extend([
                {"task_id": "task-000001", "kind": "pairwise", "package_a": by_arm["k0"], "package_b": by_arm["b1"]},
                {"task_id": "task-000002", "kind": "single", "package_id": by_arm["b0"]},
            ])
            for index, arm in enumerate(("b1", "k0"), 1):
                rows.append({"task_id": f"task-{index + 2:06d}", "kind": "single", "package_id": by_arm[arm]})
                (grades / "grades" / f"task-{index + 2:06d}.json").write_text(
                    json.dumps({"verdict": "correct", "stale": False}), encoding="utf-8")
            (grades / "grades" / "task-000002.json").write_text('{"verdict":"great"}', encoding="utf-8")
            map_path = grades / "map.json"
            map_path.write_text(json.dumps({"schema_version": 1, "tasks": rows}), encoding="utf-8")
            report_path = root / "report.md"
            result = run_cli("tally", "--grades", grades, "--map", map_path, "--key", key_path,
                             "--questions", questions_path, "--out", report_path)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = report_path.read_text(encoding="utf-8")
            self.assertIn("task-000001", report)
            self.assertIn("task-000002", report)
            self.assertIn("missing", report.lower())
            self.assertIn("malformed", report.lower())
            self.assertIn("not graded", report.lower())
            self.assertNotIn(questions[0]["question"], report)


if __name__ == "__main__":
    unittest.main()
