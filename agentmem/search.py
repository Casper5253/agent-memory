"""Retrieval over the vault via QMD (BM25 + optional vectors), labelled by knowledge layer.

Every hit says whether it is CURRENT project state, a HISTORICAL session record, a REFERENCE note or
ARCHIVED, and carries its on-disk path so the caller can verify it.
"""
import json
import math
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from . import notes
from .fsutil import NO_WINDOW

COLLECTIONS = {
    "vault-projects": ("1-Projects", "CURRENT", "Current, reconciled state of active projects. Authoritative for 'what is true now'."),
    "vault-sessions": ("0-Sessions", "HISTORICAL", "Immutable records of past agent sessions. Evidence of what was done/decided then; may be outdated."),
    "vault-areas": ("2-Areas", "REFERENCE", "Ongoing responsibilities and areas of interest."),
    "vault-resources": ("3-Resources", "REFERENCE", "Reusable technical knowledge: concepts, patterns, lessons."),
    "vault-archive": ("4-Archive", "ARCHIVED", "Inactive projects and retired material. Historical only."),
}
LABEL_BY_FOLDER = {folder: label for folder, label, _ in COLLECTIONS.values()}

STOPWORDS = frozenset("""
a an and are as at be but by do does did for from has have how if in into is it its me my no not of
on or our out over so than that the their them then there these they this to too us was were what when
where which who why will with you your
""".split())
HISTORY_LINE = re.compile(r"^- (?:\[ \] )?(\d{4}-\d{2}-\d{2})\b")
HISTORY_OVERFLOW = re.compile(r"^- (\d{4}-\d{2}-\d{2}) \[pending-overflow\]\s*$")
HISTORY_MARKER = re.compile(r"^\s*<!--.*-->\s*$")
HISTORY_CAPTURE_REF = re.compile(r"\s*capture-ref:\S+")
HISTORY_TAG = re.compile(r"(\d{4}-\d{2}-\d{2})\s+\[(?!\[)([^\]]*)\]")
HISTORY_CITATION = re.compile(r"\[\[[^\]]*\]\]")
BM25_K1 = 1.2
BM25_B = 0.75


@dataclass(frozen=True)
class HistoryEntry:
    line: int
    date: str
    section: str
    reason: str
    text: str
    citation: str


def _history_continuations(lines, start):
    extra = []
    index = start
    while index < len(lines) and lines[index].strip() and lines[index].startswith("  "):
        extra.append(lines[index].strip())
        index += 1
    return extra, index


def _history_text(raw, extra):
    body = "- " + raw[6:] if raw.startswith("- [ ] ") else raw
    body = HISTORY_CAPTURE_REF.sub("", body)
    return " ".join(" ".join([body, *extra]).split())


def _history_tags(body):
    match = HISTORY_TAG.match(body)
    if not match:
        return "", ""
    section, separator, reason = match[2].partition(";")
    return section.strip(), reason.strip() if separator else ""


def _last_citation(text):
    citations = HISTORY_CITATION.findall(text)
    return citations[-1] if citations else ""


def _parse_history(text):
    """Return (entry, first line, last line) for every dated History block, 1-based."""
    lines = text.splitlines()
    blocks = []
    index = 0
    while index < len(lines):
        raw = lines[index]
        if not raw.strip() or HISTORY_MARKER.match(raw):
            index += 1
            continue
        overflow = HISTORY_OVERFLOW.match(raw)
        if overflow:
            inner = index + 1
            while inner < len(lines) and (not lines[inner].strip() or HISTORY_MARKER.match(lines[inner])):
                inner += 1
            if inner < len(lines) and lines[inner].startswith("- "):
                extra, end = _history_continuations(lines, inner + 1)
                body = _history_text(lines[inner], extra)
                dated = HISTORY_LINE.match(lines[inner])
                entry = HistoryEntry(line=inner + 1, date=dated[1] if dated else overflow[1],
                                     section="pending-overflow", reason="", text=body,
                                     citation=_last_citation(body))
                blocks.append((entry, index + 1, end))
                index = end
                continue
            index += 1
            continue
        start = HISTORY_LINE.match(raw)
        if start:
            extra, end = _history_continuations(lines, index + 1)
            body = _history_text(raw, extra)
            section, reason = _history_tags(raw[2:])
            entry = HistoryEntry(line=index + 1, date=start[1], section=section, reason=reason,
                                 text=body, citation=_last_citation(body))
            blocks.append((entry, index + 1, end))
            index = end
            continue
        index += 1
    return blocks


