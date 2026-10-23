"""Set R package builder: one blinded context package per (question, arm).

Usage:
    python -I tools/evaluate_set_r.py packages --vault PATH --questions PATH --arms b0,b1[,k0] [--store PATH] --out DIR

Writes packages.jsonl (shuffled, no arm names) and key.json (package_id to question and arm).
Every arm runs in its own copy of the vault, under the repo scratch directory, through the
evaluator's isolated environment. The given vault is only copied, never written. Never calls a model.
"""
import argparse
import json
import math
import random
import secrets
import shutil
import sqlite3
import sys
import tempfile
from contextlib import ExitStack, closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools import evaluate_knowledge as ek
from tools.knowledge_grader import formulate_query

BUDGET_TOKENS = 2400
ARM_LABELS = ("b0", "b1", "k0")
ARM_SOURCE = {"b0": "baseline", "b1": "reliability"}
REQUIRED_KEYS = ("id", "project", "class", "question", "gold_answer", "gold_evidence", "stale_trap", "as_of_commit")
PACKAGE_KEYS = ("package_id", "question", "project", "context_text", "citations", "tokens")


class SetupError(Exception):
    """A precondition for a set R package run is not met."""


def tokens(text):
    """Byte-fallback estimate used for every arm: ceil(UTF-8 bytes / 3)."""
    return math.ceil(len(text.encode("utf-8")) / 3)


def check_frozen(questions):
    frozen = questions.parent / "FROZEN.sha256"
    if not frozen.is_file():
        raise SetupError("FROZEN.sha256 is missing next to " + questions.name)
    pinned = None
    for line in frozen.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].lstrip("*") == questions.name:
            pinned = parts[0]
    if pinned is None:
        raise SetupError("FROZEN.sha256 has no entry for " + questions.name)
    actual = ek.sha256_file(questions)
    if actual != pinned:
        raise SetupError(f"{questions.name} sha256 {actual} does not match frozen {pinned}")
    return actual


