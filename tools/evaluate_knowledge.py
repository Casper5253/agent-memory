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
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.knowledge_grader import grade_query, lifecycle_check, formulate_query, QUERY_STOPWORDS

EXIT_PASS, EXIT_FAIL, EXIT_SETUP, EXIT_INCONCLUSIVE = 0, 1, 2, 3


def repo_tmp():
    """Return the repository-local scratch directory; never use /tmp or %TEMP%."""
    tmp = ROOT / ".audit" / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    return tmp


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
    for name in set(required) | set(manifest.get("files", {})):
        target = (fixtures_dir / name).resolve()
        if not target.is_relative_to(fixtures_dir.resolve()) or not target.is_file():
            return None, None, None, None, None, ["invalid or missing fixture: " + name]
        hashes[name] = sha256_file(target)
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
    from agentmem.adapters import read as adapter_read, read_opencode
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
            start = 0
            previous = source_paths.get((agent, sid, part - 1))
            if previous:
                start = len(json.loads(Path(previous).read_text(encoding="utf-8"))["messages"]) if agent == "opencode" else adapter_read(agent, sid, previous).end
            if agent == "opencode":
                t = read_opencode(sid, start, json.loads(Path(source).read_text(encoding="utf-8")))
            else:
                t = adapter_read(agent, sid, source, start=start)
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
                    if agent == "opencode":
                        opencode_sources[sid] = source
                    cwd = _replace_placeholders(sess.get("cwd", ""), work, vault)
                    man = Manifest(cfg.manifest_path)
                    try:
                        man.touch(agent, sid, now=time.time() - 7200, source=source,
                                  end=True, cwd=cwd)
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


def detect_qmd(env=None, cwd=None):
    shim = shutil.which("qmd")
    if not shim:
        return "unavailable", ""
    node = shutil.which("node")
    js = Path(shim).parent / "node_modules" / "@tobilu" / "qmd" / "bin" / "qmd" if node else None
    try:
        if node and js and js.exists():
            cmd = [node, str(js)]
        else:
            cmd = [shim]
        r = subprocess.run(cmd + ["--version"], capture_output=True, timeout=10,
                           env=env or dict(os.environ, NO_COLOR="1"), cwd=cwd)
        version = r.stdout.decode("utf-8", "replace").strip()[:120]
        if r.returncode != 0 or not version:
            return "unavailable", version
        return "ok", version
    except Exception as e:
        return "unavailable", str(e)[:120]


def isolated_environment(vault, home, cache_dir=None):
    base = Path(home).resolve().parent
    cache = Path(cache_dir).resolve() if cache_dir else base / "cache"
    if not Path(vault).resolve().is_relative_to(base) or not cache.is_relative_to(base):
        raise SetupError("vault, runtime and cache must share the isolated root")
    directories = {
        "HOME": base / "os-home", "USERPROFILE": base / "os-home",
        "APPDATA": base / "os-home/AppData/Roaming", "LOCALAPPDATA": base / "os-home/AppData/Local",
        "XDG_CACHE_HOME": cache, "XDG_CONFIG_HOME": base / "config", "XDG_DATA_HOME": base / "data",
        "QMD_CONFIG_DIR": base / "config/qmd", "TMP": base / "tmp", "TEMP": base / "tmp", "TMPDIR": base / "tmp",
        "NODE_COMPILE_CACHE": cache / "node", "HF_HOME": cache / "huggingface",
    }
    env = dict(os.environ)
    for key, path in directories.items():
        path.mkdir(parents=True, exist_ok=True)
        env[key] = str(path)
    for key in ("AGENTMEM_CAPTURE", "NODE_OPTIONS", "QMD_WRAPPER_CAPTURE"):
        env.pop(key, None)
    env.update(OBSIDIAN_VAULT_PATH=str(vault), AGENTMEM_HOME=str(home),
               INDEX_PATH=str(cache / "qmd/index.sqlite"), NO_COLOR="1")
    (cache / "qmd").mkdir(exist_ok=True)
    return env


def run_mem_command(vault, home, args, timeout=30, cwd=None, cache_dir=None):
    env = isolated_environment(vault, home, cache_dir)
    command = [sys.executable, "-B", "-X", "utf8", "-I", str(ROOT / "run.py")] + args
    if args and args[0] == "search":
        observer = '''import json,runpy,sys
sys.path.insert(0,sys.argv[1])
from agentmem import search
original=search._qmd
def observed(*args,**kwargs):
 try:
  result=original(*args,**kwargs)
 except Exception as error:
  sys.stderr.write("EVAL_QMD:"+json.dumps({"error":type(error).__name__})+"\\n")
  raise
 sys.stderr.write("EVAL_QMD:"+json.dumps({"returncode":result.returncode})+"\\n")
 return result
search._qmd=observed
runner=sys.argv[1]+"/run.py"
sys.argv=[runner]+sys.argv[2:]
runpy.run_path(runner,run_name="__main__")'''
        command = [sys.executable, "-B", "-X", "utf8", "-I", "-c", observer, str(ROOT)] + args
    try:
        r = subprocess.run(
            command,
            capture_output=True, timeout=timeout, env=env, cwd=str(cwd or Path(home).resolve().parent)
        )
        return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as e:
        return -1, "", str(e)


