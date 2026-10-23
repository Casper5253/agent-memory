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
import re
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
from agentmem.config import Config
from agentmem.knowledge import policy as kp
from agentmem.knowledge.models import KnowledgeError
from agentmem.knowledge.proposals import _output_schema, build_proposal_input, extract_and_apply, serialize_model_input
from agentmem.knowledge.sources import import_capture_for_receipt
from agentmem.knowledge.store import KnowledgeStore
from agentmem.notes import session_metadata

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
    items = knowledge_entries(response["items"])
    return items, dict(backend="knowledge", hit_count=len(items), search_error="")


def knowledge_entries(response_items):
    """Render context items as texts with citations. An unresolved item carries its competing claims as alternatives."""
    entries = []
    for item in response_items:
        claims = [item] if "statement" in item else item.get("alternatives", [])
        for claim in claims:
            text = f"[{claim['status']}] {claim['statement']}"
            if claim.get("rationale"):
                text += f" Rationale: {claim['rationale']}"
            entries.append(dict(text=text, citations=claim["citations"]))
    return entries


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


def build_packages(vault_source, questions, arms, store_source, rng):
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
    rng.shuffle(packages)
    return packages, key_packages


def write_outputs(out, packages, key_packages, key_file, questions_sha, arms, seed):
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "packages.jsonl", "w", encoding="utf-8", newline="\n") as f:
        for package in packages:
            f.write(json.dumps({key: package[key] for key in PACKAGE_KEYS}, ensure_ascii=False) + "\n")
    key = dict(schema_version=1, questions_sha256=questions_sha, budget_tokens=BUDGET_TOKENS,
        tokenizer="ceil(utf8_bytes/3)", arms=list(arms), shuffle_seed=seed, packages=key_packages)
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
    seed = args.seed if args.seed is not None else secrets.randbits(32)
    packages, key_packages = build_packages(vault_source, questions, arms, args.store, random.Random(seed))
    key_file = Path(args.key) if args.key else out / "key.json"
    write_outputs(out, packages, key_packages, key_file, questions_sha, arms, seed)
    return packages, key_packages, arms


PROJECT_PRIVACY = "model_allowed"
SESSIONS_DIR = "0-Sessions"


def normalized_name(name):
    return re.sub(r"[^a-z0-9]+", " ", name.casefold()).strip()


def project_id_for(name):
    return kp.uuid_identity(["set-r-project", name])


def project_config(vault, names):
    """Knowledge config built in code, so the user's runtime config is never read."""
    return Config(vault=vault, knowledge_project_ids={name: project_id_for(name) for name in names},
                  project_privacy={name: PROJECT_PRIVACY for name in names})


def load_fragment_map(sweep_plan):
    """Merged fragment folder to target project, read from the applied sweep plan."""
    plan = json.loads(sweep_plan.read_text(encoding="utf-8"))
    return {normalized_name(entry["folder"]): entry["proposed_parent"]
            for entry in plan.get("fragments", []) if entry.get("auto_apply")}


def session_notes(vault, names, fragments=None):
    """Session notes of the named projects. A fragment name resolves to its target first. Malformed notes are listed."""
    wanted = {normalized_name(name): name for name in names}
    fragments = fragments or {}
    notes, skipped = [], []
    for path in sorted((vault / SESSIONS_DIR).rglob("*.md")):
        rel = path.relative_to(vault).as_posix()
        text = path.read_text(encoding="utf-8")
        if not re.search(r"(?m)^type: session$", text):
            continue
        try:
            fields = session_metadata(text)
        except ValueError as error:
            skipped.append(dict(session=rel, reason=str(error)))
            continue
        frontmatter_project = fields.get("project", "")
        project = wanted.get(normalized_name(fragments.get(normalized_name(frontmatter_project), frontmatter_project)))
        if project is None:
            continue
        if "date" not in fields:
            skipped.append(dict(session=rel, reason="missing_date"))
            continue
        notes.append(dict(path=rel, text=text, fields=fields, part=int(fields.get("part", "1")), project=project))
    return notes, skipped


def session_receipt(note):
    """Receipt shape the knowledge import path takes for a note without a capture receipt."""
    fields = note["fields"]
    return dict(agent=fields["agent"], session_id=fields["session_id"], part=note["part"], project=note["project"],
        digest_sha=kp.sha_text(note["text"]), note_path=note["path"], text=note["text"],
        content_sha=kp.sha_text(note["text"]), when=fields["date"] + "T00:00:00Z", repo_head=None)


