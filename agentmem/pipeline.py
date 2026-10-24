"""Capture pipeline: settled session -> digest -> gate -> summary -> immutable note -> reconciled project note.

Order of effects is chosen so any crash is safe to retry:
  1. a versioned receipt freezes the source range, summary, identity and note hash
  2. note is created once and its persisted bytes are checked against the receipt
  3. project note is reconciled under the project lock
  4. manifest records the capture (one transaction) -> cursor advances
A retry after a crash between 1 and 3 finds the note, re-reconciles (idempotent by session link), records.
"""
import hashlib
import json
import os
import re
import tempfile
import time
import traceback
from datetime import datetime
from pathlib import Path

from . import adapters, extract, llm, notes, projects
from .config import Config
from .fsutil import atomic_write, create_once, file_lock
from .knowledge import sources as ksources
from .logutil import log
from .manifest import Manifest
from .redact import redact, redact_session_note



def _parse_time(value: str) -> datetime:
    if value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
        except ValueError:
            pass
    return datetime.now().astimezone()


def _source_for(row) -> str:
    if row["agent"] == "codex" and not (row["source"] and Path(row["source"]).exists()):
        return adapters.find_codex_transcript(row["session_id"]) or ""
    if row["agent"] == "opencode":
        return row["session_id"]
    return row["source"] or ""


def _excluded(cfg: Config, cwd: str) -> bool:
    if not cwd:
        return False
    c = os.path.normcase(os.path.normpath(cwd))
    roots = [cfg.meta, *map(Path, cfg.excluded_roots)]
    return any(c == os.path.normcase(str(r)) or c.startswith(os.path.normcase(str(r)) + os.sep) for r in roots)


def _identity_key(agent, sid, part):
    return hashlib.sha256(json.dumps([agent, sid, part], separators=(",", ":")).encode("utf-8")).hexdigest()


