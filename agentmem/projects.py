"""Attribute each capture part using deterministic content signals and user-authored pins.

_meta/projects.json: {"projects": [{"name": "...", "roots": ["C:\\..."], "status": "active|archived"}]}
Mapped roots define candidates. The cwd resolver remains the fallback and SessionStart source.
Linked worktrees share their parent repository's project.
"""
import json
import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from .fsutil import atomic_write, file_lock
from .logutil import log

HOME = Path.home()
PROJECTS_ROOT = HOME / "Documents" / "Projects"
ENVIRONMENT = "Agent Environment"
UNSORTED = "Unsorted"


def _norm(p: str) -> str:
    p = os.path.normpath(os.path.expanduser(str(p)))
    return p.lower() if os.name == "nt" else p


def repository_root(cwd):
    """Resolve linked worktrees through Git's commondir, without changing their map."""
    if not cwd:
        return ""
    for cur in (Path(cwd), *Path(cwd).parents):
        marker = cur / ".git"
        if marker.is_dir():
            return str(cur)
        if marker.is_file():
            try:
                value = marker.read_text(encoding="utf-8").strip()
                if not value.startswith("gitdir: "):
                    return ""
                gitdir = (cur / value[8:]).resolve()
                common = gitdir / "commondir"
                if common.is_file():
                    shared = (gitdir / common.read_text(encoding="utf-8").strip()).resolve()
                    return str(shared.parent) if shared.name == ".git" else ""
                return str(cur) if gitdir.is_dir() else ""
            except (OSError, ValueError):
                return ""
    return ""


def repo_head(cwd):
    if not cwd or not Path(cwd).is_dir():
        return None
    try:
        result = subprocess.run(["git", "-C", str(cwd), "rev-parse", "HEAD"], capture_output=True, timeout=0.5)
        head = result.stdout.decode("ascii").strip()
        return head if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40,64}", head) else None
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return None


def _routing_cwd(cwd, data):
    if cwd:
        for cur in (Path(cwd), *Path(cwd).parents):
            marker = cur / ".git"
            if marker.is_dir():
                break
            if marker.is_file():
                # A deliberately mapped nested project owns its root even when the
                # containing directory happens to be a linked worktree.
                if any(_is_under(_norm(cwd), _norm(root)) and _norm(root) != _norm(cur)
                       and _is_under(_norm(root), _norm(cur))
                       for proj in data["projects"] for root in proj.get("roots", [])):
                    return cwd
                return repository_root(cwd) or cwd
    return cwd


def _is_under(child: str, root: str) -> bool:
    return child == root or child.startswith(root.rstrip("\\/") + os.sep)


def load_map(path: Path) -> dict:
    if not path.exists():
        return {"projects": []}
    return json.loads(path.read_text(encoding="utf-8"))


def title_case(folder: str) -> str:
    if re.search(r"[A-Z ]", folder):
        return folder.strip()
    return " ".join(w.upper() if len(w) <= 3 and w.isalpha() and w in {"ai", "fde", "ssd", "mcp", "api", "ui"} else w.capitalize()
                    for w in re.split(r"[-_\s]+", folder) if w)


def _derive(cwd: str) -> "tuple[str, str] | None":
    """(name, root) for an unmapped directory, or None for Unsorted."""
    n = _norm(cwd)
    if n == _norm(HOME):  # home-level sessions are about the agent environment itself
        return ENVIRONMENT, os.path.normpath(cwd)
    if n == _norm(PROJECTS_ROOT):  # the Projects root itself is generic (tests, cross-project work): Unsorted
        return None
    for h in (HOME / ".codex", HOME / ".claude", HOME / ".agents", HOME / ".config" / "opencode"):
        if _is_under(n, _norm(h)):
            return ENVIRONMENT, str(h)
    root = _norm(PROJECTS_ROOT)
    if not _is_under(n, root):
        return None
    cur = Path(os.path.normpath(cwd))
    while _norm(cur) != root:
        if (cur / ".git").exists():
            return title_case(cur.name), str(cur)
        cur = cur.parent
    first_level = n[len(root) + 1:].split(os.sep)[0]
    first = next((c for c in PROJECTS_ROOT.iterdir() if c.name.lower() == first_level.lower()), PROJECTS_ROOT / first_level)
    return title_case(first.name), str(first)


GENERIC_DIRS = (HOME, PROJECTS_ROOT, HOME / "Documents", HOME / "Desktop", HOME / "Downloads")
CODEX_SCRATCH = HOME / "Documents" / "Codex"   # Codex desktop's per-task scratch folders
_ABS_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|~[\\/])[^\s\"'<>|*?`]+")
_STOP_NAMES = {"projects", "project", "documents", "codex", "claude", "opencode", "agents", "memory", "notes", "archive", "work", "src", "app"}


def is_generic(cwd: str) -> bool:
    """A working directory that says nothing about which project the session was about."""
    n = _norm(cwd)
    return any(n == _norm(g) for g in GENERIC_DIRS) or _is_under(n, _norm(CODEX_SCRATCH))


