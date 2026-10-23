"""Build blind Set R answer and grading tasks, then tally their grades.

This tool never calls a model. Task payloads are kept separate from the blind map;
only ``tally`` joins that map to the package key.
"""
import argparse
import json
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.evaluate_set_r import PACKAGE_KEYS, REQUIRED_KEYS, SetupError

ARMS = ("b0", "b1", "k0")
PAIR_ARMS = frozenset(("k0", "b1"))
VERDICTS = ("correct", "partial", "wrong", "unknown")
PACKAGE_ID = re.compile(r"p[0-9a-f]{16}\Z")
PROMPT_DIR = ROOT / "tools" / "prompts"


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SetupError(f"cannot read JSON {Path(path).name}: {type(error).__name__}") from None


def _read_jsonl(paths):
    rows = []
    for path in paths:
        try:
            with Path(path).open(encoding="utf-8") as stream:
                for number, line in enumerate(stream, 1):
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        raise SetupError(f"invalid JSON in {Path(path).name} line {number}") from None
                    if not isinstance(row, dict):
                        raise SetupError(f"expected object in {Path(path).name} line {number}")
                    rows.append(row)
        except (OSError, UnicodeError) as error:
            raise SetupError(f"cannot read {Path(path).name}: {type(error).__name__}") from None
    return rows


def _load_questions(path):
    rows = _read_jsonl([path])
    by_id = {}
    for row in rows:
        missing = [field for field in REQUIRED_KEYS if field not in row]
        if missing:
            raise SetupError(f"question row in {Path(path).name} lacks required fields")
        qid = row.get("id")
        if not isinstance(qid, str) or not qid:
            raise SetupError("question id must be a non-empty string")
        if qid in by_id:
            raise SetupError(f"duplicate question id: {qid}")
        for field in ("class", "question", "project", "gold_answer"):
            if not isinstance(row.get(field), str) or not row[field].strip():
                raise SetupError(f"question {qid} has invalid {field}")
        if not isinstance(row.get("gold_evidence"), (list, str)):
            raise SetupError(f"question {qid} has invalid gold_evidence")
        if not isinstance(row.get("stale_trap"), (list, str)):
            raise SetupError(f"question {qid} has invalid stale_trap")
        by_id[qid] = row
    if not by_id:
        raise SetupError("questions file is empty")
    return by_id


def _load_key(path):
    data = _read_json(path)
    packages = data.get("packages") if isinstance(data, dict) else None
    if not isinstance(packages, dict):
        raise SetupError("key must contain a packages object")
    result = {}
    for package_id, item in packages.items():
        if not isinstance(package_id, str) or not PACKAGE_ID.fullmatch(package_id) or not isinstance(item, dict):
            raise SetupError("invalid package entry in key")
        question_id, arm = item.get("question_id"), item.get("arm")
        if not isinstance(question_id, str) or not isinstance(arm, str) or arm not in ARMS:
            raise SetupError(f"invalid key entry for package {package_id}")
        result[package_id] = {"question_id": question_id, "arm": arm}
    return result


def _load_packages(paths):
    rows = _read_jsonl(paths)
    by_id = {}
    for row in rows:
        if set(row) != set(PACKAGE_KEYS):
            raise SetupError("package row must match evaluate_set_r package schema")
        package_id = row["package_id"]
        if not isinstance(package_id, str) or not PACKAGE_ID.fullmatch(package_id):
            raise SetupError("package_id must match evaluate_set_r package id format")
        if package_id in by_id:
            raise SetupError(f"duplicate package id: {package_id}")
        if not all(isinstance(row[field], str) for field in ("question", "project", "context_text")):
            raise SetupError(f"invalid text fields for package {package_id}")
        if not isinstance(row["citations"], list):
            raise SetupError(f"invalid citations for package {package_id}")
        by_id[package_id] = row
    if not by_id:
        raise SetupError("packages file is empty")
    return by_id


def _load_prompt(name):
    try:
        prompt = (PROMPT_DIR / name).read_text(encoding="utf-8").strip()
    except OSError as error:
        raise SetupError(f"cannot read prompt {name}: {type(error).__name__}") from None
    if not prompt:
        raise SetupError(f"prompt {name} is empty")
    return prompt


def _write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _check_new_zone(path):
    if path.exists():
        if not path.is_dir() or any(path.iterdir()):
            raise SetupError(f"refusing to overwrite output zone {path.name}")
    else:
        path.mkdir(parents=True)