def package_baseline_context(vault, home, query, qmd_available=True, cwd=None, cache_dir=None, max_bytes=None):
    budget = query.get("budget", 2400)
    limit = max_bytes if max_bytes is not None else budget
    terms = query["query"]
    rc, stdout, stderr = run_mem_command(vault, home, ["search", terms, "--json", "-n", "8"],
                                         cwd=cwd, cache_dir=cache_dir)
    hits = []
    error = ""
    if rc != 0:
        error = f"search_exit:{rc}:" + stderr[:200]
    else:
        try:
            hits = json.loads(stdout)
            if not isinstance(hits, list):
                raise ValueError("search response must be a list")
        except ValueError as exc:
            error = "invalid_search_json:" + str(exc)
            hits = []
    traces = [json.loads(line.split("EVAL_QMD:", 1)[1]) for line in stderr.splitlines() if line.startswith("EVAL_QMD:")]
    fallback = bool(hits and any("fallback" in hit for hit in hits)) or bool(traces and traces[-1].get("returncode") != 0)
    backend = "error" if error else ("fallback" if fallback else ("qmd" if traces else "unknown"))
    snippets = []
    total = 0
    for hit in hits:
        if total >= limit:
            break
        candidate = Path(hit.get("path", ""))
        path = candidate.resolve() if candidate.is_absolute() else (Path(vault) / candidate).resolve()
        if not path.is_relative_to(Path(vault).resolve()):
            raise SetupError("search returned a path outside the isolated vault")
        note_rc, note_out, _ = run_mem_command(vault, home, ["note", str(path)], cwd=cwd, cache_dir=cache_dir)
        if note_rc != 0:
            continue
        normalized = note_out.replace("\r\n", "\n")
        if not normalized.startswith("# source: ") or "\n\n" not in normalized:
            raise SetupError("unexpected mem note provenance envelope")
        content = normalized.split("\n\n", 1)[1].rstrip("\n")
        snippet = {"path": path.relative_to(Path(vault).resolve()).as_posix(), "label": hit.get("label", ""),
                   "score": hit.get("score"), "content": content[:800]}
        blob = json.dumps(snippet, ensure_ascii=False)
        if total + len(blob.encode("utf-8")) > limit:
            break
        snippets.append(snippet)
        total += len(blob.encode("utf-8"))
    return {"snippets": snippets, "hit_count": len(hits), "packed_bytes": total,
            "qmd_used": backend == "qmd", "fallback": fallback, "backend": backend,
            "search_error": error, "budget": limit, "budget_unit": "tokens_est_3b"}