def _explicit(path: str, data: dict) -> "str | None":
    best, best_len = None, -1
    target = _norm(path)
    for proj in data["projects"]:
        for root in proj.get("roots", []):
            r = _norm(root)
            if _is_under(target, r) and len(r) > best_len:
                best, best_len = proj["name"], len(r)
    return best


def infer_from_paths(paths, data: dict) -> "str | None":
    """Majority project among concrete file locations (explicit roots or a real folder under Documents\\Projects)."""
    votes = {}
    for p in paths:
        p = os.path.expanduser(str(p).strip().rstrip(".,;:)"))
        if not os.path.isabs(p) or is_generic(p):
            continue
        name = _explicit(p, data)
        if name is None:
            d = _derive(p if os.path.isdir(p) else os.path.dirname(p))
            name = d[0] if d and d[0] != ENVIRONMENT else None
        if name:
            votes[name] = votes.get(name, 0) + 1
    if not votes:
        return None
    ranked = sorted(votes.items(), key=lambda kv: -kv[1])
    return ranked[0][0] if len(ranked) == 1 or ranked[0][1] > ranked[1][1] else None


def infer_from_text(text: str, data: dict) -> "str | None":
    """Paths mentioned in the text first, then project names / folder names mentioned (unique best match)."""
    if not text:
        return None
    by_path = infer_from_paths(_ABS_PATH.findall(text), data)
    if by_path:
        return by_path
    low = text.lower()
    scores = {}
    for proj in data["projects"]:
        names = {proj["name"]} | {Path(r).name for r in proj.get("roots", [])}
        for n in names:
            n = n.strip()
            if len(n) < 4 or n.lower() in _STOP_NAMES:
                continue
            pattern = r"(?<![\w-])" + re.escape(n.lower()).replace(r"\ ", r"[\s_-]+").replace(r"\-", r"[\s_-]+") + r"(?![\w-])"
            hits = len(re.findall(pattern, low))
            if hits:
                scores[proj["name"]] = scores.get(proj["name"], 0) + hits
    if not scores:
        return None
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    return ranked[0][0] if len(ranked) == 1 or ranked[0][1] > ranked[1][1] else None


@dataclass
class Attribution:
    project: str
    method: str
    signals: dict
    related_projects: list


def _paths_in_text(text):
    windows = _ABS_PATH.findall(text or "")
    posix = re.findall(r"(?<![\w:/])/(?:[^\s\"'<>|*?`]+)", text or "")
    return {_norm(p.strip().rstrip(".,;:)")) for p in windows + posix}


def _fold_candidates(data):
    merged = {}
    originals = {p["name"]: p for p in data["projects"]}
    for proj in data["projects"]:
        for root in proj.get("roots", []) or [""]:
            canonical = _routing_cwd(root, data) if root else root
            name = proj["name"]
            if canonical != root:
                derived = _derive(canonical)
                name = _explicit(canonical, data) or (derived[0] if derived else title_case(Path(canonical).name))
            base = originals.get(name, proj)
            candidate = merged.setdefault(name, dict(base, name=name, roots=[], aliases=[]))
            candidate["roots"] = sorted(set(candidate["roots"]) | {r for r in (root, canonical) if r})
            aliases = set(candidate["aliases"]) | set(proj.get("aliases", [])) | set(base.get("aliases", []))
            if name != proj["name"]:
                aliases.add(proj["name"])
            candidate["aliases"] = sorted(aliases)
    return {**data, "projects": list(merged.values())}


def _repository_for_file(path, cache):
    directory = Path(path)
    if not directory.is_dir():
        directory = directory.parent
    visited = []
    repository = None
    for current in (directory, *directory.parents):
        key = _norm(current)
        if key in cache:
            repository = cache[key]
            break
        visited.append(key)
        marker = current / ".git"
        if marker.is_dir() or marker.is_file():
            repository = repository_root(str(current)) or None
            break
    for key in visited:
        cache[key] = repository
    return repository


def _repository_project(repository, data):
    target = _norm(repository)
    return next((p["name"] for p in data["projects"]
                 if any(_norm(root) == target for root in p.get("roots", []))), None)


def _path_project(path, data, cache):
    if not os.path.isabs(path):
        return None
    repository = _repository_for_file(path, cache)
    return _repository_project(repository, data) if repository else _explicit(path, data)


def _add_path_candidates(data, paths, cache):
    generated = {}
    for path in sorted(paths):
        if not os.path.isabs(path):
            continue
        repository = _repository_for_file(path, cache)
        if repository:
            if _repository_project(repository, data):
                continue
            directory = repository
        else:
            if is_generic(path) or _explicit(path, data):
                continue
            directory = path if os.path.isdir(path) else os.path.dirname(path)
        derived = _derive(repository or _routing_cwd(directory, data))
        if not derived or derived[0] == ENVIRONMENT or not Path(derived[1]).is_dir():
            continue
        name, root = derived
        candidate = next((p for p in data["projects"] if p["name"] == name), None)
        if candidate is None:
            candidate = {"name": name, "roots": [], "status": "active"}
            data["projects"].append(candidate)
        if root not in candidate["roots"]:
            candidate["roots"].append(root)
        generated[name] = root
    return generated