def history_entries(text: str) -> list[HistoryEntry]:
    return [entry for entry, _, _ in _parse_history(text)]


def _meaningful_terms(query: str) -> list[str]:
    seen = set()
    terms = []
    for word in re.findall(r"\w{3,}", (query or "").lower()):
        if word in STOPWORDS or word in seen:
            continue
        seen.add(word)
        terms.append(word)
        if len(terms) >= 20:
            break
    return terms


def _prefix_match(word, term):
    # Four characters when the shorter word is a full prefix, for task/tasks.
    shared = 0
    for left, right in zip(word, term):
        if left != right:
            break
        shared += 1
    return shared >= 5 or (shared >= 4 and shared == min(len(word), len(term)))


def _entry_hits(text, terms):
    low = HISTORY_CITATION.sub(" ", text).lower()
    words = set(re.findall(r"\w+", low))
    hits = []
    for term in terms:
        if term in low or (len(term) >= 4 and term[:4] in low and any(_prefix_match(word, term) for word in words)):
            hits.append(term)
    return hits


def _entry_score(text, terms):
    return len(_entry_hits(text, terms))


def _history_selection(blocks, terms, qmd_line=None, fallback=True):
    query_terms = _meaningful_terms(terms)
    if query_terms:
        threshold = min(2, len(query_terms))
        candidates = []
        for entry, _, _ in blocks:
            claim = HISTORY_CITATION.sub(" ", entry.text).lower()
            hits = _entry_hits(entry.text, query_terms)
            if len(hits) < threshold:
                continue
            candidates.append((entry, claim, hits, len(re.findall(r"\w+", claim))))
        if candidates:
            count = len(candidates)
            frequency = {term: sum(1 for _, _, hits, _ in candidates if term in hits) for term in query_terms}
            average = sum(length for _, _, _, length in candidates) / count
            scored = []
            for entry, claim, hits, length in candidates:
                score = 0.0
                for term in hits:
                    frequency_term = frequency[term]
                    inverse = math.log(1 + (count - frequency_term + 0.5) / (frequency_term + 0.5))
                    if term in claim:
                        tf = claim.count(term)
                    else:
                        tf = sum(1 for word in set(re.findall(r"\w+", claim)) if _prefix_match(word, term))
                    score += inverse * tf * (BM25_K1 + 1) / (tf + BM25_K1 * (1 - BM25_B + BM25_B * length / average))
                scored.append((-score, -int(entry.date.replace("-", "")), -entry.line, entry))
            scored.sort()
            top = []
            seen = set()
            for row in scored:
                entry = row[3]
                key = " ".join(HISTORY_CITATION.sub(" ", entry.text).lower().split())
                if key in seen:
                    continue
                seen.add(key)
                top.append(entry)
                if len(top) == 3:
                    break
            return sorted(top, key=lambda entry: (entry.date, entry.line), reverse=True)
    if not fallback:
        return []
    if qmd_line is None:
        return [blocks[-1][0]] if blocks else []
    for entry, start, end in blocks:
        if start <= qmd_line <= end:
            return [entry]
    return [min(blocks, key=lambda block: min(abs(qmd_line - block[1]), abs(qmd_line - block[2])))[0]] if blocks else []