def run_batches(vault, questions_path, out, fragments=None):
    if not vault.is_dir():
        raise SetupError("--vault is not a directory")
    if (out / "index.jsonl").exists():
        raise SetupError("--out already holds index.jsonl; refusing to overwrite")
    check_frozen(questions_path)
    names = sorted({question["project"] for question in load_questions(questions_path)})
    cfg = project_config(vault, names)
    notes, skipped = session_notes(vault, names, fragments)
    out.mkdir(parents=True, exist_ok=True)
    rows, seen = [], set()
    with tempfile.TemporaryDirectory(prefix="set-r-batches-", dir=str(ek.repo_tmp())) as directory:
        with closing(KnowledgeStore.create(Path(directory) / "scratch.sqlite")) as store:
            for note in notes:
                identity = (note["fields"]["agent"], note["fields"]["session_id"], note["part"])
                if identity in seen:
                    skipped.append(dict(session=note["path"], reason="duplicate_session_identity"))
                    continue
                seen.add(identity)
                receipt = session_receipt(note)
                try:
                    outcome = import_capture_for_receipt(cfg, store, receipt)
                except KnowledgeError as error:
                    skipped.append(dict(session=note["path"], reason=error.code))
                    continue
                if outcome.status != "imported":
                    skipped.append(dict(session=note["path"], reason=outcome.reason))
                    continue
                source_ids = list(outcome.import_result.source_ids)
                prompt = serialize_model_input(build_proposal_input(store, source_ids[0]))
                batch_id = kp.sha_text(note["path"])[:16]
                batch = dict(batch=batch_id, session=note["path"], project=note["project"],
                    project_id=project_id_for(note["project"]), agent=receipt["agent"], session_id=receipt["session_id"],
                    part=note["part"], content_sha=receipt["content_sha"], source_ids=source_ids,
                    prompt=prompt, output_schema=_output_schema(), tokens=tokens(prompt))
                (out / f"{batch_id}.json").write_text(json.dumps(batch, indent=2, ensure_ascii=False), encoding="utf-8")
                rows.append(dict(batch=batch_id, session=note["path"], project=note["project"], tokens=batch["tokens"]))
    (out / "index.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    return rows, skipped


def run_import(vault, batches, proposals, store_path):
    if not vault.is_dir():
        raise SetupError("--vault is not a directory")
    if store_path.exists():
        raise SetupError("--store already exists; refusing to reuse it")
    skipped, rejected = [], []
    docs = []
    for path in sorted(batches.glob("*.json")):
        try:
            docs.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            skipped.append(dict(batch=path.stem, reason="invalid_batch_file"))
    cfg = project_config(vault, sorted({doc["project"] for doc in docs}))
    imported = 0
    created = 0
    store_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(KnowledgeStore.create(store_path)) as store:
        ready = []
        for doc in docs:
            note_file = vault / doc["session"]
            if not note_file.is_file():
                skipped.append(dict(batch=doc["batch"], reason="note_missing"))
                continue
            text = note_file.read_text(encoding="utf-8")
            if kp.sha_text(text) != doc["content_sha"]:
                skipped.append(dict(batch=doc["batch"], reason="note_changed_since_batch"))
                continue
            try:
                fields = session_metadata(text)
            except ValueError as error:
                skipped.append(dict(batch=doc["batch"], reason=str(error)))
                continue
            note = dict(path=doc["session"], text=text, fields=fields, part=doc["part"], project=doc["project"])
            try:
                outcome = import_capture_for_receipt(cfg, store, session_receipt(note))
            except KnowledgeError as error:
                skipped.append(dict(batch=doc["batch"], reason=error.code))
                continue
            if outcome.status != "imported" or list(outcome.import_result.source_ids) != doc["source_ids"]:
                skipped.append(dict(batch=doc["batch"], reason="source_id_mismatch"))
                continue
            ready.append(doc)
    for doc in ready:
        proposal = proposals / f"{doc['batch']}.json"
        if not proposal.is_file():
            skipped.append(dict(batch=doc["batch"], reason="no_proposal"))
            continue
        raw = proposal.read_text(encoding="utf-8")
        try:
            json.loads(raw)
        except ValueError:
            skipped.append(dict(batch=doc["batch"], reason="invalid_json"))
            continue
        try:
            result = extract_and_apply(str(store_path), doc["source_ids"][0], lambda _prompt, raw=raw: raw,
                                       cfg=cfg, extractor_version="haiku-v1")
        except KnowledgeError as error:
            rejected.append(dict(batch=doc["batch"], reason=error.code))
            continue
        imported += 1
        created += len(result.created_claim_ids)
    return dict(batches=len(docs), imported=imported, created_claims=created, rejected=rejected, skipped=skipped)


def batches_cli(args):
    fragments = load_fragment_map(Path(args.sweep_plan))
    rows, skipped = run_batches(Path(args.vault), Path(args.questions), Path(args.out), fragments)
    for item in skipped:
        print("skipped: " + json.dumps(item, ensure_ascii=False))
    print(f"batches {len(rows)} tokens {sum(row['tokens'] for row in rows)} skipped {len(skipped)} -> {args.out}")
    return 0


def import_cli(args):
    counts = run_import(Path(args.vault), Path(args.batches), Path(args.proposals), Path(args.store))
    for item in counts["rejected"]:
        print("rejected: " + json.dumps(item, ensure_ascii=False))
    for item in counts["skipped"]:
        print("skipped: " + json.dumps(item, ensure_ascii=False))
    print(f"batches {counts['batches']} imported {counts['imported']} rejected {len(counts['rejected'])} "
          f"skipped {len(counts['skipped'])} claims {counts['created_claims']}")
    return 0


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
    packages.add_argument("--seed", type=int, help="shuffle seed; recorded in key.json (random when omitted)")
    batches = commands.add_parser("batches", help="one extraction batch per session note of the named projects")
    batches.add_argument("--vault", required=True)
    batches.add_argument("--questions", required=True)
    batches.add_argument("--out", required=True)
    batches.add_argument("--sweep-plan", required=True, help="applied sweep plan; its fragment merges map projects")
    importer = commands.add_parser("import", help="import batches and apply hand-written or model proposals as reported")
    importer.add_argument("--vault", required=True)
    importer.add_argument("--batches", required=True)
    importer.add_argument("--proposals", required=True)
    importer.add_argument("--store", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "batches":
            return batches_cli(args)
        if args.command == "import":
            return import_cli(args)
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