def answer_tasks(package_paths, out_dir):
    packages = _load_packages(package_paths)
    prompt = _load_prompt("set_r_answer_v1.txt")
    output = Path(out_dir) / "tasks"
    _check_new_zone(output)
    for package_id, row in sorted(packages.items()):
        payload = {
            "prompt": prompt,
            "question": row["question"],
            "project": row["project"],
            "context_text": row["context_text"],
            "citations": row["citations"],
        }
        _write_json(output / f"{package_id}.json", payload)
    return len(packages)


def _read_answer(path, package_id):
    data = _read_json(path)
    if not isinstance(data, dict) or set(data) != {"answer"} or not isinstance(data["answer"], str):
        raise SetupError(f"malformed answer file for package {package_id}")
    if not data["answer"].strip():
        raise SetupError(f"empty answer for package {package_id}")
    return data["answer"]


def _validate_join(packages, key, questions):
    if set(packages) != set(key):
        raise SetupError("package ids do not match key package ids")
    by_question_arm = defaultdict(dict)
    for package_id, metadata in key.items():
        qid, arm = metadata["question_id"], metadata["arm"]
        if qid not in questions:
            raise SetupError(f"package {package_id} refers to unknown question {qid}")
        package = packages[package_id]
        question = questions[qid]
        if package["question"] != question["question"] or package["project"] != question["project"]:
            raise SetupError(f"package {package_id} does not match its question")
        if arm in by_question_arm[qid]:
            raise SetupError(f"duplicate {arm} package for question {qid}")
        by_question_arm[qid][arm] = package_id
    if set(by_question_arm) != set(questions):
        raise SetupError("key does not cover every question")
    for qid, arm_packages in by_question_arm.items():
        if set(arm_packages) != set(ARMS):
            raise SetupError(f"question {qid} must have exactly one b0, b1, and k0 package")
    return by_question_arm


