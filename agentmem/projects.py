"""Map a working directory to a project, deterministically. Never derived from prompt text.

_meta/projects.json: {"projects": [{"name": "...", "roots": ["C:\\..."], "status": "active|archived"}]}
Longest matching root wins. Unknown directories under ~/Documents/Projects get a project named after the
nearest git root (or first folder level); anything else is "Unsorted" (captured, never consolidated).
"""
import json
import os
import re
from pathlib import Path

from .fsutil import atomic_write, file_lock

HOME = Path.home()
PROJECTS_ROOT = HOME / "Documents" / "Projects"
ENVIRONMENT = "Agent Environment"
UNSORTED = "Unsorted"


def _norm(p: str) -> str:
    p = os.path.normpath(os.path.expanduser(str(p)))
    return p.lower() if os.name == "nt" else p


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
    if n in (_norm(HOME), _norm(PROJECTS_ROOT)):  # sessions at the top level are about the environment itself
        return ENVIRONMENT, os.path.normpath(cwd)
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


def resolve(cwd: str, map_path: Path, create: bool = True) -> str:
    if not cwd:
        return UNSORTED
    target = _norm(cwd)
    data = load_map(map_path)
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