def attribute(cwd, map_path, files=(), text="", create=True):
    original = load_map(map_path)
    canonical = _routing_cwd(cwd, original)
    cwd_project = resolve(canonical, map_path, create=create) if canonical else UNSORTED
    original = load_map(map_path)
    data = _fold_candidates(original)
    edited = set()
    for path in files:
        path = os.path.expanduser(str(path))
        path = path if os.path.isabs(path) else os.path.join(cwd or "", path)
        edited.add(_norm(path))
    mentioned = _paths_in_text(text)
    repositories = {}
    generated = _add_path_candidates(data, edited | mentioned, repositories)
    file_owners = Counter(_path_project(path, data, repositories) for path in edited)
    path_owners = Counter(_path_project(path, data, repositories) for path in mentioned)
    known = {p["name"] for p in original["projects"]}
    pins = []
    for pin in original.get("pins", []):
        if pin.get("project") not in known:
            log(SimpleNamespace(log_path=map_path.parent / "logs" / "capture.jsonl"),
                "invalid-project-pin", reason="unknown project")
            continue
        if pin.get("root") and cwd and _is_under(_norm(cwd), _norm(pin["root"])):
            pins.append(pin)
    pinned = max(pins, key=lambda p: len(_norm(p["root"])))['project'] if pins else None
    signals = {}
    for proj in data["projects"]:
        name = proj["name"]
        count_files = min(10, file_owners[name])
        count_paths = path_owners[name]
        names = {name, *proj.get("aliases", []), *(Path(r).name for r in proj.get("roots", []))}
        names = {n.strip() for n in names if len(n.strip()) >= 4 and n.strip().lower() not in _STOP_NAMES}
        count_names = int(any(re.search(r"(?<![\w-])" + re.escape(n).replace(r"\ ", r"[\s_-]+").replace(r"\-", r"[\s_-]+") + r"(?![\w-])", text, re.I) for n in names))
        cwd_signal = int(name == cwd_project)
        signals[name] = dict(files=count_files, paths=count_paths, names=count_names, cwd=cwd_signal,
                             score=3 * count_files + 2 * count_paths + count_names + cwd_signal)
    ranked = sorted(signals, key=lambda name: (-signals[name]["score"], name))
    winner = ranked[0] if ranked else cwd_project
    first = signals[winner]["score"] if ranked else 0
    second = signals[ranked[1]]["score"] if len(ranked) > 1 else 0
    content = any(v["files"] or v["paths"] or v["names"] for v in signals.values())
    method = "content" if content and first > 0 and first >= 2 * second else "cwd-fallback"
    project = winner if method == "content" else cwd_project
    if pinned:
        project, method = pinned, "pinned"
    related = sorted(name for name, signal in signals.items() if name != project and signal["score"] >= 3)
    if create:
        for name in [project, *related]:
            if name in generated:
                resolve(generated[name], map_path, create=True)
    return Attribution(project, method, signals, related)


def resolve_session(cwd: str, map_path: Path, files=(), text: str = "", create: bool = True) -> str:
    return attribute(cwd, map_path, files, text, create).project


def note_path(vault: Path, project: str) -> Path:
    """Active projects live in 1-Projects; an archived project keeps its note in 4-Archive."""
    active = vault / "1-Projects" / project / f"{project}.md"
    archived = vault / "4-Archive" / project / f"{project}.md"
    return archived if archived.exists() and not active.exists() else active


def resolve(cwd: str, map_path: Path, create: bool = True) -> str:
    if not cwd:
        return UNSORTED
    data = load_map(map_path)
    cwd = _routing_cwd(cwd, data)
    target = _norm(cwd)
    best, best_len = None, -1
    for proj in data["projects"]:
        for root in proj.get("roots", []):
            r = _norm(root)
            if _is_under(target, r) and len(r) > best_len:
                best, best_len = proj["name"], len(r)
    if best:
        return best
    derived = _derive(cwd)
    if derived is None:
        return UNSORTED
    name, root = derived
    if _norm(root) in (_norm(HOME), _norm(PROJECTS_ROOT)):
        return name  # never persist a root that would prefix-match every directory below it
    if create:
        with file_lock(map_path.with_suffix(".lock"), timeout=10):
            data = load_map(map_path)
            existing = next((p for p in data["projects"] if p["name"].lower() == name.lower()), None)
            if existing:
                if root not in existing["roots"]:
                    existing["roots"].append(root)
            else:
                data["projects"].append({"name": name, "roots": [root], "status": "active"})
            atomic_write(map_path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return name