def _hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _source_range_hash(transcript):
    if transcript.agent == "opencode":
        data = adapters.export_opencode(transcript.session_id)
        messages = data.get("messages") or []
        selected = messages[transcript.start:transcript.end]
        raw = json.dumps(selected, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    else:
        path = Path(transcript.source)
        with path.open("rb") as source:
            source.seek(transcript.start)
            raw = source.read(transcript.end - transcript.start)
    return hashlib.sha256(raw).hexdigest()


def _receipt_path(cfg, agent, sid, part):
    return cfg.meta / "capture-receipts" / (_identity_key(agent, sid, part) + ".json")


def _receipt_hash(receipt):
    return _hash(json.dumps({k: v for k, v in receipt.items() if k != "receipt_sha"}, sort_keys=True, ensure_ascii=False))


def _load_receipt(cfg, agent, sid, part, start):
    path = _receipt_path(cfg, agent, sid, part)
    if not path.exists():
        return None
    try:
        r = json.loads(path.read_text(encoding="utf-8"))
        if (r["version"] != 1 or (r["agent"], r["session_id"], r["part"], r["start"]) != (agent, sid, part, start)
                or r["receipt_sha"] != _receipt_hash(r) or _hash(r["text"]) != r["content_sha"]):
            raise ValueError("receipt identity or hash mismatch")
        note_part = int(r.get("note_part", part))
        if note_part == part and r.get("note_project", r.get("project")) != r.get("project"):
            raise ValueError("capture_identity_conflict: initial capture project differs from note project")
        if note_part < part and notes.continuation_project(r["text"], part) != r.get("project"):
            raise ValueError("capture_identity_conflict: continuation project differs from note receipt")
        _validated_path(cfg, r["note_path"])
        _validate_note(r["text"], agent, sid, note_part,
                       expected_project=r.get("note_project", r.get("project")))
        return r
    except (ValueError, KeyError, TypeError) as error:
        raise ValueError("capture_identity_conflict: invalid receipt") from error


def _validated_path(cfg, relative):
    path = (cfg.vault / relative).resolve()
    if not path.is_relative_to(cfg.sessions_dir.resolve()):
        raise ValueError("capture_identity_conflict: note outside session directory")
    return path


def _validate_note(text, agent, sid, part, expected_project=None):
    fields = notes.session_metadata(text)
    if (fields.get("agent"), fields.get("session_id"), fields.get("part")) != (agent, sid, str(part)):
        raise ValueError("capture_identity_conflict: note identity mismatch")
    if expected_project is not None and fields.get("project") != expected_project:
        raise ValueError("capture_identity_conflict: note project mismatch")
    return fields


def _legacy_evidence(cfg, agent, sid, part):
    suffix = "".join(ch for ch in sid if ch.isalnum())[-8:]
    tail = f"-p{part}" if part > 1 else ""
    matches = []
    for path in cfg.sessions_dir.glob(f"*/*/*-{suffix}{tail}.md"):
        text = path.read_text(encoding="utf-8")
        fields = notes.session_metadata(text)
        if (fields.get("agent"), fields.get("part"), fields.get("session_id")) == (agent, str(part), "[REDACTED]"):
            raise ValueError("capture_identity_conflict: legacy session identity is masked")
        if (fields.get("agent"), fields.get("session_id"), fields.get("part")) == (agent, sid, str(part)):
            matches.append((path, text, fields))
    if len(matches) > 1:
        raise ValueError("capture_identity_conflict: ambiguous legacy notes")
    return matches[0] if matches else None


def _link(cfg: Config, path: Path) -> str:
    return path.relative_to(cfg.vault).with_suffix("").as_posix()


def finalize(cfg: Config, man: Manifest, row, summarizer=None, consolidator=None) -> str:
    """Recover the frozen receipt, or prepare one before publishing immutable evidence."""
    agent, sid, part = row["agent"], row["session_id"], row["parts"] + 1
    key = _identity_key(agent, sid, part)
    with file_lock(cfg.locks_dir / f"capture-{key}.lock", timeout=cfg.lock_timeout_s):
        fresh = man.get(agent, sid)
        if fresh["parts"] != row["parts"]:
            return "already-captured"
        configured_summarizer = summarizer or (
            lambda digest, timeout: llm.summarize(digest, timeout=timeout,
                                                  email_allowlist=cfg.privacy_email_allowlist))
        return _finalize_locked(cfg, man, fresh, configured_summarizer, consolidator)


def _finalize_locked(cfg, man, row, summarizer, consolidator):
    agent, sid, part = row["agent"], row["session_id"], row["parts"] + 1
    receipt = _load_receipt(cfg, agent, sid, part, row["captured_until"])
    if receipt is None:
        legacy = _legacy_evidence(cfg, agent, sid, part)
        if legacy:
            path, text, fields = legacy
            summary = notes.summary_from_session(text)
            start, end = notes.source_range(text)
            if start != row["captured_until"]:
                raise ValueError("capture_identity_conflict: legacy range mismatch")
            receipt = dict(version=1, agent=agent, session_id=sid, part=part, start=start, end=end,
                           project=fields["project"], summary=summary, digest_sha=_hash(text),
                           note_path=path.relative_to(cfg.vault).as_posix(), text=text, content_sha=_hash(text),
                           when=fields["date"], summarizer=fields.get("summarizer", "legacy"), cost=0,
                           prefix_sha="", evidence="legacy-note", related_projects=[])
        else:
            source = _source_for(row)
            if not source:
                raise FileNotFoundError(f"no transcript for {agent}:{sid}")
            if row["parts"] and agent == "opencode":
                previous = _load_receipt(cfg, agent, sid, part - 1, man.capture(agent, sid, part - 1)["start_offset"])
                if previous and previous.get("prefix_sha"):
                    prefix = adapters.read(agent, sid, source, end=row["captured_until"])
                    if prefix.prefix_sha != previous["prefix_sha"]:
                        raise ValueError("source_change: earlier OpenCode messages mutated")
            original_messages = None
            if agent == "opencode" and cfg.knowledge_mode != "off":
                exported = adapters.export_opencode(sid)
                t = adapters.read_opencode(sid, start=row["captured_until"], data=exported)
                original_messages = exported.get("messages") or []
            else:
                t = adapters.read(agent, sid, source, start=row["captured_until"])
            source_error = _source_error(t.warnings)
            for warning in t.warnings:
                log(cfg, "capture-source-warning", agent=agent, session=sid, **warning)
            if t.session_id != sid:
                raise ValueError("capture_identity_conflict: transcript session differs")
            cwd = t.cwd or row["cwd"] or ""
            done_status = "captured" if row["parts"] else "skipped"
            if t.end <= row["captured_until"] or not t.events:
                man.mark(agent, sid, status=done_status, captured_until=max(row["captured_until"], t.end),
                         end_seen=0, skip_reason=row["skip_reason"] or "no new content",
                         last_error=source_error)
                return "no-new-content"
            if _excluded(cfg, cwd):
                man.mark(agent, sid, status="skipped", captured_until=t.end, end_seen=0, skip_reason="excluded directory")
                return "excluded"
            d = extract.build(t, cfg.min_user_prompts, cfg.min_assistant_chars, cfg.digest_char_cap,
                              email_allowlist=cfg.privacy_email_allowlist)
            if not d.substantial:
                man.mark(agent, sid, status=done_status, captured_until=t.end, end_seen=0,
                         skip_reason=d.gate_reason, cwd=cwd,
                         last_error=source_error)
                return "trivial"
            edited = [p for ev in t.events if ev.kind == "edit" for p in ev.paths]
            attribution = projects.attribute(cwd, cfg.projects_map, files=edited, text="\n".join(d.prompts))
            project = attribution.project
            if ksources._privacy_for(cfg, project) == "excluded":
                man.mark(agent, sid, status="skipped", captured_until=t.end, end_seen=0,
                         project=project, skip_reason="project excluded")
                return "excluded"
            repo_head = projects.repo_head(cwd)
            when = _parse_time(t.started)
            cost, kind, summary = 0.0, "deterministic", None
            if (cfg.summarizer == "claude-haiku"
                    and ksources._privacy_for(cfg, project) not in ("local_only", "excluded")):
                try:
                    summary = summarizer(d.text, timeout=cfg.llm_timeout_s)
                    cost = summary.pop("_cost_usd", 0.0)
                    kind = "claude-haiku"
                except llm.LLMError as error:
                    log(cfg, "llm-fallback", agent=agent, session=sid, step="summarize", error=str(error))
            if summary is None:
                summary = extract.fallback_summary(t, d)
            summary = _redact_values(summary, cfg.privacy_email_allowlist)
            summary["repo_head"] = repo_head
            source_hash = _source_range_hash(t)
            previous = man.capture(agent, sid, part - 1) if part > 1 else None
            previous_path = _validated_path(cfg, previous["note_path"]) if previous else None
            previous_text = previous_path.read_text(encoding="utf-8") if previous_path and previous_path.exists() else ""
            previous_fields = notes.session_metadata(previous_text) if previous_text else {}
            is_continuation = previous_fields.get("logical_session") == "true"
            note_part = int(previous_fields.get("part", "1")) if is_continuation else part
            path = previous_path if is_continuation else notes.session_note_path(
                cfg.sessions_dir, when, agent, summary.get("title") or t.title or "session", sid, part)
            if is_continuation:
                text = notes.append_session_continuation(previous_text, when=when, source=t.source, start=t.start,
                        end=t.end, source_hash=source_hash, summarizer=kind, summary=summary, files=d.files,
                        commands=d.commands, gate_reason=d.gate_reason, capture_part=part, project=project,
                        email_allowlist=cfg.privacy_email_allowlist)
            else:
                text = notes.render_session(agent=agent, session_id=sid, project=project, part=part, when=when,
                        started=t.started, ended=t.ended, source=t.source, start=t.start, end=t.end, summarizer=kind,
                        summary=summary, files=d.files, commands=d.commands, gate_reason=d.gate_reason,
                        prev_link=Path(previous["note_path"]).with_suffix("").as_posix() if previous else None,
                        project_note=_link(cfg, projects.note_path(cfg.vault, project)), repo_head=repo_head,
                        attribution=attribution, source_hash=source_hash, email_allowlist=cfg.privacy_email_allowlist)
            text = redact_session_note(text, cfg.privacy_email_allowlist)
            receipt = dict(version=1, agent=agent, session_id=sid, part=part, start=t.start, end=t.end,
                           project=project, summary=summary, digest_sha=d.sha, note_path=path.relative_to(cfg.vault).as_posix(),
                           note_part=note_part, note_project=previous_fields.get("project", project),
                           base_content_sha=_hash(previous_text) if is_continuation else None,
                           text=text, content_sha=_hash(text), when=when.isoformat(), summarizer=kind, cost=cost,
                           prefix_sha=t.prefix_sha, evidence="transcript", related_projects=attribution.related_projects,
                           source_warnings=_redact_values(t.warnings, cfg.privacy_email_allowlist))
            if cfg.knowledge_mode != "off":
                try:
                    receipt["sources"] = ksources.build_receipt_packets(t, repo_head, part=part,
                        note_path=receipt["note_path"], origin_group=d.sha,
                        privacy=ksources._privacy_for(cfg, project) or "model_allowed", original_messages=original_messages,
                        email_allowlist=cfg.privacy_email_allowlist)
                except Exception:
                    log(cfg, "knowledge-packet-error", agent=agent, session=sid, part=part)
        if ksources._privacy_for(cfg, receipt["project"]) == "excluded":
            man.mark(agent, sid, status="skipped", end_seen=0, project=receipt["project"], skip_reason="project excluded")
            return "excluded"
        receipt["receipt_sha"] = _receipt_hash(receipt)
        create_once(_receipt_path(cfg, agent, sid, part), json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n")
        receipt = _load_receipt(cfg, agent, sid, part, row["captured_until"])
    if ksources._privacy_for(cfg, receipt["project"]) == "excluded":
        man.mark(agent, sid, status="skipped", end_seen=0, project=receipt["project"], skip_reason="project excluded")
        return "excluded"
    path = _validated_path(cfg, receipt["note_path"])
    base_sha = receipt.get("base_content_sha")
    if base_sha:
        if not path.exists():
            raise ValueError("capture_identity_conflict: continuation base note is missing")
        current_sha = _hash(path.read_text(encoding="utf-8"))
        if current_sha == base_sha:
            atomic_write(path, receipt["text"])
        elif current_sha != receipt["content_sha"]:
            raise ValueError("capture_identity_conflict: continuation base note changed")
    else:
        create_once(path, receipt["text"])
    persisted = path.read_bytes()
    if hashlib.sha256(persisted).hexdigest() != receipt["content_sha"]:
        raise ValueError("capture_identity_conflict: persisted note bytes differ")
    _validate_note(persisted.decode("utf-8"), agent, sid, receipt.get("note_part", part),
                   expected_project=receipt.get("note_project", receipt.get("project")))
    link = _link(cfg, path)
    project, summary, kind = receipt["project"], receipt["summary"], receipt["summarizer"]
    when = _parse_time(receipt["when"])
    cost, consolidated = receipt["cost"], False
    if project != projects.UNSORTED:
        capture_id = _capture_identity(agent, sid, part)
        extra_cost, consolidated = consolidate_into_project(cfg, project, summary, link, when, kind, consolidator,
                                                            order_part=part, capture_id=capture_id)
        cost += extra_cost
    for related in receipt["related_projects"]:
        note_path = projects.note_path(cfg.vault, related)
        with file_lock(cfg.locks_dir / f"project-{notes.slug(related, 12)}.lock", timeout=cfg.lock_timeout_s):
            notes.update_project_note(note_path, related, f"{when:%Y-%m-%d}", session_link=link,
                     session_line=f"- {when:%Y-%m-%d} [[{link}|{summary.get('title') or 'session'}]] (related)",
                     email_allowlist=cfg.privacy_email_allowlist)
    man.record_capture(agent=agent, session_id=sid, part=part, note_path=receipt["note_path"], project=project,
                       title=summary.get("title", ""), date=f"{when:%Y-%m-%d}", start=receipt["start"], end=receipt["end"],
                       summarizer=kind, cost=cost, sha=receipt["digest_sha"], consolidated=consolidated,
                       summary_json=json.dumps(summary, ensure_ascii=False),
                       error=_source_error(receipt.get("source_warnings", [])))
    if project != projects.UNSORTED:
        exclude = {(agent, sid, part)} if not consolidated else set()
        consolidate_pending(cfg, man, summarizer=summarizer, consolidator=consolidator, exclude=exclude)
    if cfg.knowledge_mode != "off" and project != projects.UNSORTED:
        try:
            outcome = ksources.maybe_import_after_capture(cfg, receipt)
            if outcome.status == "error":
                log(cfg, "knowledge-import-error", agent=agent, session=sid, part=part, reason=outcome.reason)
        except Exception as e:  # noqa: BLE001 - knowledge must never break capture
            log(cfg, "knowledge-import-error", agent=agent, session=sid, part=part, project=project,
                error=f"{type(e).__name__}: {e}")
    log(cfg, "captured", agent=agent, session=sid, part=part, project=project, note=link, summarizer=kind,
        consolidated=consolidated, cost_usd=round(cost, 5))
    return "captured"


def _source_error(warnings):
    return json.dumps({"source_warnings": warnings}, separators=(",", ":")) if warnings else None


def _redact_values(value, email_allowlist=None):
    if isinstance(value, str):
        return redact(value, email_allowlist=email_allowlist)
    if isinstance(value, dict):
        return {k: _redact_values(v, email_allowlist) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_values(v, email_allowlist) for v in value]
    return value


def _discover_orphan_transcripts(cfg: Config, man: Manifest, now=None) -> int:
    """Register aged Claude and Codex JSONL transcripts before the ordinary settle-window drain.

    It uses the configured roots and file modification time for the existing settle rule, then lets
    normal parsing, gating, privacy, and retry handling decide whether to capture. OpenCode exports
    require a known session id, so they remain hook-bound.
    """
    now = time.time() if now is None else now
    settle_s = cfg.settle_minutes * 60
    discovery_since = man.discovery_since(now)
    discovered = 0
    registered = 0
    roots = (("claude", Path(cfg.claude_transcripts_root)), ("codex", Path(cfg.codex_transcripts_root)))
    candidates = []
    for agent, root in roots:
        if not root.is_dir():
            continue
        paths = root.glob("*/*.jsonl") if agent == "claude" else root.rglob("rollout-*.jsonl")
        for source in paths:
            try:
                modified = source.stat().st_mtime
                if agent == "claude":
                    uuid_pattern = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
                    if not re.fullmatch(uuid_pattern, source.stem):
                        continue
                    known_id = source.stem
                else:
                    match = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", source.name)
                    known_id = match.group(0) if match else ""
                if not known_id or modified > now - settle_s:
                    continue
                known = man.get(agent, known_id)
                if known and known["status"] not in ("captured", "skipped"):
                    continue
                if not known and modified <= discovery_since:
                    continue
                candidates.append((modified, agent, source, known_id, known))
            except OSError:
                continue

    for modified, agent, source, known_id, known in sorted(candidates, key=lambda item: (item[0], str(item[2]))):
        if known is None and registered >= cfg.discovery_max_per_drain:
            continue
        try:
            transcript = adapters.read(agent, source.stem, str(source))
            if not transcript.session_id or not transcript.cwd or not transcript.events:
                continue
            if _summarizer_cwd(transcript.cwd):
                continue
            known = man.get(agent, transcript.session_id)
            if known:
                if known["status"] in ("captured", "skipped") and transcript.end > known["captured_until"]:
                    discovered += int(man.reopen_for_growth(agent, transcript.session_id, end=transcript.end,
                        source=str(source), cwd=transcript.cwd, modified=modified))
                continue
            man.touch(agent, transcript.session_id, cwd=transcript.cwd, source=str(source), now=modified)
            discovered += 1
            registered += 1
        except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
            continue
    return discovered


def _summarizer_cwd(cwd: str) -> bool:
    """Match the neutral cwd used by llm._run without excluding other temp paths."""
    try:
        relative = Path(cwd).resolve().relative_to(Path(tempfile.gettempdir()).resolve())
    except (OSError, ValueError):
        return False
    return bool(relative.parts and relative.parts[0].startswith("agentmem-llm-"))


def _pending_lines(summary: dict, link: str, date: str) -> list:
    out = [f"- [ ] {date} [[{link}]] — {d['decision']}" for d in summary.get("decisions", [])[:6]]
    out += [f"- [ ] {date} [[{link}]] — fact: {s}" for s in summary.get("facts", [])[:4]]
    out += [f"- [ ] {date} [[{link}]] — next: {s}" for s in summary.get("next_steps", [])[:4]]
    out += [f"- [ ] {date} [[{link}]] — question: {s}" for s in summary.get("open_questions", [])[:4]]
    return out or [f"- [ ] {date} [[{link}]] — review session for state changes"]


def _pending_marker(pending: str, link: str, capture_id=None):
    identity = f"capture-ref:{capture_id}" if capture_id else f"[[{link}]]"
    for line in pending.splitlines():
        if f"[[{link}]]" not in line or identity not in line or "status update pending" not in line:
            continue
        base = re.search(r"\bbase=([0-9a-f]{64})\b", line)
        order = re.search(r"\border=(\d+):(\d+)\b", line)
        return (base[1] if base else None, (int(order[1]), int(order[2])) if order else None)
    return None, None


def _capture_identity(agent, sid, part):
    return hashlib.sha256(json.dumps([agent, sid, int(part)], separators=(",", ":")).encode("utf-8")).hexdigest()


def consolidate_into_project(cfg: Config, project: str, summary: dict, link: str, when: datetime,
                             summary_kind: str, consolidator=None, order_override=None, order_part: int = 1,
                             capture_id=None, retry: bool = False) -> tuple:
    """Reconcile one session into its project note. Returns (cost, consolidated?)."""
    from . import status
    if consolidator is None:
        consolidator = lambda state, current_summary, link, date, timeout: llm.consolidate(
            state, current_summary, link, date, timeout=timeout, email_allowlist=cfg.privacy_email_allowlist)
    if ksources._privacy_for(cfg, project) == "excluded":
        return 0.0, False
    note_path = projects.note_path(cfg.vault, project)
    date = f"{when:%Y-%m-%d}"
    capture_marker = f"<!-- agentmem:capture:{capture_id} -->" if capture_id else ""
    pending_ref = f"capture-ref:{capture_id}" if capture_id else f"[[{link}]]"
    session_line = f"- {date} [[{link}|{summary.get('title') or 'session'}]]"
    consolidated_session_line = f"{session_line} {capture_marker}".rstrip()
    with file_lock(cfg.locks_dir / f"project-{notes.slug(project, 12)}.lock", timeout=cfg.lock_timeout_s):
        refused = notes.recover_status_update(note_path)
        if refused:
            log(cfg, 'status-replay-refused', project=project, journal=refused.name)
        if not note_path.exists():
            notes.update_project_note(note_path, project, date, email_allowlist=cfg.privacy_email_allowlist)
        text = notes.ensure_blocks(note_path.read_text(encoding="utf-8"))
        current_state = notes.get_block(text, notes.STATE_BEGIN, notes.STATE_END)
        pending = notes.get_block(text, notes.PENDING_BEGIN, notes.PENDING_END)
        order_time = int(order_override if order_override is not None else when.timestamp() * 1_000_000)
        order = (order_time, int(order_part))
        base_revision = hashlib.sha256(current_state.encode("utf-8")).hexdigest()
        pending_base, pending_order = _pending_marker(pending, link, capture_id)
        session_block = notes.get_block(text, notes.SESS_BEGIN, notes.SESS_END)
        session_marker = capture_marker if capture_id else f"[[{link}|"
        if (session_marker in session_block
                and pending_ref not in pending):
            return 0.0, True
        if (summary_kind != "deterministic" and cfg.summarizer != "none"
                and ksources._privacy_for(cfg, project) not in ("local_only", "excluded")):
            try:
                old = status.live_page(current_state)
                if not status.evidence_refs(summary, link):
                    raise llm.LLMError('uncited summary, status not updated')
                result, cost = consolidator(current_state, summary,
                                           f"[[{link}]]", date, timeout=cfg.llm_timeout_s)
                source_orders = notes.evidence_orders(text, old)
                if isinstance(result, status.Result):
                    result = status.proposed_update(result, old)
                result = status.reconcile(old, result, status.evidence_support(summary, link),
                                          source_orders=source_orders, new_order=order)
                retained_orders = {item.ref: source_orders.get(item.ref, order)
                                   for items in result.sections.values() for item in items}
                if ksources._privacy_for(cfg, project) == "excluded":
                    return cost, False
                for action, item in result.events:
                    log(cfg, 'status-item', project=project, note=link, action=action, item=item)
                notes.update_project_note(note_path, project, date, status_result=result,
                                          update_key=f'{capture_id or link}:{order[0]}:{order[1]}', pending_clear=pending_ref,
                                          session_link=link, session_line=consolidated_session_line,
                                          state_order_value=max(notes.state_order(text), order),
                                          evidence_order_values=retained_orders,
                                          email_allowlist=cfg.privacy_email_allowlist)
                return cost, True
            except (llm.LLMError, ValueError) as e:
                log(cfg, "llm-fallback", step="consolidate", project=project, note=link, error=str(e))
                reason = str(e)
        else:
            reason = "summary is deterministic or project consolidation is unavailable"
        reason = " ".join(reason.split())
        pending_order = pending_order or order
        pending_lines = [f"- [ ] {date} [[{link}]] {pending_ref} — status update pending ({reason[:180]}; "
                         f"base={pending_base or base_revision}; order={pending_order[0]}:{pending_order[1]})"]
        pending_lines += [f"{line} {pending_ref}" for line in _pending_lines(summary, link, date)
                          if "status update pending" not in line]
        notes.update_project_note(note_path, project, date, pending_clear=pending_ref,
                                  pending_add=pending_lines,
                                  session_link=link, session_line=session_line,
                                  email_allowlist=cfg.privacy_email_allowlist, touch=not retry)
        return 0.0, False


def _validate_pending_identity(cfg, man, row, text, summary, receipt=None):
    memory = row["summarizer"] == "remember"
    fields = notes.session_metadata(text, note_type="memory" if memory else "session")
    actual_part = fields.get("part", "1" if memory else None)
    expected_part = int(receipt.get("note_part", row["part"])) if receipt else int(row["part"])
    expected_project = (receipt.get("note_project", receipt.get("project")) if receipt else row["project"])
    if (fields.get("agent") != row["agent"] or fields.get("project") != expected_project
            or actual_part != str(expected_part)):
        raise ValueError("capture_identity_conflict: pending note identity or project differs")
    sid = fields.get("session_id")
    if sid == row["session_id"]:
        return
    if receipt:
        raise ValueError("capture_identity_conflict: pending receipt session differs")
    if sid != "[REDACTED]":
        raise ValueError("capture_identity_conflict: pending session differs")
    path = _validated_path(cfg, row["note_path"])
    matches = sum(_validated_path(cfg, r[0]) == path for r in man.db.execute("SELECT note_path FROM captures"))
    statement = summary.get("summary", "")
    body = text.split("\n---\n", 1)[1]
    if matches != 1 or not isinstance(statement, str) or not statement.strip() or " ".join(statement.split()) not in " ".join(body.split()):
        raise ValueError("capture_identity_conflict: masked pending identity lacks unique supported manifest")
    if not memory and notes.source_range(text) != (row["start_offset"], row["end_offset"]):
        raise ValueError("capture_identity_conflict: masked pending source range differs")


def _receipt_section_present(text, receipt, part):
    receipt_text = receipt["text"]
    if int(part) == int(receipt.get("note_part", part)):
        return text.startswith(receipt_text)
    sections = re.split(r"(?m)(?=^## Continued )", receipt_text)
    for section in sections:
        match = re.search(r"(?m)^- Capture part: (\d+)$", section)
        if match and int(match[1]) == int(part):
            return section.rstrip() in text
    return False


def consolidate_pending(cfg: Config, man: Manifest, summarizer=None, consolidator=None, limit: int = 20,
                        exclude: set = None) -> dict:
    """Retry reconciliation for captures that fell back (LLM down, or deterministic summary)."""
    if summarizer is None:
        summarizer = lambda digest, timeout: llm.summarize(
            digest, timeout=timeout, email_allowlist=cfg.privacy_email_allowlist)
    from . import status
    done = failed = 0
    excluded = exclude or set()
    rows = [row for row in man.retryable() if (row["agent"], row["session_id"], row["part"]) not in excluded]
    for row in rows[:limit]:
        if ksources._privacy_for(cfg, row["project"]) == "excluded":
            failed += 1
            continue
        summary = json.loads(row["summary_json"] or "{}")
        kind = row["summarizer"]
        try:
            path = _validated_path(cfg, row["note_path"])
            persisted = path.read_bytes()
            text = persisted.decode("utf-8")
            receipt = _load_receipt(cfg, row["agent"], row["session_id"], row["part"], row["start_offset"] or 0)
            _validate_pending_identity(cfg, man, row, text, summary, receipt)
            fields = notes.session_metadata(text, note_type="memory" if row["summarizer"] == "remember" else "session")
            if receipt and (receipt.get("project") != row["project"]
                            or receipt.get("note_path") != row["note_path"]
                            or receipt.get("start") != row["start_offset"]
                            or receipt.get("end") != row["end_offset"]
                            or not _receipt_section_present(text, receipt, row["part"])
                            or summary != receipt["summary"]):
                raise ValueError("capture_identity_conflict: pending evidence mismatch")
        except (OSError, ValueError) as error:
            log(cfg, "pending-retry-failed", session=row["session_id"], error=str(error))
            failed += 1
            continue
        # Reconciliation uses the summary that produced the immutable note.
        if kind == "deterministic" and cfg.summarizer != "none":
            kind = "persisted"
        link = Path(row["note_path"]).with_suffix("").as_posix()
        session_when = _parse_time(fields.get("started")) if fields.get("started") else _parse_time(row["date"])
        capture_id = (None if row["summarizer"] == "remember" else
                      _capture_identity(row["agent"], row["session_id"], row["part"]))
        cost, ok = consolidate_into_project(cfg, row["project"], summary, link, session_when, kind, consolidator,
                                            order_override=int(session_when.timestamp() * 1_000_000),
                                            order_part=int(row["part"]), capture_id=capture_id, retry=True)
        if ok:
            man.set_consolidated(row["agent"], row["session_id"], row["part"], 1, cost)
            done += 1
        else:
            failed += 1
            # An uncited summary fails the same way on every retry, so hold it instead of letting it fill the batch.
            if (kind != "deterministic" and ksources._privacy_for(cfg, row["project"]) != "local_only"
                    and not status.evidence_refs(summary, link)):
                man.hold_retry(row["agent"], row["session_id"], row["part"], "uncited summary")
                log(cfg, "pending-retry-held", session=row["session_id"], part=row["part"], reason="uncited summary")
    return {"consolidated": done, "still_pending": failed}


def process_ready(cfg: Config, man: Manifest, budget_s: float = 900, summarizer=None, consolidator=None) -> list:
    results = []
    deadline = time.monotonic() + budget_s
    _discover_orphan_transcripts(cfg, man)
    for row in man.ready(cfg.settle_minutes * 60, cfg.max_attempts):
        if time.monotonic() > deadline or cfg.paused():
            break
        try:
            with file_lock(cfg.locks_dir / f"session-{row['agent']}-{notes.slug(row['session_id'], 12)}.lock", timeout=5):
                fresh = man.get(row["agent"], row["session_id"])  # re-read under the lock
                outcome = finalize(cfg, man, fresh, summarizer, consolidator)
        except Exception as e:  # noqa: BLE001 - any failure is recorded and retried with backoff
            man.fail(row["agent"], row["session_id"], f"{type(e).__name__}: {e}")
            log(cfg, "error", agent=row["agent"], session=row["session_id"], error=f"{type(e).__name__}: {e}",
                trace=traceback.format_exc(limit=3))
            outcome = "error"
        results.append((row["agent"], row["session_id"], outcome))
    return results