def _history_snippet(entries):
    entries = list(entries)
    if not entries:
        return None

    def compose(items):
        prefix = f"{len(items)} matching entries, newest first." if len(items) >= 2 else ""
        return "\n".join(([prefix] if prefix else []) + [entry.text for entry in items])

    kept = []
    for entry in entries:
        trial = kept + [entry]
        if len(compose(trial)) <= 1200:
            kept = trial
        elif not kept:
            return entry.text[:1199].rstrip() + "…", entry.line
        else:
            break
    return compose(kept), kept[0].line


def _history_render(path, terms, qmd_line):
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    blocks = _parse_history(text)
    return _history_snippet(_history_selection(blocks, terms, qmd_line))


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
    return subprocess.run(base + (["--index", index] if index else []) + args, capture_output=True, timeout=timeout, env=env, creationflags=NO_WINDOW)


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
    if _project_history(path):
        return 'HISTORICAL'
    rel = path.replace("\\", "/")
    for folder, label in LABEL_BY_FOLDER.items():
        if f"/{folder}/" in f"/{rel}" or rel.startswith(folder + "/"):
            return label
    return "OTHER"


def _project_history(path):
    path = Path(str(path).replace('\\', '/'))
    return (any(part.casefold() == '1-projects' for part in path.parts[:-1])
            and path.name.casefold() == notes.project_history_path(path).name.casefold())


def _rank_hits(hits, layers=None):
    hits = [hit for hit in hits if not layers or _label(hit['path']) in layers]
    return sorted(hits, key=lambda hit: _project_history(hit['path']))


def _to_disk(cfg: Config, file_field: str) -> str:
    m = re.match(r"qmd://([^/]+)/([^?]*)", file_field or "")
    if m and m.group(1) in COLLECTIONS:
        return str(cfg.vault / COLLECTIONS[m.group(1)][0] / m.group(2))
    return file_field


def _shows_line(line_text, excerpt):
    """True when the excerpt shows the whole hit line, or ends partway through it (QMD cuts snippets)."""
    if not line_text:
        return False
    if line_text in excerpt:
        return True
    head = line_text[:40]
    start = excerpt.rfind(head)
    return start >= 0 and line_text.startswith(excerpt[start:])


def _located_label(path, label, snippet, line=None):
    if _project_history(path):
        return 'HISTORICAL'
    if label != "CURRENT":
        return label
    try:
        text = Path(path).read_text(encoding="utf-8")
        normalized = " ".join(text.split())
        excerpt = re.sub(r"\s*(?:\.\.\.|…)\s*$", "", " ".join(snippet.split()))
        position, span_length = -1, 0
        if isinstance(line, int) and line > 0:
            lines = text.splitlines()
            if line <= len(lines) and _shows_line(" ".join(lines[line - 1].split()), excerpt):
                position = len(" ".join("\n".join(lines[:line - 1]).split())) + 1
        if position < 0:
            # No usable line number (or a stale one): locate the excerpt in the full text, once.
            position = normalized.find(excerpt) if excerpt and normalized.count(excerpt) == 1 else -1
            span_length = len(excerpt)
        if position < 0:
            return "UNVERIFIED"
        for begin, end, located in ((notes.STATE_BEGIN, notes.STATE_END, "CURRENT"),
                                   (notes.PENDING_BEGIN, notes.PENDING_END, "PENDING"),
                                   (notes.SESS_BEGIN, notes.SESS_END, "RECENT_SESSION")):
            start, stop = normalized.find(begin), normalized.find(end)
            if 0 <= start < position < stop and position + span_length <= stop:
                return located
        return "PROJECT"
    except (OSError, ValueError):
        return "UNVERIFIED"