def _new_tasks_zone(out_dir):
    out_dir = Path(out_dir)
    tasks_path = out_dir / "tasks"
    map_path = out_dir / "map.json"
    if map_path.exists():
        raise SetupError("refusing to overwrite map.json")
    _check_new_zone(tasks_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    return tasks_path, map_path


def grade_tasks(answers_dir, package_paths, key_path, questions_path, out_dir, seed):
    packages = _load_packages(package_paths)
    key = _load_key(key_path)
    questions = _load_questions(questions_path)
    by_question_arm = _validate_join(packages, key, questions)
    answers = {}
    for package_id in sorted(packages):
        answers[package_id] = _read_answer(Path(answers_dir) / "answers" / f"{package_id}.json", package_id)

    grade_prompt = _load_prompt("set_r_grade_v1.txt")
    tasks_path, map_path = _new_tasks_zone(out_dir)
    shuffled_qids = sorted(questions)
    rng = random.Random(seed)
    rng.shuffle(shuffled_qids)
    first_is_k0 = bool(rng.randrange(2))
    orientation = {qid: (first_is_k0 if index % 2 == 0 else not first_is_k0)
                   for index, qid in enumerate(shuffled_qids)}

    task_map = []
    task_number = 0

    def emit(kind, content, **refs):
        nonlocal task_number
        task_number += 1
        task_id = f"task-{task_number:06d}"
        _write_json(tasks_path / f"{task_id}.json", content)
        task_map.append({"task_id": task_id, "kind": kind, **refs})

    for qid in sorted(questions):
        question = questions[qid]
        refs = by_question_arm[qid]
        k0_id, b1_id = refs["k0"], refs["b1"]
        a_id, b_id = (k0_id, b1_id) if orientation[qid] else (b1_id, k0_id)
        shared = {
            "prompt": grade_prompt,
            "question": question["question"],
            "gold_answer": question["gold_answer"],
            "gold_evidence": question["gold_evidence"],
            "stale_trap": question["stale_trap"],
        }
        emit("pairwise", {**shared, "kind": "pairwise", "A": answers[a_id], "B": answers[b_id]},
             package_a=a_id, package_b=b_id)
        for arm in ARMS:
            package_id = refs[arm]
            emit("single", {**shared, "kind": "single", "answer": answers[package_id]},
                 package_id=package_id)

    _write_json(map_path, {"schema_version": 1, "tasks": task_map})
    return len(task_map)


def _validate_map(map_path, key_path, questions_path):
    data = _read_json(map_path)
    rows = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(rows, list):
        raise SetupError("invalid blind map schema")
    key = _load_key(key_path)
    questions = _load_questions(questions_path)
    by_question_arm = defaultdict(dict)
    for package_id, metadata in key.items():
        qid, arm = metadata["question_id"], metadata["arm"]
        if qid not in questions:
            raise SetupError(f"key refers to unknown question {qid}")
        if arm in by_question_arm[qid]:
            raise SetupError(f"duplicate {arm} package in key for question {qid}")
        by_question_arm[qid][arm] = package_id
    if set(by_question_arm) != set(questions):
        raise SetupError("key does not cover every question")
    for qid, arms in by_question_arm.items():
        if set(arms) != set(ARMS):
            raise SetupError(f"question {qid} must have exactly one b0, b1, and k0 package")

    seen_task_ids = set()
    seen_single_packages = set()
    seen_pair_questions = set()
    resolved = []
    for row in rows:
        if not isinstance(row, dict) or row.get("kind") not in ("single", "pairwise"):
            raise SetupError("invalid blind map task row")
        kind = row["kind"]
        expected_fields = {"task_id", "kind", "package_id"} if kind == "single" else {
            "task_id", "kind", "package_a", "package_b"}
        if set(row) != expected_fields:
            raise SetupError("blind map task row has unexpected fields")
        task_id = row["task_id"]
        if not isinstance(task_id, str) or not re.fullmatch(r"task-\d{6}", task_id) or task_id in seen_task_ids:
            raise SetupError("invalid or duplicate task id in blind map")
        seen_task_ids.add(task_id)
        if kind == "single":
            package_id = row["package_id"]
            if not isinstance(package_id, str) or package_id not in key or package_id in seen_single_packages:
                raise SetupError("unknown or duplicate package in blind map")
            seen_single_packages.add(package_id)
            metadata = key[package_id]
            resolved.append({**row, "question_id": metadata["question_id"], "arm": metadata["arm"]})
        else:
            package_a, package_b = row["package_a"], row["package_b"]
            if (not isinstance(package_a, str) or not isinstance(package_b, str)
                    or package_a not in key or package_b not in key or package_a == package_b):
                raise SetupError("unknown or repeated package in pairwise map")
            meta_a, meta_b = key[package_a], key[package_b]
            if meta_a["question_id"] != meta_b["question_id"]:
                raise SetupError("pairwise packages refer to different questions")
            qid = meta_a["question_id"]
            if qid in seen_pair_questions or {meta_a["arm"], meta_b["arm"]} != PAIR_ARMS:
                raise SetupError("invalid or duplicate pairwise question in blind map")
            seen_pair_questions.add(qid)
            resolved.append({**row, "question_id": qid, "arm_a": meta_a["arm"], "arm_b": meta_b["arm"]})
    expected_packages = set(key)
    expected_questions = set(questions)
    if seen_single_packages != expected_packages:
        raise SetupError("blind map must contain one single task for every package")
    if seen_pair_questions != expected_questions:
        raise SetupError("blind map must contain one k0 versus b1 pair for every question")
    return resolved, questions


def _read_grade(path, kind):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "malformed"
    if not isinstance(data, dict):
        return None, "malformed"
    if kind == "pairwise":
        if set(data) != {"winner"} or data["winner"] not in ("A", "B", "tie"):
            return None, "malformed"
    elif (set(data) != {"verdict", "stale"} or data["verdict"] not in VERDICTS
          or type(data["stale"]) is not bool):
        return None, "malformed"
    return data, None


def _pair_outcome(grade, row):
    winner = grade["winner"]
    if winner == "tie":
        return "tie"
    winning_arm = row["arm_a"] if winner == "A" else row["arm_b"]
    return "k0 win" if winning_arm == "k0" else "b1 win"


def tally(grades_dir, map_path, key_path, questions_path, out_path):
    resolved, questions = _validate_map(map_path, key_path, questions_path)
    grades_dir = Path(grades_dir) / "grades"
    missing = []
    malformed = []
    pair_overall = Counter({"k0 win": 0, "b1 win": 0, "tie": 0})
    pair_class = defaultdict(Counter)
    single_counts = {arm: Counter({**{verdict: 0 for verdict in VERDICTS}, "stale_true": 0, "stale_false": 0})
                     for arm in ARMS}
    question_outcomes = {qid: {"pair": "not graded", "singles": {arm: "not graded" for arm in ARMS}}
                         for qid in questions}

    for row in resolved:
        path = grades_dir / f"{row['task_id']}.json"
        if not path.is_file():
            missing.append(row["task_id"])
            continue
        grade, error = _read_grade(path, row["kind"])
        if error:
            malformed.append(row["task_id"])
            continue
        qid = row["question_id"]
        if row["kind"] == "pairwise":
            outcome = _pair_outcome(grade, row)
            question_outcomes[qid]["pair"] = outcome
            pair_overall[outcome] += 1
            pair_class[questions[qid]["class"]][outcome] += 1
        else:
            arm = row["arm"]
            single_counts[arm][grade["verdict"]] += 1
            single_counts[arm][f"stale_{str(grade['stale']).lower()}"] += 1
            stale_label = "stale" if grade["stale"] else "not stale"
            question_outcomes[qid]["singles"][arm] = f"{grade['verdict']} / {stale_label}"

    lines = [
        "# Set R blind grading report",
        "",
        "Arm-hint limitation: package wording may suggest an arm; task payloads omit arm labels, package IDs, and the key.",
        "Counts only; no significance claims.",
        "",
        "## Paired K0 vs B1",
        "",
        "| Scope | K0 wins | B1 wins | Ties |",
        "|---|---:|---:|---:|",
        f"| Overall | {pair_overall['k0 win']} | {pair_overall['b1 win']} | {pair_overall['tie']} |",
    ]
    for klass in sorted({question["class"] for question in questions.values()}):
        counts = pair_class[klass]
        lines.append(f"| {klass} | {counts['k0 win']} | {counts['b1 win']} | {counts['tie']} |")

    lines.extend([
        "",
        "## Single-answer results",
        "",
        "| Arm | Correct | Partial | Wrong | Unknown | Stale true | Stale false |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    for arm in ARMS:
        counts = single_counts[arm]
        lines.append("| " + arm + " | " + " | ".join(str(counts[key]) for key in
                     ("correct", "partial", "wrong", "unknown", "stale_true", "stale_false")) + " |")

    lines.extend([
        "",
        "## Per-question outcomes",
        "",
        "| Question ID | Class | K0/B1 pair | b0 single | b1 single | k0 single |",
        "|---|---|---|---|---|---|",
    ])
    for qid in sorted(questions):
        outcomes = question_outcomes[qid]
        singles = outcomes["singles"]
        lines.append(f"| {qid} | {questions[qid]['class']} | {outcomes['pair']} | "
                     f"{singles['b0']} | {singles['b1']} | {singles['k0']} |")

    lines.extend(["", "## Missing or malformed grade files", ""])
    if not missing and not malformed:
        lines.append("None.")
    else:
        lines.extend(("| Task ID | Status |", "|---|---|"))
        lines.extend(f"| {task_id} | missing |" for task_id in sorted(missing))
        lines.extend(f"| {task_id} | malformed |" for task_id in sorted(malformed))
    Path(out_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"pairs": sum(pair_overall.values()), "singles": sum(sum(single_counts[arm][v] for v in VERDICTS) for arm in ARMS),
            "missing": len(missing), "malformed": len(malformed)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build and tally blind Set R answer grades")
    commands = parser.add_subparsers(dest="command", required=True)

    answer_parser = commands.add_parser("answer-tasks")
    answer_parser.add_argument("--packages", nargs="+", required=True)
    answer_parser.add_argument("--out", required=True)

    grade_parser = commands.add_parser("grade-tasks")
    grade_parser.add_argument("--answers", required=True)
    grade_parser.add_argument("--packages", nargs="+", required=True)
    grade_parser.add_argument("--key", required=True)
    grade_parser.add_argument("--questions", required=True)
    grade_parser.add_argument("--out", required=True)
    grade_parser.add_argument("--seed", type=int, required=True)

    tally_parser = commands.add_parser("tally")
    tally_parser.add_argument("--grades", required=True)
    tally_parser.add_argument("--map", required=True)
    tally_parser.add_argument("--key", required=True)
    tally_parser.add_argument("--questions", required=True)
    tally_parser.add_argument("--out", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "answer-tasks":
            print(f"wrote {answer_tasks(args.packages, args.out)} answer tasks")
        elif args.command == "grade-tasks":
            print(f"wrote {grade_tasks(args.answers, args.packages, args.key, args.questions, args.out, args.seed)} grade tasks")
        else:
            counts = tally(args.grades, args.map, args.key, args.questions, args.out)
            print(f"tallied {counts['pairs']} pairs and {counts['singles']} singles; "
                  f"missing {counts['missing']}, malformed {counts['malformed']}")
    except (SetupError, OSError, UnicodeError, ValueError) as error:
        print(f"setup error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
