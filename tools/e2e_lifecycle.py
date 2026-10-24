"""Synthetic native-hook lifecycle acceptance. No provider call without --live."""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import asdict, dataclass, field
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def records(path):
    result = []
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                result.append(json.loads(line))
            except ValueError:
                continue
    return result


def append_record(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")


@dataclass
class Truth:
    storage: str
    reason: str
    next: str
    open: list[str] = field(default_factory=list)
    previous: list[dict] = field(default_factory=list)

    def advance(self, step):
        if step.get("storage") and step["storage"] != self.storage:
            self.previous.append(dict(storage=self.storage, reason=self.reason))
        for name in ("storage", "reason", "next", "open"):
            if name in step:
                setattr(self, name, step[name])


@dataclass
class Observation:
    agent: str
    session_id: str
    context: str
    reply: str
    events: list[dict]
    returncode: int | None
    killed: bool = False
    command: list[str] = field(default_factory=list)
    provider_usage: list[dict] = field(default_factory=list)


def reply_object(text):
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            result, _ = decoder.raw_decode(text[match.start():])
        except ValueError:
            continue
        if isinstance(result, dict) and "current_storage" in result:
            return result
    return {}


def affirmative_phrase(text, phrase):
    for match in re.finditer(re.escape(phrase), text, re.I):
        prefix = re.split(r"[.;\n]", text[:match.start()])[-1][-70:]
        if not re.search(r"\b(?:not|never|no longer|might|may|could|consider|proposed)\b", prefix, re.I):
            return True
    return False


def score_reply(text, truth):
    obj = reply_object(text)
    missing = []
    if obj.get("current_storage") != truth.storage:
        missing.append("current_storage")
    if not affirmative_phrase(str(obj.get("rationale", "")), truth.reason):
        missing.append("rationale")
    if not affirmative_phrase(str(obj.get("next_action", "")), truth.next):
        missing.append("next_action")
    for item in truth.open:
        if item.casefold() not in json.dumps(obj.get("open_work", [])).casefold():
            missing.append("open_work:" + item)
    stale = int(obj.get("current_storage") in {p["storage"] for p in truth.previous}
                and obj.get("current_storage") != truth.storage)
    return dict(status_correct=not missing, stale_as_current=stale, missing_items=missing,
                structured_reply=bool(obj))


def current_state(text):
    match = re.search(r"<!-- agentmem:state:begin -->(.*?)<!-- agentmem:state:end -->", text, re.S)
    state = match.group(1) if match else text
    return re.sub(r"(?ims)^#{1,4}\s+(?:superseded|history|pending|recent sessions)\b[^\n]*\n.*?(?=^#{1,4}\s|\Z)", "", state)


def score_status(text, truth):
    state = current_state(text)
    clauses = re.split(r"[;\n.!?]+", state)
    asserted = set()
    for clause in clauses:
        clause = re.sub(r"[\w./\\-]+\.json\b", "", clause, flags=re.I)
        if not re.search(r"\b(?:storage|backend|format|chosen|chose|use|uses|decision)\b", clause, re.I):
            continue
        if re.search(r"\b(?:superseded|previous|no longer|historical)\b", clause, re.I):
            continue
        for storage in ("json", "sqlite"):
            if re.search(r"\b" + storage + r"\b", clause, re.I):
                if (affirmative_phrase(clause, storage)
                        and not re.search(r"\b(?:replaced|instead of|rather than|away from)\s+" + storage + r"\b", clause, re.I)):
                    asserted.add(storage)
    missing = [] if truth.storage in asserted else ["current_storage"]
    if not affirmative_phrase(state, truth.reason):
        missing.append("rationale")
    if not affirmative_phrase(state, truth.next):
        missing.append("next_action")
    for item in truth.open:
        if item.casefold() not in state.casefold():
            missing.append("open_work:" + item)
    stale = len(asserted - {truth.storage})
    return dict(status_correct=not missing and not stale, stale_as_current=stale,
                missing_items=missing, asserted_storage=sorted(asserted),
                stale_count_scope="Distinct wrongly current storage choices; stale rationale/next obligations appear in missing_items.",
                method="Fixed storage-assertion clauses and literal scenario obligations; no model judge.")


def manifest_rows(vault):
    path = vault / "_meta/manifest.sqlite"
    result = dict(sessions=[], captures=[])
    if path.exists():
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            for table in result:
                if db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                    result[table] = [dict(r) for r in db.execute("SELECT * FROM " + table)]
    return result


def live_guard(session_ids, project, live_config):
    cfg = read_json(live_config)
    vault = Path(cfg["vault"])
    rows = 0
    db_path = vault / "_meta/manifest.sqlite"
    with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as db:
        for table in ("sessions", "captures"):
            columns = {r[1] for r in db.execute("PRAGMA table_info(" + table + ")")}
            conditions, args = [], []
            if "session_id" in columns and session_ids:
                conditions.append("session_id IN (" + ",".join("?" for _ in session_ids) + ")")
                args += list(session_ids)
            if "cwd" in columns:
                conditions.append("cwd=?")
                args.append(str(project))
            if conditions:
                rows += db.execute("SELECT count(*) FROM " + table + " WHERE " + " OR ".join(conditions), args).fetchone()[0]
    needles = [s.encode() for s in session_ids] + [str(project).encode(), project.as_posix().encode()]
    checked = matched = 0
    for path in vault.rglob("*.md"):
        checked += 1
        data = path.read_bytes()
        matched += any(needle in data for needle in needles)
    metadata = {}
    for name, root in (("vault", vault), ("runtime", live_config.parent)):
        metadata[name] = {}
        for path in root.rglob("*"):
            if path.is_file() and not path.name.endswith(("-shm", "-wal")):
                stat = path.stat()
                key = hashlib.sha256(path.relative_to(root).as_posix().encode()).hexdigest()
                metadata[name][key] = [stat.st_size, stat.st_mtime_ns]
    return dict(matching_rows=rows, matching_notes=matched, notes_checked=checked, metadata=metadata)


def app_entry(code_root, argv):
    """Observe model calls and detached workers without changing selected application decisions."""
    sys.path.insert(0, str(code_root))
    from agentmem import cli, llm, worker
    original = llm._run

    def observed(*args, **kwargs):
        path = Path(os.environ["E2E_ROOT"]) / "model-calls" / (str(os.getpid()) + ".jsonl")
        call_id = uuid.uuid4().hex
        append_record(path, dict(kind="start", id=call_id, at=time.time(), model=kwargs.get("model", args[2] if len(args) > 2 else None)))
        try:
            value = original(*args, **kwargs)
        except Exception as error:
            append_record(path, dict(kind="error", id=call_id, at=time.time(), error=type(error).__name__))
            raise
        append_record(path, dict(kind="complete", id=call_id, at=time.time()))
        return value

    llm._run = observed

    def spawn(extra_args=()):
        command = [sys.executable, "-B", "-I", str(Path(__file__).resolve()), "app", "--code-root", str(code_root), "worker", *extra_args]
        env = dict(os.environ, AGENTMEM_CAPTURE="1")
        options = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, close_fds=True)
        if os.name == "nt":
            options["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000
        else:
            options["start_new_session"] = True
        subprocess.Popen(command, **options)

    worker.spawn_detached = spawn
    return cli.main(argv)


def hook_proxy(agent, code_root):
    payload = json.loads(sys.stdin.read())
    event = payload.get("hook_event_name") or payload.get("hookEventName")
    root = Path(os.environ["E2E_STEP"])
    receipt = root / "hooks" / (str(time.time_ns()) + "-" + str(os.getpid()) + ".json")
    record = dict(payload=payload, event=event, at=time.time(), status="received")
    write_json(receipt, record)
    if os.environ.get("E2E_DELIVERY") == "no_end" and event == "SessionEnd":
        record.update(status="withheld", stdout="{}")
    else:
        env = dict(os.environ)
        env.pop("AGENTMEM_CAPTURE", None)
        command = [sys.executable, "-B", "-I", str(Path(__file__).resolve()), "app", "--code-root", str(code_root), "hook", agent]
        child = subprocess.run(command, input=json.dumps(payload).encode(), env=env, capture_output=True, timeout=12)
        record.update(status="forwarded", command=command, returncode=child.returncode,
                      stdout=child.stdout.decode("utf-8", "replace"), stderr=child.stderr.decode("utf-8", "replace"))
    record["finished_at"] = time.time()
    write_json(receipt, record)
    sys.stdout.write(record["stdout"])
    return 0


class Sandbox:
    def __init__(self, output, code_root, scenario):
        self.output, self.code_root = output.resolve(), code_root.resolve()
        self.vault = self.output / "sandbox/vault"
        self.runtime = self.output / "sandbox/runtime"
        self.project = self.output / "sandbox/project"
        self.scenario = scenario
        self.ids = set()
        self.truth = Truth(**scenario["initial"])

    def environment(self, step_dir, delivery="normal"):
        env = dict(os.environ, AGENTMEM_HOME=str(self.runtime), OBSIDIAN_VAULT_PATH=str(self.vault),
                   HERDR_ENV="0", AGENTMEM_CAPTURE="1", E2E_ROOT=str(self.output), E2E_STEP=str(step_dir),
                   E2E_DELIVERY=delivery, GIT_CEILING_DIRECTORIES=str(self.output / "sandbox"))
        for name in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
            env.pop(name, None)
        return env

    def git(self, *args):
        run = subprocess.run(["git", *args], cwd=self.project, capture_output=True, check=True)
        return run.stdout.decode("utf-8", "replace").strip()

    def prepare(self, summarizer):
        if self.output.exists() and any(self.output.iterdir()):
            raise ValueError("Output must be empty; never overwrite a previous live run")
        for path in (self.project, self.runtime, self.vault / "_meta"):
            path.mkdir(parents=True, exist_ok=True)
        self.git("init")
        self.git("config", "user.name", "Synthetic Builder")
        self.git("config", "user.email", "builder@example.invalid")
        (self.project / "AGENTS.md").write_text("Synthetic acceptance task. Do not delegate, browse, inspect external files or change configuration. Use only this repository.\n")
        (self.project / "CLAUDE.md").write_text("Synthetic acceptance task. Do not delegate or inspect external files. Follow the supplied task only.\n")
        write_json(self.project / "storage.json", dict(backend=self.truth.storage, reason=self.truth.reason))
        self.git("add", ".")
        self.git("commit", "-m", "Seed synthetic lifecycle")
        write_json(self.runtime / "config.json", dict(vault=str(self.vault), summarizer=summarizer,
                    auto_index=False, embeddings=False, settle_minutes=20))
        write_json(self.vault / "_meta/projects.json", dict(projects=[dict(name=self.scenario["project"], roots=[str(self.project)], status="active")]))
        name = self.scenario["project"]
        self.status_path = self.vault / "1-Projects" / name / (name + ".md")
        self.status_path.parent.mkdir(parents=True)
        self.status_path.write_text("# " + name + "\n<!-- agentmem:state:begin -->\n### Current state\n"
            + f"- Current storage format: {self.truth.storage}; rationale: {self.truth.reason}.\n"
            + "- Next action: " + self.truth.next + ".\n"
            + "### Active decisions\n- Use " + self.truth.storage + " storage for " + self.truth.reason + ".\n"
            + "### Superseded decisions\n- none\n### Open questions and next steps\n- " + self.truth.next
            + "\n<!-- agentmem:state:end -->\n<!-- agentmem:pending:begin -->\n<!-- agentmem:pending:end -->\n"
            + "<!-- agentmem:sessions:begin -->\n<!-- agentmem:sessions:end -->\n", encoding="utf-8")
        self.setup_hooks()
        with (self.project / ".git/info/exclude").open("a") as stream:
            stream.write("\n.codex/\n")
        command = [sys.executable, "-B", "-I", "-c", "import sys,json;sys.path.insert(0,sys.argv[1]);from agentmem.config import load,RUNTIME;c=load();print(json.dumps({'runtime':str(RUNTIME),'paths':{k:str(getattr(c,k)) for k in ('vault','meta','manifest_path','queue_dir','locks_dir','log_path','projects_map')}}))", str(self.code_root)]
        result = subprocess.run(command, capture_output=True, env=self.environment(self.output), check=True)
        loaded = json.loads(result.stdout)
        assert all(Path(p).resolve().is_relative_to((self.output / "sandbox").resolve()) for p in [loaded["runtime"], *loaded["paths"].values()])
        write_json(self.output / "dry-config.json", dict(command=command, loaded=loaded))
        write_json(self.output / "source.json", dict(code_root=str(self.code_root), hashes={str(p.relative_to(self.code_root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((self.code_root / "agentmem").rglob("*.py"))}, scenario=scenario_copy(self.scenario)))

    def setup_hooks(self):
        for agent, filename in (("claude", "claude-settings.json"), ("codex", "hooks.json")):
            command = hook_command([sys.executable, "-B", "-I", str(Path(__file__).resolve()), "hook", "--code-root", str(self.code_root), "--agent", agent])
            hook = dict(type="command", command=command, timeout=15)
            if agent == "codex":
                hook["commandWindows"] = "& " + " ".join("'" + x.replace("'", "''") + "'" for x in [sys.executable, "-B", "-I", str(Path(__file__).resolve()), "hook", "--code-root", str(self.code_root), "--agent", agent])
            config = dict(hooks={event:[dict(hooks=[hook])] for event in ("SessionStart", "Stop", "SessionEnd", "PreCompact")})
            target = self.output / filename if agent == "claude" else self.project / ".codex" / filename
            write_json(target, config)
        (self.project / ".codex/config.toml").write_text("[features]\nhooks = true\n")

    def snapshot(self):
        notes = {p.relative_to(self.vault).as_posix():p.read_text(encoding="utf-8") for p in self.vault.rglob("*.md")}
        return dict(notes=notes, status=self.status_path.read_text(encoding="utf-8"), manifest=manifest_rows(self.vault),
                    repo=dict(head=self.git("rev-parse", "HEAD"), branch=self.git("branch", "--show-current"), dirty=self.git("status", "--porcelain"),
                              storage=read_json(self.project / "storage.json"),
                              files={p.name:p.read_text(encoding="utf-8") for p in self.project.glob("*.txt")}))

    def settle(self, step_dir, timeout=120):
        deadline = time.monotonic() + timeout
        previous = None
        stable = 0
        while time.monotonic() < deadline:
            rows = manifest_rows(self.vault)
            busy = (any((self.vault / "_meta/queue").glob("*.json"))
                    or any(r.get("status") in ("pending", "error") and r.get("end_seen") for r in rows["sessions"]))
            calls = [r for p in (self.output / "model-calls").glob("*.jsonl") for r in records(p)]
            pending_calls = {r["id"] for r in calls if r["kind"] == "start"} - {r["id"] for r in calls if r["kind"] != "start"}
            signature = json.dumps(rows, sort_keys=True)
            stable = stable + 1 if signature == previous else 0
            if not busy and not pending_calls and stable >= 3:
                return dict(completed=True, timeout=False)
            previous = signature
            time.sleep(0.4)
        return dict(completed=False, timeout=True)

    def commit_step(self, step):
        self.git("add", "--", "storage.json", "decision.txt", "open-work.txt")
        self.git("commit", "-m", "Synthetic storage " + step["storage"])

    def drain(self, directory):
        command = [sys.executable, "-B", "-I", str(Path(__file__).resolve()), "app", "--code-root", str(self.code_root), "worker"]
        run = subprocess.run(command, env=self.environment(directory), cwd=self.project, capture_output=True, timeout=60)
        return dict(command=command, returncode=run.returncode, stdout=run.stdout.decode(), stderr=run.stderr.decode(), time_aged=False)

    def pause(self):
        (self.vault / "_meta/PAUSE").write_text("Acceptance finished; preserve pending cases without future automatic model calls.\n", encoding="utf-8")


def scenario_copy(value):
    return json.loads(json.dumps(value))


def hook_command(argv):
    return " ".join('"' + str(value).replace("\\", "/").replace('"', '\\"') + '"' for value in argv)


def prompt_for(step, truth):
    report = ('Return a JSON object with current_storage (json, sqlite, or unknown), rationale, next_action, '
              'open_work (list), prior_decisions (list). Report current project state, not the agentmem runtime environment.')
    if step["kind"] == "read":
        return "State the project status supplied at startup and its next action. Do not inspect files or run tools. " + report
    if step["kind"] == "kill":
        return ("Synthetic interruption probe. Write exactly three files interrupted-status.txt, interrupted-decision.txt, interrupted-next.txt "
                "inside this repository, each stating that interrupted test-plan work remains unfinished. "
                "After writing all three, begin a detailed 2000-word test-plan analysis in your response. Do not delegate or access other folders.")
    target = dict(storage=step.get("storage", truth.storage), reason=step.get("reason", truth.reason),
                  next=step.get("next", truth.next), open=step.get("open", truth.open))
    commit = "The harness will make a real commit of these three files after your turn; do not run git yourself." if step.get("commit") else "Leave all three files uncommitted; the coverage work is not finished."
    return ("Synthetic project task. Set storage.json to JSON with backend=" + json.dumps(target["storage"])
        + " and reason=" + json.dumps(target["reason"]) + ". Write decision.txt explaining this choice and its rationale. "
        + "Write open-work.txt with next action " + json.dumps(target["next"]) + " and open obligations " + json.dumps(target["open"])
        + ". Change exactly these three files, using only local file tools and git. " + commit + " Do not delegate or browse. " + report)


class NativeAgent:
    def __init__(self, claude="claude", codex="codex", timeout=240):
        self.executables, self.timeout = dict(claude=claude, codex=codex), timeout

    def run(self, sandbox, step, agent, directory):
        directory.mkdir(parents=True)
        sid = str(uuid.uuid4()) if agent == "claude" else ""
        prompt = prompt_for(step, sandbox.truth)
        if agent == "claude":
            command = [self.executables[agent], "-p", "--model", "haiku", "--output-format", "stream-json", "--verbose", "--include-hook-events",
                       "--setting-sources", "", "--settings", str(sandbox.output / "claude-settings.json"), "--session-id", sid,
                       "--tools", "Read,Write" if step["kind"] in ("work", "kill") else "", "--permission-mode", "acceptEdits", prompt]
        else:
            trust = "projects." + json.dumps(str(sandbox.project)) + '.trust_level="trusted"'
            command = [self.executables[agent], "exec", "-m", "gpt-6-luna", "-c", 'model_reasoning_effort="low"', "-c", trust,
                       "--dangerously-bypass-hook-trust", "--json", "-s", "workspace-write", "-C", str(sandbox.project), prompt]
        write_json(directory / "request.json", dict(command=command, cwd=str(sandbox.project), delivery=step.get("delivery", "normal"),
                   environment={k:v for k,v in sandbox.environment(directory).items() if k.startswith(("AGENTMEM", "OBSIDIAN", "E2E_", "GIT_CEILING"))}))
        killed = False
        with (directory / "stdout.jsonl").open("wb") as out, (directory / "stderr.txt").open("wb") as err:
            process = subprocess.Popen(command, cwd=sandbox.project, env=sandbox.environment(directory, step.get("delivery", "normal")),
                                       stdin=subprocess.DEVNULL, stdout=out, stderr=err)
            deadline = time.monotonic() + self.timeout
            while process.poll() is None and time.monotonic() < deadline:
                events = [read_json(p) for p in sorted((directory / "hooks").glob("*.json"))]
                kill_ready = all((sandbox.project / n).exists() for n in ("interrupted-status.txt", "interrupted-decision.txt", "interrupted-next.txt"))
                if step["kind"] == "kill" and kill_ready and any(e["event"] == "SessionStart" and e.get("status") == "forwarded" for e in events):
                    if os.name == "nt":
                        stopped = subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
                        try:
                            process.wait(timeout=15)
                        except subprocess.TimeoutExpired:
                            stopped.check_returncode()
                            raise
                    else:
                        process.kill()
                    killed = True
                    break
                time.sleep(0.03)
            if process.poll() is None and not killed:
                process.kill()
            rc = process.wait(timeout=15)
        events = [read_json(p) for p in sorted((directory / "hooks").glob("*.json"))]
        for event in events:
            if event["payload"].get("session_id"):
                sid = event["payload"]["session_id"]
        if not sid:
            sid = next((r.get("thread_id", "") for r in records(directory / "stdout.jsonl") if r.get("type") == "thread.started"), "")
        contexts, replies, usage = [], [], []
        for event in events:
            if event["event"] == "SessionStart" and event.get("stdout"):
                context = json.loads(event["stdout"]).get("hookSpecificOutput", {}).get("additionalContext", "")
                contexts.append(context)
        for row in records(directory / "stdout.jsonl"):
            if row.get("type") == "assistant":
                replies += [c["text"] for c in row.get("message", {}).get("content", []) if c.get("type") == "text"]
            if row.get("item", {}).get("type") == "agent_message":
                replies.append(row["item"].get("text", ""))
            if row.get("usage") or row.get("num_turns"):
                usage.append({k:row[k] for k in ("type", "usage", "num_turns", "modelUsage") if k in row})
        return Observation(agent, sid, "\n".join(contexts), "\n".join(replies), events, rc, killed, command, usage)


def evaluate_step(step, truth, observation, before, after):
    notes_before, notes_after = before["notes"], after["notes"]
    changed = [p for p in notes_after if notes_after[p] != notes_before.get(p)]
    created = [p for p in notes_after if p not in notes_before]
    session_id = observation.session_id if observation else ""
    captures = [r for r in after["manifest"]["captures"] if r.get("session_id") == session_id] if session_id else []
    retained = all(any(p["storage"].casefold() in text.casefold() and p["reason"].casefold() in text.casefold()
                       for text in notes_after.values()) for p in truth.previous)
    result = dict(status=score_status(after["status"], truth), notes_created=created, notes_changed=changed,
                  notes_per_logical_session=len({r["note_path"] for r in captures}), decision_retained=retained,
                  decision_retention_scope="Matching storage/rationale in the same vault note. Committed Git history is retained separately.",
                  ground_truth=asdict(truth), repo=after["repo"])
    if observation:
        result["reply"] = score_reply(observation.reply, truth)
        result["injected"] = bool(observation.context)
        result["startup_chars"] = len(observation.context)
        result["startup_utf8_bytes"] = len(observation.context.encode())
        result["startup_budget_unit"] = "characters; no actual-token claim"
        result["native_end_received"] = any(e["event"] == "SessionEnd" for e in observation.events)
        result["end_forwarded"] = any(e["event"] == "SessionEnd" and e.get("status") == "forwarded" for e in observation.events)
        result["killed_before_first_stop"] = observation.killed and not any(e["event"] == "Stop" for e in observation.events)
        result["session_registered"] = any(r.get("session_id") == session_id for r in after["manifest"]["sessions"])
        result["reply_required"] = step["kind"] in ("read", "work")
    return result


def work_completed(step, truth, before, after):
    target = Truth(**scenario_copy(asdict(truth)))
    target.advance(step)
    files = after["repo"].get("files", {})
    return (after["repo"]["storage"] == dict(backend=target.storage, reason=target.reason)
            and target.reason.casefold() in files.get("decision.txt", "").casefold()
            and all(t.casefold() in files.get("open-work.txt", "").casefold() for t in [target.next, *target.open])
            and (before["repo"]["storage"] != after["repo"]["storage"] or before["repo"].get("files", {}) != files))


def run_scenario(sandbox, backend, agents, guard=None):
    scores, observations = [], {}
    for step in sandbox.scenario["steps"]:
        directory = sandbox.output / step["id"]
        before = sandbox.snapshot()
        observation = None
        performed = None
        if step["kind"] in ("read", "work", "kill"):
            agent = agents[step["agent"] % len(agents)]
            observation = backend.run(sandbox, step, agent, directory)
            sandbox.ids.add(observation.session_id)
            observations[step["id"]] = observation
            write_json(directory / "observation.json", asdict(observation))
            if guard:
                live = guard(sandbox.ids, sandbox.project)
                write_json(directory / "live-after-agent.json", live)
                if live["matching_rows"] or live["matching_notes"]:
                    raise RuntimeError("Sandbox identity found in live state; stop")
            if not any(e["event"] == "SessionStart" and e.get("status") == "forwarded" for e in observation.events):
                raise RuntimeError("Native SessionStart proxy was not called; setup is unmeasured, never a quality zero")
            if step["kind"] != "kill" and observation.returncode:
                raise RuntimeError("Agent failed; inspect persisted output")
            if step["kind"] == "work":
                performed = work_completed(step, sandbox.truth, before, sandbox.snapshot())
                if performed:
                    sandbox.truth.advance(step)
                if step.get("commit") and performed:
                    sandbox.commit_step(step)
            if step["kind"] == "kill" and observation.killed:
                sandbox.truth.advance(step)
            wait = sandbox.settle(directory)
        else:
            directory.mkdir(parents=True)
            observation = observations[step["session"]]
            wait = dict(completed=True, timeout=False)
            if step["kind"] == "drain":
                write_json(directory / "drain.json", sandbox.drain(directory))
                wait = sandbox.settle(directory, timeout=10)
        after = sandbox.snapshot()
        write_json(directory / "before.json", before)
        write_json(directory / "after.json", after)
        (directory / "status.diff").write_text("".join(difflib.unified_diff(before["status"].splitlines(True), after["status"].splitlines(True), fromfile="before", tofile="after")))
        score = evaluate_step(step, sandbox.truth, observation, before, after)
        score.update(step=step["id"], kind=step["kind"], wait=wait)
        if step.get("commit"):
            score["commit_created"] = before["repo"]["head"] != after["repo"]["head"]
            score["commit_actor"] = "harness after real agent file work"
        if step["kind"] == "work":
            score["work_completed"] = performed
            score["capture_probe_applicable"] = performed
            score["repo_storage_matches"] = after["repo"]["storage"] == dict(backend=sandbox.truth.storage, reason=sandbox.truth.reason)
            score["file_work_present"] = all((sandbox.project / n).exists() for n in ("storage.json", "decision.txt", "open-work.txt"))
        if step["kind"] == "drain":
            score["unaged_no_end_recovered"] = score["notes_per_logical_session"] > 0
        if guard:
            live = guard(sandbox.ids, sandbox.project)
            write_json(directory / "live-guard.json", live)
            if live["matching_rows"] or live["matching_notes"]:
                raise RuntimeError("Sandbox identity found in live state; stop")
        calls = [r for p in (sandbox.output / "model-calls").glob("*.jsonl") for r in records(p)]
        score["model_calls"] = dict(primary_invocations=len(observations), capture_llm_starts=sum(r["kind"] == "start" for r in calls),
                                    capture_llm_errors=sum(r["kind"] == "error" for r in calls),
                                    provider_usage=observation.provider_usage if observation else [], exact_primary_api_requests="unmeasured; CLI turn/token counters retained")
        write_json(directory / "score.json", score)
        scores.append(score)
        write_json(sandbox.output / "result.json", dict(scores=scores, complete=False))
        print(step["id"], "status", score["status"]["status_correct"], "stale", score["status"]["stale_as_current"], flush=True)
        if wait["timeout"]:
            return dict(scores=scores, complete=False, stop_reason="Worker did not settle; later steps unmeasured")
    result = dict(scores=scores, complete=True, scenario=sandbox.scenario,
                  primary_invocations=len(observations), capture_llm_starts=scores[-1]["model_calls"]["capture_llm_starts"],
                  limitation="Native hooks are process-local instrumented forwards to selected code. SessionEnd is deliberately withheld only for no_end; clocks/cursors are never aged.")
    write_json(sandbox.output / "result.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("run", "hook", "app"))
    parser.add_argument("--code-root", type=Path, required=True)
    parser.add_argument("--agent", choices=("claude", "codex"))
    parser.add_argument("--agents", default="claude,codex")
    parser.add_argument("--scenario", type=Path, default=Path(__file__).resolve().parents[1] / "tests/fixtures/e2e/lifecycle.json")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--claude-bin", default=shutil.which("claude") or "claude")
    parser.add_argument("--codex-bin", default=shutil.which("codex") or "codex")
    args, rest = parser.parse_known_args(argv)
    if args.mode in ("app", "hook"):
        root = os.environ.get("E2E_ROOT")
        if not root:
            parser.error("Internal modes require a runner-owned E2E_ROOT sandbox")
        boundary = Path(root).resolve() / "sandbox"
        for key in ("AGENTMEM_HOME", "OBSIDIAN_VAULT_PATH"):
            value = os.environ.get(key)
            if not value or not Path(value).resolve().is_relative_to(boundary):
                parser.error(key + " must remain inside E2E_ROOT/sandbox")
        if args.mode == "hook" and not Path(os.environ.get("E2E_STEP", "")).resolve().is_relative_to(Path(root).resolve()):
            parser.error("Hook receipts must remain inside E2E_ROOT")
    if args.mode == "app":
        return app_entry(args.code_root, rest)
    if args.mode == "hook":
        return hook_proxy(args.agent, args.code_root)
    if rest or not args.live or not args.output:
        parser.error("run requires --live and --output; no model calls by default")
    agents = args.agents.split(",")
    if not agents or any(a not in ("claude", "codex") for a in agents):
        parser.error("Only the explicitly budgeted Claude/Codex agents are supported")
    if not (args.code_root / "run.py").is_file():
        parser.error("code root must contain run.py")
    live_config = Path.home() / ".agents/memory/config.json"
    summarizer = read_json(live_config).get("summarizer", "claude-haiku")
    sandbox = Sandbox(args.output, args.code_root, read_json(args.scenario))
    sandbox.prepare(summarizer)
    guard = lambda ids, project: live_guard(ids, project, live_config)
    try:
        write_json(sandbox.output / "live-before.json", guard(set(), sandbox.project))
        result = run_scenario(sandbox, NativeAgent(args.claude_bin, args.codex_bin), agents, guard)
    finally:
        sandbox.pause()
    return 0 if result["complete"] else 3


if __name__ == "__main__":
    sys.exit(main())
