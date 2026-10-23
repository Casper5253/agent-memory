"""Retrieval over the vault via QMD (BM25 + optional vectors), labelled by knowledge layer.

Every hit says whether it is CURRENT project state, a HISTORICAL session record, a REFERENCE note or
ARCHIVED, and carries its on-disk path so the caller can verify it.
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from .config import Config
from . import notes

COLLECTIONS = {
    "vault-projects": ("1-Projects", "CURRENT", "Current, reconciled state of active projects. Authoritative for 'what is true now'."),
    "vault-sessions": ("0-Sessions", "HISTORICAL", "Immutable records of past agent sessions. Evidence of what was done/decided then; may be outdated."),
    "vault-areas": ("2-Areas", "REFERENCE", "Ongoing responsibilities and areas of interest."),
    "vault-resources": ("3-Resources", "REFERENCE", "Reusable technical knowledge: concepts, patterns, lessons."),
    "vault-archive": ("4-Archive", "ARCHIVED", "Inactive projects and retired material. Historical only."),
}
LABEL_BY_FOLDER = {folder: label for folder, label, _ in COLLECTIONS.values()}


def qmd_cmd() -> "list | None":
    """[node, .../@tobilu/qmd/bin/qmd]: avoids the .cmd shim so arguments never pass through a shell."""
    shim = shutil.which("qmd")
    node = shutil.which("node")
    if not shim:
        return None
    js = Path(shim).parent / "node_modules" / "@tobilu" / "qmd" / "bin" / "qmd"
    if node and js.exists():
        return [node, str(js)]
    return [shim] if not shim.lower().endswith((".cmd", ".bat")) else None


def _qmd(args: list, timeout: int = 120, index: str = "") -> subprocess.CompletedProcess:
    base = qmd_cmd()
    if base is None:
        raise FileNotFoundError("qmd not installed")
    env = dict(os.environ, NO_COLOR="1")
    return subprocess.run(base + (["--index", index] if index else []) + args, capture_output=True, timeout=timeout, env=env)


def ensure_collections(cfg: Config) -> list:
    existing = _qmd(["collection", "list"], index=cfg.qmd_index).stdout.decode("utf-8", "replace")
    made = []
    for name, (folder, _, context) in COLLECTIONS.items():
        path = cfg.vault / folder
        path.mkdir(parents=True, exist_ok=True)
        if not re.search(rf"(?m)^\s*{re.escape(name)}\b", existing):
            _qmd(["collection", "add", str(path), "--name", name, "--mask", "**/*.md"], index=cfg.qmd_index)
            made.append(name)
        _qmd(["context", "add", f"qmd://{name}", context], index=cfg.qmd_index)
    return made


def update_index(cfg: Config, embed: "bool | None" = None) -> dict:
    out = {}
    try:
        r = _qmd(["update"], timeout=600, index=cfg.qmd_index)
        out["update"] = r.returncode
        if (cfg.embeddings if embed is None else embed):
            r = _qmd(["embed", "--timeout", "10"], timeout=900, index=cfg.qmd_index)
            out["embed"] = r.returncode
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        out["error"] = str(e)
    return out


def _label(path: str) -> str:
    rel = path.replace("\\", "/")
    for folder, label in LABEL_BY_FOLDER.items():
        if f"/{folder}/" in f"/{rel}" or rel.startswith(folder + "/"):
            return label
    return "OTHER"


def _to_disk(cfg: Config, file_field: str) -> str:
    m = re.match(r"qmd://([^/]+)/([^?]*)", file_field or "")
    if m and m.group(1) in COLLECTIONS:
        return str(cfg.vault / COLLECTIONS[m.group(1)][0] / m.group(2))
    return file_field


def _located_label(path, label, snippet, line=None):
    if label != "CURRENT":
        return label
    try:
        text = Path(path).read_text(encoding="utf-8")
        normalized = " ".join(text.split())
        excerpt = " ".join(snippet.split())
        span_length = 0
        if isinstance(line, int) and line > 0:
            lines = text.splitlines()
            if line <= len(lines) and lines[line - 1].strip() and " ".join(lines[line - 1].split()) in excerpt:
                position = len(" ".join("\n".join(lines[:line - 1]).split())) + 1
            else:
                position = -1
        else:
            position = normalized.find(excerpt) if excerpt and normalized.count(excerpt) == 1 else -1
            span_length = len(excerpt)
        if position < 0:
            return label
        for begin, end, located in ((notes.STATE_BEGIN, notes.STATE_END, "CURRENT"),
                                   (notes.PENDING_BEGIN, notes.PENDING_END, "PENDING"),
                                   (notes.SESS_BEGIN, notes.SESS_END, "RECENT_SESSION")):
            start, stop = normalized.find(begin), normalized.find(end)
            if 0 <= start < position < stop and position + span_length <= stop:
                return located
        return "PROJECT"
    except (OSError, ValueError):
        return label


def _build_hit(cfg, it):
    path = _to_disk(cfg, it.get("file") or it.get("path") or "")
    snippet = re.sub(r"^@@[^@]*@@\s*(\([^)]*\))?\s*", "", " ".join(str(it.get("snippet") or "").split()))[:400]
    label = _label(os.path.relpath(path, cfg.vault) if os.path.isabs(path) else path)
    return {
        "label": _located_label(path, label, snippet, it.get("line")),
        "path": path, "title": it.get("title", ""), "score": it.get("score"),
        "line": it.get("line"), "docid": it.get("docid", ""),
        "snippet": snippet,
    }


def _qmd_search(cfg, terms, n, collections, semantic):
    args = ["vsearch" if semantic else "search", terms, "--format", "json", "-n", str(n)]
    for c in collections:
        args += ["-c", c]
    r = _qmd(args, timeout=180, index=cfg.qmd_index)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "replace")[:300])
    raw = r.stdout.decode("utf-8", "replace")
    return json.loads(raw[raw.index("["):]) if "[" in raw else []


def _split_terms(terms, max_terms=6):
    seen = set()
    words = []
    for w in terms.split():
        key = w.lower()
        if key and key not in seen:
            seen.add(key)
            words.append(w)
        if len(words) >= max_terms:
            break
    return words


def _majority_merge(per_term_hits, num_terms, n):
    path_data = {}
    for term_idx, hits in enumerate(per_term_hits):
        for it in hits:
            path = it["_disk_path"]
            if path not in path_data:
                path_data[path] = {"term_indices": set(), "best_score": None, "best_it": None}
            entry = path_data[path]
            entry["term_indices"].add(term_idx)
            score = it.get("score")
            if score is not None and (entry["best_score"] is None or score > entry["best_score"]):
                entry["best_score"] = score
                entry["best_it"] = it
    threshold = num_terms / 2
    kept = [(p, d) for p, d in path_data.items() if len(d["term_indices"]) > threshold]
    kept.sort(key=lambda x: (-len(x[1]["term_indices"]), -(x[1]["best_score"] or 0), x[0]))
    return [d["best_it"] for _, d in kept[:n]]


def search(cfg: Config, terms: str, n: int = 8, layers=None, semantic: bool = False) -> list:
    collections = [c for c, (_, label, _) in COLLECTIONS.items() if not layers or label in layers]
    words = _split_terms(terms)
    if len(words) <= 1:
        try:
            items = _qmd_search(cfg, terms, n, collections, semantic)
        except Exception as e:  # noqa: BLE001 - QMD down: degrade to a bounded scan, say so
            return fallback_search(cfg, terms, n, layers, reason=str(e))
        return [_build_hit(cfg, it) for it in items]
    per_term_hits = []
    for word in words:
        try:
            items = _qmd_search(cfg, word, n, collections, semantic)
        except FileNotFoundError:
            return fallback_search(cfg, terms, n, layers, reason="qmd not installed")
        except Exception:  # noqa: BLE001
            items = []
        for it in items:
            it["_disk_path"] = _to_disk(cfg, it.get("file") or it.get("path") or "")
        per_term_hits.append(items)
    merged = _majority_merge(per_term_hits, len(words), n)
    hits = [_build_hit(cfg, it) for it in merged]
    for it in merged:
        it.pop("_disk_path", None)
    return hits


def fallback_search(cfg: Config, terms: str, n: int, layers=None, reason: str = "") -> list:
    words = [w.lower() for w in re.findall(r"\w{3,}", terms)]
    scored = []
    for folder, label in LABEL_BY_FOLDER.items():
        if layers and label not in layers:
            continue
        for p in list((cfg.vault / folder).rglob("*.md"))[:3000]:
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            low = text.lower()
            matched = [w for w in words if w in low]
            if not matched or len(matched) <= len(words) / 2:
                continue
            score = sum(low.count(w) for w in words)
            if not score:
                continue
            i = min((low.find(w) for w in matched), default=0)
            located_line = low[:i].count("\n") + 1
            snippet = " ".join(text[max(0, i - 120): i + 280].split())
            scored.append({"label": _located_label(p, label, snippet, located_line), "path": str(p), "title": p.stem, "score": score, "line": located_line,
                           "docid": "", "snippet": " ".join(text[max(0, i - 120): i + 280].split())})
    scored.sort(key=lambda h: -h["score"])
    for h in scored:
        h["fallback"] = reason[:120] or "qmd unavailable"
    return scored[:n]