def _build_hit(cfg, it, terms=""):
    path = _to_disk(cfg, it.get("file") or it.get("path") or "")
    snippet = re.sub(r"^@@[^@]*@@\s*(\([^)]*\))?\s*", "", " ".join(str(it.get("snippet") or "").split()))[:400]
    label = _label(os.path.relpath(path, cfg.vault) if os.path.isabs(path) else path)
    line = it.get("line")
    if _project_history(path):
        rendered = _history_render(path, terms, line)
        if rendered:
            snippet, line = rendered
    return {
        "label": _located_label(path, label, snippet, line),
        "path": path, "title": it.get("title", ""), "score": it.get("score"),
        "line": line, "docid": it.get("docid", ""),
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
    collections = [c for c, (_, label, _) in COLLECTIONS.items() if not layers or label in layers
                   or (c == 'vault-projects' and 'HISTORICAL' in layers)]
    words = _split_terms(terms)
    if len(words) <= 1:
        try:
            items = _qmd_search(cfg, terms, n, collections, semantic)
        except Exception as e:  # noqa: BLE001 - QMD down: degrade to a bounded scan, say so
            return fallback_search(cfg, terms, n, layers, reason=str(e))
        return _rank_hits([_build_hit(cfg, it, terms) for it in items], layers)
    try:
        superset_items = _qmd_search(cfg, terms, n, collections, semantic)
    except FileNotFoundError:
        return fallback_search(cfg, terms, n, layers, reason="qmd not installed")
    except Exception as e:  # noqa: BLE001
        return fallback_search(cfg, terms, n, layers, reason=str(e))
    superset_paths = set()
    for it in superset_items:
        it["_disk_path"] = _to_disk(cfg, it.get("file") or it.get("path") or "")
        superset_paths.add(it["_disk_path"])
    per_term_limit = max(25 * n, 200)
    per_term_hits = []
    for word in words:
        try:
            items = _qmd_search(cfg, word, per_term_limit, collections, semantic)
        except FileNotFoundError:
            return fallback_search(cfg, terms, n, layers, reason="qmd not installed")
        except Exception as e:  # noqa: BLE001
            return fallback_search(cfg, terms, n, layers, reason=str(e))
        for it in items:
            it["_disk_path"] = _to_disk(cfg, it.get("file") or it.get("path") or "")
        per_term_hits.append(items)
    merged = _majority_merge(per_term_hits, len(words), n)
    hits = [_build_hit(cfg, it, terms) for it in superset_items]
    seen_paths = set(superset_paths)
    remaining = n - len(hits)
    for it in merged:
        if remaining <= 0:
            break
        if it["_disk_path"] not in seen_paths:
            hits.append(_build_hit(cfg, it, terms))
            seen_paths.add(it["_disk_path"])
            remaining -= 1
    for it in superset_items:
        it.pop("_disk_path", None)
    for it in merged:
        it.pop("_disk_path", None)
    return _rank_hits(hits, layers)


def fallback_search(cfg: Config, terms: str, n: int, layers=None, reason: str = "") -> list:
    words = [w.lower() for w in re.findall(r"\w{3,}", terms)]
    scored = []
    for folder, label in LABEL_BY_FOLDER.items():
        if layers and label not in layers and not (folder == '1-Projects' and 'HISTORICAL' in layers):
            continue
        for p in list((cfg.vault / folder).rglob("*.md"))[:3000]:
            file_label = _label(str(p))
            if layers and file_label not in layers:
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            low = text.lower()
            is_history = _project_history(str(p))
            blocks = _parse_history(text) if is_history else []
            chosen = _history_selection(blocks, terms, None, fallback=False) if blocks else []
            matched = [w for w in words if w in low]
            if not chosen and (not matched or len(matched) <= len(words) / 2):
                continue
            score = sum(low.count(w) for w in words) or len(chosen)
            if not score:
                continue
            i = min((low.find(w) for w in matched), default=0)
            located_line = low[:i].count("\n") + 1
            snippet = " ".join(text[max(0, i - 120): i + 280].split())
            if is_history:
                rendered = _history_snippet(_history_selection(blocks, terms, located_line))
                if rendered:
                    snippet, located_line = rendered
            scored.append({"label": _located_label(p, file_label, snippet, located_line), "path": str(p), "title": p.stem, "score": score, "line": located_line,
                           "docid": "", "snippet": snippet})
    scored.sort(key=lambda h: -h["score"])
    scored = _rank_hits(scored, layers)
    for h in scored:
        h["fallback"] = reason[:120] or "qmd unavailable"
    return scored[:n]