def write_outputs(output_dir, result, per_query_outcomes, corpus):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    result["per_query_count"] = len(per_query_outcomes)
    result["timestamp"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (output_dir / "result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    with open(output_dir / "per_query.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["query_id", "pass", "snippet_count", "packed_bytes", "qmd_used", "failures"])
        for o in per_query_outcomes:
            w.writerow([o["query_id"], o["pass"], o.get("snippet_count", 0),
                        o.get("packed_bytes", 0), o.get("qmd_used", ""),
                        "; ".join(o.get("failures", []))])

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
    lines.append(f"Primary quality: {result.get('counters', {})}")
    lines.append(f"Raw-question quality: {result.get('raw_counters', {})}")
    lines.append("")
    for o in per_query_outcomes:
        if not o["pass"]:
            label = "UNMEASURED" if o.get("unmeasured") else "FAIL"
            lines.append(f"- {label} {o['query_id']}: {'; '.join(o.get('failures', []))}")
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
    loaded = load_fixtures(fixtures_dir)
    if loaded[5] is not None:
        result = {"verdict": "setup_error", "errors": loaded[5], "correctness_pass": False, "adoption_pass": False}
        write_outputs(output_dir, result, [], {})
        return EXIT_SETUP
    manifest, corpus, queries, expected, hashes, _ = loaded
    mismatch = check_fixture_hashes(manifest, hashes)
    if mismatch:
        write_outputs(output_dir, {"verdict": "setup_error", "errors": mismatch}, [], corpus)
        return EXIT_SETUP
    with tempfile.TemporaryDirectory(prefix="agentmem-eval-", dir=str(repo_tmp())) as directory:
        tmp = Path(directory)
        corpus["_transcripts_dir"] = str(Path(fixtures_dir).resolve())
        cache = tmp / "cache"
        vault, work, home, roots = make_temp_vault(tmp, corpus, qmd_index="index")
        try:
            run_pipeline_for_corpus(vault, work, home, corpus, roots, qmd_index="index")
            rc, stdout, stderr = run_mem_command(vault, home, ["index", "--no-embed"], timeout=120, cwd=tmp, cache_dir=cache)
            index_result = {"returncode": rc, "stdout": stdout.strip(), "stderr": stderr[:500]}
            qmd_status, qmd_version = detect_qmd(env=isolated_environment(vault, home, cache), cwd=tmp)
            database = cache / "qmd/index.sqlite"
            index_result["database_exists"] = database.is_file()
            if database.is_file():
                with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
                    index_result["active_documents"] = connection.execute("SELECT COUNT(*) FROM documents WHERE active=1").fetchone()[0]
            outcomes = []
            for query in queries:
                primary = dict(query, query=formulate_query(query["query"]))
                contexts = {}
                grades = {}
                for name, request in (("primary", primary), ("raw", query)):
                    package = package_baseline_context(vault, home, request, qmd_available=qmd_status == "ok", cwd=tmp, cache_dir=cache)
                    grade = grade_query(query, expected[query["query_id"]], package, corpus=corpus, vault=vault)
                    grade.update(backend=package["backend"], qmd_used=package["qmd_used"], fallback=package["fallback"],
                                 hit_count=package["hit_count"], search_terms=request["query"], search_error=package["search_error"])
                    contexts[name] = package
                    grades[name] = grade
                row = dict(grades["primary"], case=query["case"], partition=query["partition"], raw=grades["raw"])
                row["quality_measured"] = not bool(row["unmeasured"])
                outcomes.append(row)
            diagnostics = []
            for terms in ("historical", "storage"):
                package = package_baseline_context(vault, home, {"query": terms, "budget": 2400}, cwd=tmp, cache_dir=cache)
                diagnostics.append({"terms": terms, "backend": package["backend"], "hit_count": package["hit_count"],
                                    "snippet_count": len(package["snippets"]), "search_error": package["search_error"]})
            lifecycle = lifecycle_check(vault, corpus)
        except (SetupError, ValueError, KeyError, sqlite3.Error) as error:
            write_outputs(output_dir, {"verdict": "setup_error", "errors": [str(error)]}, [], corpus)
            return EXIT_SETUP

        def summarize(rows):
            measured = [r for r in rows if not r.get("unmeasured")]
            return {"queries": len(rows), "quality_measured": len(measured), "quality_passed": sum(r["pass"] for r in measured),
                    "quality_failed": sum(not r["pass"] for r in measured), "unmeasured": len(rows) - len(measured),
                    "qmd_queries": sum(r["backend"] == "qmd" for r in rows),
                    "qmd_hit_queries": sum(r["backend"] == "qmd" and r["hit_count"] > 0 for r in rows),
                    "fallback_queries": sum(r["backend"] == "fallback" for r in rows),
                    "fallback_hit_queries": sum(r["backend"] == "fallback" and r["hit_count"] > 0 for r in rows),
                    "search_errors": sum(bool(r["search_error"]) for r in rows)}
        counters = summarize(outcomes)
        raw_counters = summarize([r["raw"] for r in outcomes])
        per_case = {case: {"primary": summarize([r for r in outcomes if r["case"] == case]),
                          "raw": summarize([r["raw"] for r in outcomes if r["case"] == case])} for case in sorted({q["case"] for q in queries})}
        correctness = bool(outcomes) and all(row["pass"] for row in outcomes) and lifecycle["passed"]
        exit_code = EXIT_FAIL if counters["quality_failed"] or not lifecycle["passed"] else EXIT_INCONCLUSIVE
        result = {"commit": get_commit(), "evaluator_sha256": sha256_file(Path(__file__)),
                  "python_version": sys.version.split()[0], "sqlite_version": sqlite3.sqlite_version,
                  "temp_dir": str(tmp), "qmd_index_path": str(database), "qmd_config_dir": str(tmp / "config/qmd"),
                  "qmd_status": qmd_status, "qmd_version": qmd_version, "index": index_result,
                  "tokenizer": "byte-fallback", "fixture_hashes": hashes,
                  "query_formulation": {"version": 1, "rule": "lowercase, drop fixed stopwords, first 4 remaining words", "stopwords": sorted(QUERY_STOPWORDS)},
                  "counters": counters, "raw_counters": raw_counters, "per_case": per_case, "diagnostics": diagnostics,
                  "costs": {"model_usd": None, "model_calls": 0, "reason": "frozen replay summaries; model cost unmeasured"},
                  "lifecycle": lifecycle, "lifecycle_failure": not lifecycle["passed"], "per_query": outcomes,
                  "correctness_pass": correctness, "adoption_pass": False, "exit_code": exit_code,
                  "verdict": "fail" if exit_code == EXIT_FAIL else "inconclusive",
                  "limitations": ["B0 only; no candidate comparison or adoption decision.",
                                  "Fault execution is unmeasured and excluded from quality counts.",
                                  "Frozen replay summaries and no-op consolidation do not establish model extraction or normal current-state quality.",
                                  "One correctness run; no latency, cost, semantic or exact-token performance claim.",
                                  "The 800-character snippet cap and conservative byte packaging are frozen for both query forms."]}
        write_outputs(output_dir, result, outcomes, corpus)
        return exit_code


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
