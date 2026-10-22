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


class SetupError(Exception):
    """A fixture or runtime precondition is violated."""


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


def _replace_placeholders(s, work, vault):
    return s.replace("{WORK}", str(work)).replace("{VAULT}", str(vault))


def make_temp_vault(base_tmp, corpus, qmd_index=""):
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
            r = _replace_placeholders(r, work, vault_path)
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
    config = {
        "summarizer": "none",
        "auto_index": False,
        "embeddings": False,
    }
    if qmd_index:
        config["qmd_index"] = str(qmd_index)
    (home / "config.json").write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    return vault, work, home, project_roots


def _replace_placeholders_in_json(obj, work, vault):
    if isinstance(obj, str):
        return obj.replace("{WORK}", str(work)).replace("{VAULT}", str(vault))
    if isinstance(obj, list):
        return [_replace_placeholders_in_json(x, work, vault) for x in obj]
    if isinstance(obj, dict):
        return {k: _replace_placeholders_in_json(v, work, vault) for k, v in obj.items()}
    return obj


def _copy_transcript(src, dest, work, vault):
    """Copy a transcript, substituting {WORK} and {VAULT} with temp paths."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    text = src.read_text(encoding="utf-8")
    if src.suffix == ".jsonl":
        out_lines = []
        for line in text.splitlines(keepends=True):
            stripped = line.strip()
            if not stripped:
                out_lines.append(line)
                continue
            try:
                rec = json.loads(stripped)
                rec = _replace_placeholders_in_json(rec, work, vault)
                out_lines.append(json.dumps(rec, ensure_ascii=False) + "\n")
            except json.JSONDecodeError:
                out_lines.append(line)
        dest.write_text("".join(out_lines), encoding="utf-8")
    else:
        try:
            rec = json.loads(text)
            rec = _replace_placeholders_in_json(rec, work, vault)
            dest.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        except json.JSONDecodeError:
            dest.write_text(_replace_placeholders(text, work, vault), encoding="utf-8")


def _make_replay_summarizer(responses_by_digest):
    """Return a summarizer that returns the fixture response matching the digest text.

    The mapping is keyed by SHA-256 of the exact digest the pipeline builds, so the
    response is tied to the specific session and part being captured.
    """
    def _summarize(digest_text, timeout=0):
        key = sha256_bytes(digest_text.encode("utf-8"))
        if key not in responses_by_digest:
            raise SetupError(f"no fixture summary for digest {key[:16]}; "
                             "replay model cannot match this session/part")
        data = json.loads(responses_by_digest[key])
        data["_cost_usd"] = 0.0
        return data
    return _summarize


def _noop_consolidator(state, summary, link, date, timeout=0):
    """Avoid real model calls during project-note reconciliation."""
    return state or "", 0.0


def run_pipeline_for_corpus(vault, work, home, corpus, project_roots, qmd_index):
    sys.path.insert(0, str(ROOT))
    from agentmem.adapters import read as adapter_read
    from agentmem.config import Config
    from agentmem.extract import build as extract_build
    from agentmem import llm
    from agentmem.manifest import Manifest
    from agentmem import worker

    cfg = Config(
        vault=vault,
        summarizer="claude-haiku",
        settle_minutes=0,
        auto_index=False,
        embeddings=False,
        qmd_index=str(qmd_index) if qmd_index else "",
    )

    transcripts_dir = Path(corpus.get("_transcripts_dir", ""))
    if not transcripts_dir.is_dir():
        raise SetupError(f"transcripts directory not found: {transcripts_dir}")

    # Copy every transcript into the temp vault and resolve source paths.
    source_paths = {}
    opencode_sources = {}
    missing_response = []
    for sess in corpus.get("sessions", []):
        sid = sess["session_id"]
        agent = sess["agent"]
        part = sess.get("part", 1)
        if not sess.get("summary_response", "").strip():
            missing_response.append((sid, part))
        transcript_rel = sess.get("transcript", "")
        src = transcripts_dir / transcript_rel
        if not src.is_file():
            raise SetupError(f"transcript not found: {src}")
        dest = vault / "_meta" / "transcripts" / agent / Path(transcript_rel).name
        _copy_transcript(src, dest, work, vault)
        source_paths[(agent, sid, part)] = str(dest)
        if agent == "opencode":
            opencode_sources[sid] = str(dest)

    if missing_response:
        raise SetupError(f"missing summary_response for {len(missing_response)} session parts")

    # Replay opencode exports from the copied local JSON files instead of calling CLI.
    original_export_opencode = None
    if opencode_sources:
        import agentmem.adapters as adapters_mod
        original_export_opencode = adapters_mod.export_opencode

        def _replay_export_opencode(session_id, timeout=120):
            path = opencode_sources.get(session_id)
            if not path or not Path(path).is_file():
                raise FileNotFoundError(f"opencode export fixture missing for {session_id}")
            return json.loads(Path(path).read_text(encoding="utf-8"))

        adapters_mod.export_opencode = _replay_export_opencode

    try:
        # Pre-compute digest -> summary_response so the replay summarizer can identify
        # the exact session and part from the digest the pipeline will build.
        responses_by_digest = {}
        for sess in corpus.get("sessions", []):
            sid = sess["session_id"]
            agent = sess["agent"]
            part = sess.get("part", 1)
            source = source_paths[(agent, sid, part)]
            if agent == "opencode":
                t = adapter_read(agent, sid, sid, start=0)
            else:
                t = adapter_read(agent, sid, source, start=0)
            d = extract_build(t, cfg.min_user_prompts, cfg.min_assistant_chars, cfg.digest_char_cap)
            key = sha256_bytes(d.text.encode("utf-8"))
            responses_by_digest[key] = sess["summary_response"]

        original_summarize = llm.summarize
        llm.summarize = _make_replay_summarizer(responses_by_digest)
        try:
            sessions = corpus.get("sessions", [])
            max_part = max((s.get("part", 1) for s in sessions), default=1)
            for part in range(1, max_part + 1):
                for sess in sessions:
                    if sess.get("part", 1) != part:
                        continue
                    sid = sess["session_id"]
                    agent = sess["agent"]
                    source = source_paths[(agent, sid, part)]
                    cwd = _replace_placeholders(sess.get("cwd", ""), work, vault)
                    man = Manifest(cfg.manifest_path)
                    try:
                        man.touch(agent, sid, now=time.time() - 7200, source=source,
                                  end=True, cwd=cwd)
                        # Each fixture part ships in its own file; reset the cursor so the
                        # adapter reads the whole file as one part.
                        man.mark(agent, sid, captured_until=0)
                    finally:
                        man.close()
                    worker.enqueue(cfg, agent, sid, event="end", source=source, cwd=cwd)
                worker.run(cfg, budget_s=120, summarizer=llm.summarize,
                           consolidator=_noop_consolidator, index=False)
        finally:
            llm.summarize = original_summarize
    finally:
        if original_export_opencode is not None:
            import agentmem.adapters as adapters_mod
            adapters_mod.export_opencode = original_export_opencode

    sys.path.pop(0)
    return cfg


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
               AGENTMEM_HOME=str(home))
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
    """Count manifest captures, not files, so identity collisions (T19) are counted honestly."""
    expected_notes = len(corpus.get("sessions", []))
    from agentmem.manifest import Manifest
    man = Manifest(vault / "_meta" / "manifest.sqlite")
    try:
        notes_written = man.db.execute("SELECT count(*) FROM captures").fetchone()[0]
    finally:
        man.close()
    passed = notes_written == expected_notes
    return {"passed": passed, "notes_written": notes_written,
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
        # The corpus transcript field already includes the leading "transcripts/" directory.
        transcripts_dir = Path(fixtures_dir)
        corpus["_transcripts_dir"] = str(transcripts_dir) if transcripts_dir.is_dir() else ""

        qmd_index = tmp / "qmd_index"
        vault, work, home, project_roots = make_temp_vault(tmp, corpus, qmd_index=qmd_index)

        try:
            cfg = run_pipeline_for_corpus(vault, work, home, corpus, project_roots, qmd_index=qmd_index)
        except SetupError as e:
            result = {"verdict": "setup_error", "exit_code": EXIT_SETUP,
                      "errors": [str(e)], "correctness_pass": False, "adoption_pass": False,
                      "limitations": []}
            write_outputs(output_dir, result, [], corpus)
            return EXIT_SETUP

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