def load_questions(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for number, line in enumerate(f, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            missing = [key for key in REQUIRED_KEYS if key not in row]
            if missing:
                raise SetupError(f"{path.name} line {number} lacks {', '.join(missing)}")
            rows.append(row)
    return rows


def new_package_id(used):
    while True:
        package_id = "p" + secrets.token_hex(8)
        if package_id not in used and not any(label in package_id for label in ARM_LABELS):
            return package_id


def fit(items):
    """Keep items in order while the rendered context fits the shared budget."""
    kept = []
    for item in items:
        candidate = kept + [item]
        if tokens("\n\n".join(entry["text"] for entry in candidate)) > BUDGET_TOKENS:
            break
        kept.append(candidate[-1])
    context_text = "\n\n".join(entry["text"] for entry in kept)
    citations = [citation for entry in kept for citation in entry["citations"]]
    return context_text, citations, len(kept)


def baseline_items(arm, question_text):
    result = ek.package_baseline_context(arm["vault"], arm["home"], {"query": formulate_query(question_text),
        "budget": BUDGET_TOKENS}, qmd_available=arm["qmd_available"], cwd=arm["base"], cache_dir=arm["cache"],
        max_bytes=BUDGET_TOKENS * 3, source_root=arm["source_root"])
    items = [dict(text=snippet["content"], citations=[dict(path=snippet["path"], label=snippet["label"])])
             for snippet in result["snippets"]]
    diagnostics = dict(backend=result["backend"], hit_count=result["hit_count"], search_error=result["search_error"])
    return items, diagnostics


def knowledge_items(arm, question):
    from agentmem.knowledge.retrieval import ContextRequest, build_context
    from agentmem.knowledge.store import KnowledgeStore
    with KnowledgeStore(arm["store"], vault=arm["vault"]) as store:
        response = build_context(store, ContextRequest(formulate_query(question["question"]),
            project=question["project"], budget=BUDGET_TOKENS)).to_dict()
    if response.get("status") != "ok":
        return [], dict(backend="knowledge", hit_count=0, search_error=str(response.get("errors", response.get("status"))))
    items = []
    for item in response["items"]:
        text = f"[{item['status']}] {item['statement']}"
        if item.get("rationale"):
            text += f" Rationale: {item['rationale']}"
        items.append(dict(text=text, citations=item["citations"]))
    return items, dict(backend="knowledge", hit_count=len(items), search_error="")


def prepare_baseline(stack, base, arm_name, vault_source):
    vault = base / "vault"
    shutil.copytree(vault_source, vault)
    home = base / "home"
    home.mkdir(parents=True)
    cache = base / "cache"
    source_root, commit = stack.enter_context(ek.arm_source(base, ARM_SOURCE[arm_name]))
    rc, _, stderr = ek.run_mem_command(vault, home, ["index", "--no-embed"], timeout=120, cwd=base,
        cache_dir=cache, source_root=source_root)
    if rc != 0:
        raise SetupError(f"{arm_name} index failed: {stderr[:300]}")
    qmd_status, _ = ek.detect_qmd(env=ek.isolated_environment(vault, home, cache), cwd=base)
    return dict(base=base, vault=vault, home=home, cache=cache, source_root=source_root, commit=commit,
        qmd_available=qmd_status == "ok")


def prepare_knowledge(base, vault_source, store_source):
    vault = base / "vault"
    shutil.copytree(vault_source, vault)
    store = base / "knowledge.sqlite"
    with closing(sqlite3.connect(f"{Path(store_source).resolve().as_uri()}?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(store)) as target:
            source.backup(target)
    return dict(vault=vault, store=store, commit=None)


def build_packages(vault_source, questions, arms, store_source):
    packages, key_packages = [], {}
    with tempfile.TemporaryDirectory(prefix="set-r-", dir=str(ek.repo_tmp())) as directory, ExitStack() as stack:
        tmp = Path(directory)
        prepared = {}
        for arm_name in arms:
            base = tmp / arm_name
            base.mkdir()
            if arm_name == "k0":
                prepared[arm_name] = prepare_knowledge(base, vault_source, store_source)
            else:
                prepared[arm_name] = prepare_baseline(stack, base, arm_name, vault_source)
        for question in questions:
            for arm_name in arms:
                arm = prepared[arm_name]
                if arm_name == "k0":
                    items, diagnostics = knowledge_items(arm, question)
                else:
                    items, diagnostics = baseline_items(arm, question["question"])
                context_text, citations, kept_items = fit(items)
                package_id = new_package_id(key_packages)
                packages.append(dict(package_id=package_id, question=question["question"], project=question["project"],
                    context_text=context_text, citations=citations, tokens=tokens(context_text)))
                key_packages[package_id] = dict(question_id=question["id"], arm=arm_name,
                    arm_source_commit=arm["commit"], kept_items=kept_items, **diagnostics)
    random.Random().shuffle(packages)
    return packages, key_packages


def write_outputs(out, packages, key_packages, key_file, questions_sha, arms):
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "packages.jsonl", "w", encoding="utf-8", newline="\n") as f:
        for package in packages:
            f.write(json.dumps({key: package[key] for key in PACKAGE_KEYS}, ensure_ascii=False) + "\n")
    key = dict(schema_version=1, questions_sha256=questions_sha, budget_tokens=BUDGET_TOKENS,
        tokenizer="ceil(utf8_bytes/3)", arms=list(arms), packages=key_packages)
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text(json.dumps(key, indent=2, ensure_ascii=False), encoding="utf-8")


def run_packages(args):
    vault_source = Path(args.vault)
    questions_path = Path(args.questions)
    out = Path(args.out)
    if not vault_source.is_dir():
        raise SetupError("--vault is not a directory")
    if (out / "packages.jsonl").exists():
        raise SetupError("--out already holds packages.jsonl; refusing to overwrite")
    questions_sha = check_frozen(questions_path)
    questions = load_questions(questions_path)
    arms = [arm.strip() for arm in args.arms.split(",") if arm.strip()]
    unknown = [arm for arm in arms if arm not in ARM_LABELS]
    if unknown:
        raise SetupError("unknown arms: " + ", ".join(unknown))
    if "k0" in arms and not args.store:
        print("skip k0: --store not given", file=sys.stderr)
        arms = [arm for arm in arms if arm != "k0"]
    if "k0" in arms and not Path(args.store).is_file():
        raise SetupError("--store is not a file")
    packages, key_packages = build_packages(vault_source, questions, arms, args.store)
    key_file = Path(args.key) if args.key else out / "key.json"
    write_outputs(out, packages, key_packages, key_file, questions_sha, arms)
    return packages, key_packages, arms


def main(argv=None):
    parser = argparse.ArgumentParser(description="Set R blinded context packages")
    commands = parser.add_subparsers(dest="command", required=True)
    packages = commands.add_parser("packages", help="build one package per (question, arm)")
    packages.add_argument("--vault", required=True)
    packages.add_argument("--questions", required=True)
    packages.add_argument("--arms", default="b0,b1")
    packages.add_argument("--store")
    packages.add_argument("--out", required=True)
    packages.add_argument("--key")
    args = parser.parse_args(argv)
    try:
        packages, key_packages, arms = run_packages(args)
    except SetupError as error:
        print("setup error: " + str(error), file=sys.stderr)
        return 2
    for arm in arms:
        entries = [entry for entry in key_packages.values() if entry["arm"] == arm]
        empty = sorted({entry["question_id"] for entry in entries if entry["kept_items"] == 0})
        print(f"{arm}: {len(entries)} packages, {len(empty)} empty: {', '.join(empty) or 'none'}")
    print(f"wrote {len(packages)} packages to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
