"""Read-only knowledge command adapters."""
import sqlite3
from pathlib import Path

from .. import config
from .models import KnowledgeError
from .retrieval import ContextRequest, QmdClient, build_context, error_response, resolve_scope
from .store import KnowledgeStore
from .policy import canonical


EXIT_CODES = {"ok": 0, "invalid_request": 2, "busy": 4, "unavailable": 4, "privacy_violation": 5}


def render_text(payload):
    if payload.get("errors"):
        return "\n".join(error["message"] + " (" + error["code"] + ")" for error in payload["errors"])
    lines = ["Project " + str(payload.get("project_id")), "Mode " + payload.get("mode", "current")]
    for item in payload.get("items", []):
        if item["status"] == "unresolved":
            lines.append("Unresolved alternatives (disputed).")
        for claim in item.get("alternatives", [item]):
            labels = [claim["status"], claim["evidence_level"], claim["freshness"]]
            if claim["user_stated"]:
                labels.append("user_stated")
            lines.append("[" + ", ".join(labels) + "] " + claim["statement"])
            if claim["rationale"]:
                lines.append("Reason: " + claim["rationale"])
            if claim.get("freshness_warning"):
                lines.append(claim["freshness_warning"])
            for citation in claim["citations"]:
                lines.append("Source " + citation["note_path"] + " [" + str(citation["start"]) + ":" + str(citation["end"]) + "] "
                    + citation["speaker"] + ": " + citation["quote"])
        if item.get("omitted_alternatives"):
            lines.append("Omitted alternatives: " + str(item["omitted_alternatives"]))
    lines.extend(payload.get("warnings", []))
    if "used" in payload:
        lines.append(str(payload["used"]) + "/" + str(payload["budget"]) + " " + payload["budget_unit"])
    if "counts" in payload:
        lines.append(canonical(payload["counts"]))
    return "\n".join(lines)


def _request(args, qmd_timeout_ms=1500):
    return ContextRequest(" ".join(getattr(args, "terms", [])), project=args.project, cwd=str(Path.cwd()),
        mode=getattr(args, "mode", "current"), as_of=getattr(args, "as_of", None),
        budget=getattr(args, "budget", 2400), semantic=getattr(args, "semantic", False),
        tokenizer_id=getattr(args, "tokenizer", None), qmd_timeout_ms=qmd_timeout_ms)


def _serve(args, status_only=False):
    try:
        cfg = config.load(args.vault)
        with KnowledgeStore(cfg.vault / cfg.knowledge_db, vault=cfg.vault) as store:
            request = _request(args, cfg.knowledge_qmd_timeout_ms)
            project = resolve_scope(store, request)
            if cfg.project_privacy.get(project["name"]) == "excluded":
                payload = error_response("project_excluded", "privacy_violation").to_dict()
            elif status_only:
                counts = dict(store.db.execute("SELECT status,COUNT(*) FROM claims WHERE project_id=? GROUP BY status", (project["id"],)))
                payload = dict(schema_version="1.0", status="ok", errors=[], project_id=project["id"],
                    project_revision=project["revision"], mode=project["mode"], privacy=project["privacy"], counts=counts)
            else:
                payload = build_context(store, request, QmdClient(cfg.qmd_index)).to_dict()
    except (KnowledgeError, sqlite3.Error, OSError, ValueError, SystemExit) as error:
        code = error.code if isinstance(error, KnowledgeError) else "store_unavailable"
        status = "privacy_violation" if code == "project_excluded" else "invalid_request" if code in (
            "project_not_found", "ambiguous_project", "archived_requires_history") else "unavailable"
        payload = error_response(code, status).to_dict()
    print(canonical(payload) if args.json else render_text(payload))
    return EXIT_CODES[payload["status"]]


def cmd_context(args):
    return _serve(args)


def cmd_status(args):
    return _serve(args, status_only=True)


def register(subparsers):
    context = subparsers.add_parser("context", help="bounded project context from supported knowledge")
    context.add_argument("terms", nargs="*")
    context.add_argument("--project")
    context.add_argument("--mode", choices=("current", "history"), default="current")
    context.add_argument("--as-of")
    context.add_argument("--budget", type=int, default=2400)
    context.add_argument("--semantic", action="store_true", help="optional QMD vector search; calls an embedding model")
    context.add_argument("--tokenizer")
    context.add_argument("--json", action="store_true")
    context.set_defaults(fn=cmd_context)
    knowledge = subparsers.add_parser("knowledge", help="knowledge service status")
    commands = knowledge.add_subparsers(dest="knowledge_command", required=True)
    status = commands.add_parser("status", help="read-only project counts")
    status.add_argument("--project")
    status.add_argument("--json", action="store_true")
    status.set_defaults(fn=cmd_status)
