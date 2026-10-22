"""WP-00 knowledge evaluation harness. Baseline arm only.

Usage:
    python -I tools/evaluate_knowledge.py --arm baseline --fixtures tests/fixtures/knowledge --output .audit/b0

Exit codes: 0 pass, 1 failed adoption gate, 2 invalid setup, 3 inconclusive.
Stdlib only. Never touches the real vault. Never calls a model or the network.
"""
import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXIT_PASS, EXIT_FAIL, EXIT_SETUP, EXIT_INCONCLUSIVE = 0, 1, 2, 3


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def load_fixtures(fixtures_dir):
    fixtures_dir = Path(fixtures_dir)
    required = ["manifest.json", "corpus.json", "queries.jsonl", "expected.jsonl"]
    missing = [name for name in required if not (fixtures_dir / name).is_file()]
    if missing:
        return None, None, None, None, None, missing
    manifest = json.loads((fixtures_dir / "manifest.json").read_text(encoding="utf-8"))
    corpus = json.loads((fixtures_dir / "corpus.json").read_text(encoding="utf-8"))
    queries = []
    with open(fixtures_dir / "queries.jsonl", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                queries.append(json.loads(line))
    expected = {}
    with open(fixtures_dir / "expected.jsonl", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                row = json.loads(line)
                expected[row["query_id"]] = row["gold"]
    hashes = {}
    for name in required:
        hashes[name] = sha256_file(fixtures_dir / name)
    return manifest, corpus, queries, expected, hashes, None


def check_fixture_hashes(manifest, hashes):
    declared = manifest.get("files", {})
    mismatches = []
    for name, h in declared.items():
        if name in hashes and hashes[name] != h:
            mismatches.append(f"{name}: expected {h}, got {hashes[name]}")
    return mismatches


def make_temp_vault(base_tmp, corpus):
    vault = base_tmp / "vault"
    for d in ("_meta/queue", "_meta/locks", "_meta/logs", "0-Sessions", "1-Projects",
              "2-Areas", "3-Resources", "4-Archive"):
        (vault / d).mkdir(parents=True, exist_ok=True)
    work = base_tmp / "work"
    vault_path = vault
    project_roots = {}
    for proj in corpus.get("projects", []):
        roots = []
        for r in proj.get("roots", []):
            r = r.replace("{WORK}", str(work)).replace("{VAULT}", str(vault_path))
            Path(r).mkdir(parents=True, exist_ok=True)
            roots.append(r)
        project_roots[proj["id"]] = {"name": proj["name"], "roots": roots, "status": proj.get("status", "active"),
                                     "policy": proj.get("policy", "model_allowed")}
    (vault / "_meta" / "projects.json").write_text(json.dumps({
        "projects": [{"name": v["name"], "roots": v["roots"], "status": v["status"]}
                     for v in project_roots.values()]
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    home = base_tmp / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(json.dumps({
        "summarizer": "none", "auto_index": False, "embeddings": False
    }), encoding="utf-8")
    return vault, work, home, project_roots


def _make_replay_summarizer(responses_by_session):
    def _summarize(digest_text, timeout=0):
        for key, resp in responses_by_session.items():
            if isinstance(resp, str):
                try:
                    data = json.loads(resp)
                    data["_cost_usd"] = 0.0
                    return data
                except json.JSONDecodeError:
                    pass
        return {"title": "session", "summary": "replayed", "what_happened": [],
                "decisions": [], "facts": [], "open_questions": [], "next_steps": [], "_cost_usd": 0.0}
    return _summarize


def run_pipeline_for_corpus(vault, work, home, corpus, project_roots):
    sys.path.insert(0, str(ROOT))
    from agentmem.config import Config
    from agentmem.manifest import Manifest
    from agentmem import worker

    cfg = Config(vault=vault, summarizer="none", settle_minutes=0, auto_index=False, embeddings=False)

    transcripts_dir = Path(corpus.get("_transcripts_dir", ""))
    responses = {}
    for sess in corpus.get("sessions", []):
        sid = sess["session_id"]
        responses[sid] = sess.get("summary_response", "")

    replay = _make_replay_summarizer(responses)

    for sess in corpus.get("sessions", []):
        sid = sess["session_id"]
        agent = sess["agent"]
        transcript_rel = sess.get("transcript", "")
        transcript_path = transcripts_dir / transcript_rel if transcripts_dir.is_dir() else None

        if transcript_path and transcript_path.is_file():
            dest = vault / "_meta" / "transcripts" / agent / transcript_path.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(transcript_path), str(dest))
            transcript_path = dest

        cwd = sess.get("cwd", "").replace("{WORK}", str(work)).replace("{VAULT}", str(vault))

        man = Manifest(cfg.manifest_path)
        try:
            man.touch(agent, sid, now=time.time() - 7200, source=str(transcript_path) if transcript_path else None,
                      end=True, cwd=cwd)
        finally:
            man.close()

        if transcript_path and transcript_path.is_file():
            worker.enqueue(cfg, agent, sid, event="end", source=str(transcript_path), cwd=cwd)

    results = worker.run(cfg, budget_s=60, summarizer=replay, consolidator=None, index=False)
    sys.path.pop(0)
    return cfg, results


def detect_qmd():
    shim = shutil.which("qmd")
    if not shim:
        return "unavailable", ""
    node = shutil.which("node")
    js = Path(shim).parent / "node_modules" / "@tobilu" / "qmd" / "bin" / "qmd" if node else None
    version = ""
    try:
        cmd = [str(js)] if (node and js and js.exists()) else [shim]
        r = subprocess.run(cmd + ["--version"], capture_output=True, timeout=10,
                           env=dict(os.environ, NO_COLOR="1"))
        version = r.stdout.decode("utf-8", "replace").strip()[:120]
    except Exception:
        pass
    return "ok", version


def run_mem_command(vault, home, args, timeout=30):
    env = dict(os.environ,
               OBSIDIAN_VAULT_PATH=str(vault),
               AGENTMEM_HOME=str(home),
               AGENTMEM_CAPTURE="0")
    env.pop("AGENTMEM_CAPTURE", None)
    try:
        r = subprocess.run(
            [sys.executable, "-I", str(ROOT / "run.py")] + args,
            capture_output=True, timeout=timeout, env=env, cwd=str(ROOT)
        )
        return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as e:
        return -1, "", str(e)


def package_baseline_context(vault, home, query, max_bytes=None):
    budget = query.get("budget", 2400)
    limit = max_bytes or budget
    terms = query["query"]
    rc, stdout, stderr = run_mem_command(vault, home, ["search", *terms.split(), "--json", "-n", "8"])
    hits = []
    if rc == 0 and stdout.strip():
        try:
            hits = json.loads(stdout)
        except json.JSONDecodeError:
            pass

    snippets = []
    total = 0
    for hit in hits:
        if total >= limit:
            break
        path = hit.get("path", "")
        note_rc, note_out, _ = run_mem_command(vault, home, ["note", path])
        content = note_out if note_rc == 0 else hit.get("snippet", "")
        snippet = {"path": path, "label": hit.get("label", ""), "score": hit.get("score"),
                    "content": content[:800]}
        blob = json.dumps(snippet, ensure_ascii=False)
        if total + len(blob.encode("utf-8")) > limit:
            break
        snippets.append(snippet)
        total += len(blob.encode("utf-8"))

    return {"snippets": snippets, "hit_count": len(hits), "packed_bytes": total,
            "qmd_used": True, "budget": limit}


def grade_query(query, gold, context):
    outcome = {"query_id": query["query_id"], "pass": True, "failures": []}

    combined = ""
    current_combined = ""
    source_map = {}
    for sn in context["snippets"]:
        combined += sn.get("content", "") + "\n"
        if sn.get("label") == "CURRENT":
            current_combined += sn.get("content", "") + "\n"
        source_map[sn.get("path", "")] = sn

    for claim_id in gold.get("required_claims", []):
        if claim_id not in combined:
            outcome["failures"].append(f"missing_required_claim: {claim_id}")
            outcome["pass"] = False

    for claim_id in gold.get("forbidden_current_claims", []):
        if claim_id in current_combined:
            outcome["failures"].append(f"forbidden_current_claim_present: {claim_id}")
            outcome["pass"] = False

    for s in gold.get("forbidden_strings", []):
        if s in combined:
            outcome["failures"].append(f"forbidden_string_present: {s[:60]}")
            outcome["pass"] = False

    rationale = gold.get("required_rationale")
    if rationale and rationale not in combined:
        outcome["failures"].append(f"missing_rationale: {rationale[:60]}")
        outcome["pass"] = False

    if gold.get("expected_uncertainty") and not any(w in combined.lower() for w in ["unresolved", "contradict", "uncertain", "disagree"]):
        outcome["failures"].append("expected_uncertainty_not_signaled")
        outcome["pass"] = False

    if not gold.get("expected_uncertainty") and gold.get("required_claims") and not context["snippets"]:
        outcome["failures"].append("empty_context_for_answerable_query")
        outcome["pass"] = False

    outcome["snippet_count"] = len(context["snippets"])
    outcome["packed_bytes"] = context["packed_bytes"]
    return outcome


def lifecycle_check(vault, corpus):
    sessions_dir = vault / "0-Sessions"
    notes_written = list(sessions_dir.rglob("*.md"))
    expected_notes = len(corpus.get("sessions", []))
    passed = len(notes_written) > 0
    return {"passed": passed, "notes_written": len(notes_written),
            "expected_sessions": expected_notes}


def write_outputs(output_dir, result, per_query_outcomes, corpus):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    result["per_query_count"] = len(per_query_outcomes)
    result["timestamp"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (output_dir / "result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    with open(output_dir / "per_query.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["query_id", "pass", "snippet_count", "packed_bytes", "failures"])
        for o in per_query_outcomes:
            w.writerow([o["query_id"], o["pass"], o.get("snippet_count", 0),
                        o.get("packed_bytes", 0), "; ".join(o.get("failures", []))])

    lines = [f"# Baseline evaluation report",
             f"",
             f"- Commit: {result.get('commit', 'unknown')}",
             f"- Python: {result.get('python_version', '')}",
             f"- SQLite: {result.get('sqlite_version', '')}",
             f"- QMD: {result.get('qmd_status', '')}",
             f"- Verdict: {result.get('verdict', '')}",
             f"- Correctness pass: {result.get('correctness_pass', '')}",
             f"- Adoption pass: {result.get('adoption_pass', '')}",
             f"",
             f"## Per-query results",
             f""]
    passed = sum(1 for o in per_query_outcomes if o["pass"])
    total = len(per_query_outcomes)
    lines.append(f"Passed: {passed}/{total}")
    lines.append("")
    for o in per_query_outcomes:
        if not o["pass"]:
            lines.append(f"- FAIL {o['query_id']}: {'; '.join(o.get('failures', []))}")
    lines.append("")
    lines.append("## Limitations")
    for lim in result.get("limitations", []):
        lines.append(f"- {lim}")
    (output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def get_commit():
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, timeout=5, cwd=str(ROOT))
        return r.stdout.decode().strip() if r.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def run_baseline(fixtures_dir, output_dir):
    result = load_fixtures(fixtures_dir)
    if result[5] is not None:
        output = {"verdict": "setup_error", "exit_code": EXIT_SETUP, "errors": result[5],
                  "correctness_pass": False, "adoption_pass": False, "limitations": []}
        write_outputs(output_dir, output, [], {})
        return EXIT_SETUP
    manifest, corpus, queries, expected, hashes, _ = result

    hash_mismatches = check_fixture_hashes(manifest, hashes)
    if hash_mismatches:
        result = {"verdict": "setup_error", "exit_code": EXIT_SETUP,
                  "errors": hash_mismatches, "correctness_pass": False, "adoption_pass": False,
                  "limitations": []}
        write_outputs(output_dir, result, [], corpus)
        return EXIT_SETUP

    with tempfile.TemporaryDirectory(prefix="agentmem-eval-") as tmp:
        tmp = Path(tmp)
        transcripts_dir = Path(fixtures_dir) / "transcripts"
        corpus["_transcripts_dir"] = str(transcripts_dir) if transcripts_dir.is_dir() else ""

        vault, work, home, project_roots = make_temp_vault(tmp, corpus)
        cfg, pipeline_results = run_pipeline_for_corpus(vault, work, home, corpus, project_roots)

        qmd_status, qmd_version = detect_qmd()

        per_query_outcomes = []
        total_packed = 0
        total_snippets = 0
        for q in queries:
            qid = q["query_id"]
            gold = expected.get(qid, {})
            context = package_baseline_context(vault, home, q)
            outcome = grade_query(q, gold, context)
            per_query_outcomes.append(outcome)
            total_packed += context["packed_bytes"]
            total_snippets += len(context["snippets"])

        lifecycle = lifecycle_check(vault, corpus)
        if not lifecycle["passed"]:
            for o in per_query_outcomes:
                if o["pass"]:
                    o["pass"] = False
                    o["failures"].append("lifecycle_grader:no_notes_persisted")

        passed = sum(1 for o in per_query_outcomes if o["pass"])
        total = len(per_query_outcomes)
        correctness_pass = passed == total and total > 0 and lifecycle["passed"]

        limitations = []
        if qmd_status == "unavailable":
            limitations.append("QMD not installed; baseline used fallback search")
        if not corpus.get("sessions"):
            limitations.append("no sessions in corpus")

        result = {
            "commit": get_commit(),
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "sqlite_version": sqlite3.sqlite_version,
            "qmd_status": qmd_status,
            "qmd_version": qmd_version,
            "tokenizer": "byte-fallback",
            "fixture_hashes": hashes,
            "counters": {
                "total_queries": total,
                "passed": passed,
                "failed": total - passed,
                "total_packed_bytes": total_packed,
                "total_snippets": total_snippets,
            },
            "costs": {"model_usd": None, "reason": "baseline uses replay model; no real calls"},
            "lifecycle": lifecycle,
            "correctness_pass": correctness_pass,
            "adoption_pass": correctness_pass,
            "verdict": "pass" if correctness_pass else ("inconclusive" if total == 0 else "fail"),
            "limitations": limitations,
        }

        write_outputs(output_dir, result, per_query_outcomes, corpus)

        if total == 0:
            return EXIT_INCONCLUSIVE
        return EXIT_PASS if correctness_pass else EXIT_FAIL


def main():
    p = argparse.ArgumentParser(description="WP-00 knowledge evaluation harness")
    p.add_argument("--arm", choices=["baseline"], required=True)
    p.add_argument("--fixtures", required=True)
    p.add_argument("--output", required=True)
    args = p.parse_args()

    if args.arm != "baseline":
        print(f"arm {args.arm} not implemented", file=sys.stderr)
        sys.exit(EXIT_SETUP)

    sys.exit(run_baseline(args.fixtures, args.output))


if __name__ == "__main__":
    main()
