#!/usr/bin/env python3
"""Vault hygiene sweep, dry-run plan and apply (WP-S1). Stdlib only."""
import argparse
import datetime
import json
import shutil
import subprocess
import os
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PARA_PREFIXES = ("0-", "1-", "2-", "3-", "4-")
PARA_KNOWN = {
    "0-Sessions", "1-Projects", "2-Areas", "3-Resources", "4-Archive",
    "_meta", "Projects", "Areas", "Resources", "Archive", "Sessions",
}
STATE_BEGIN = "<!-- agentmem:state:begin -->"
STATE_END = "<!-- agentmem:state:end -->"
PENDING_BEGIN = "<!-- agentmem:pending:begin -->"
PENDING_END = "<!-- agentmem:pending:end -->"
STALE_PREFIX = "(as of unknown commit) "
HEX7 = re.compile(r"\b[0-9a-fA-F]{7,}\b")
REPO_KW = ("repo", "repository", "head", "commit")
FRAG_SUFFIXES = ("-worktree", "-wt", "-copy", "-tmp", "-dev", "-feature")
GENERIC_WORDS = {"app", "project", "repo", "service", "lib", "tool", "test", "main"}
WIKI_LINK = re.compile(r"\[\[([^\]|#]+?)(?:[#|][^\]]*)?\]\]")


def _iter_md(root: Path):
    for dirpath, _, filenames in os.walk(root):
        for f in filenames:
            if f.endswith(".md"):
                yield Path(dirpath) / f


def _rel(p: Path, vault: Path) -> str:
    try:
        return p.resolve().relative_to(vault.resolve()).as_posix()
    except ValueError:
        return str(p)


def _read(p: Path) -> str | None:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _get_block(text: str, begin: str, end: str) -> str | None:
    i, j = text.find(begin), text.find(end)
    if i < 0 or j < i:
        return None
    return text[i + len(begin):j]


def _resolve_git_dir(git_dir: Path) -> Path | None:
    cd = git_dir / "commondir"
    if cd.is_file():
        raw = cd.read_text(encoding="utf-8").strip()
        return (git_dir / raw).resolve()
    return git_dir.resolve()


def _read_head(git_dir: Path) -> str | None:
    actual = _resolve_git_dir(git_dir)
    if not actual:
        return None
    head = actual / "HEAD"
    text = _read(head)
    if not text:
        return None
    text = text.strip()
    if text.startswith("ref: "):
        ref = actual / text[5:]
        return _read(ref).strip() if ref.is_file() else None
    return text if HEX7.match(text) else None


def _read_origin(git_dir: Path) -> str | None:
    actual = _resolve_git_dir(git_dir)
    if not actual:
        return None
    cfg = _read(actual / "config")
    if not cfg:
        return None
    in_origin = False
    for line in cfg.splitlines():
        s = line.strip()
        if s == '[remote "origin"]':
            in_origin = True
        elif s.startswith("["):
            in_origin = False
        elif in_origin and s.startswith("url"):
            _, _, v = s.partition("=")
            return v.strip()
    return None


def _worktree_parent_git_dir(git_file: Path) -> Path | None:
    raw = _read(git_file)
    if not raw or not raw.startswith("gitdir:"):
        return None
    gitdir = raw.split(":", 1)[1].strip()
    resolved = (git_file.parent / gitdir).resolve() if not os.path.isabs(gitdir) else Path(gitdir).resolve()
    admin_gitdir_file = resolved / "gitdir"
    if not admin_gitdir_file.is_file():
        return None
    back_ref = _read(admin_gitdir_file)
    if not back_ref:
        return None
    back_path = back_ref.strip()
    back_resolved = (resolved / back_path).resolve() if not os.path.isabs(back_path) else Path(back_path).resolve()
    if back_resolved != git_file.resolve():
        return None
    return _resolve_git_dir(resolved)


def _norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _normalize_remote_url(url: str, repo_root: Path) -> str:
    if not url:
        return ""
    if url.startswith("../") or url.startswith("./") or url.startswith("..\\") or url.startswith(".\\"):
        url = str((repo_root / url).resolve())
    url = url.replace("\\", "/")
    while "//" in url:
        url = url.replace("//", "/")
    if os.name == "nt":
        url = url.lower()
    url = url.rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    return url


def _words(name: str) -> set[str]:
    return {w for w in re.split(r"[-_\s]+", name.lower()) if w}


def _name_core(name: str) -> set[str]:
    w = _words(name)
    core = w - GENERIC_WORDS
    return core if core else w


def _base_name(name: str) -> str:
    low = name.lower()
    for s in FRAG_SUFFIXES:
        if low.endswith(s):
            return name[: len(name) - len(s)]
    return name


def _looks_derived(folder: str, candidate: str) -> bool:
    if folder == candidate:
        return False
    low_f = folder.lower()
    for s in FRAG_SUFFIXES:
        if low_f.endswith(s):
            return True
    wf = _words(folder)
    core_c = _name_core(candidate)
    if not core_c or not wf:
        return False
    return core_c < wf


def _scan_fragments(vault: Path, proj_folders: dict[str, Path]) -> list[dict]:
    worktree_frags = {}
    for name, path in proj_folders.items():
        git = path / ".git"
        if git.is_file():
            parent_gd = _worktree_parent_git_dir(git)
            if parent_gd:
                worktree_frags[name] = (path, parent_gd)

    remaining = {n: p for n, p in proj_folders.items() if n not in worktree_frags}
    established_non_frag = {n: p for n, p in remaining.items()}

    non_frag_origins = {}
    for n, p in established_non_frag.items():
        gd = p / ".git"
        if gd.is_dir():
            url = _read_origin(gd)
            if url:
                non_frag_origins.setdefault(url, []).append(n)

    name_frags = {}
    for name in sorted(remaining):
        for cand in sorted(established_non_frag):
            if cand == name:
                continue
            if _looks_derived(name, cand):
                name_frags[name] = cand
                break

    all_frags = {n: (proj_folders[n], None) for n in name_frags}
    all_frags.update({n: (p, gd) for n, (p, gd) in worktree_frags.items()})
    confirmed_non_frag = {n: p for n, p in remaining.items() if n not in name_frags}

    confirmed_origins = {}
    for n, p in confirmed_non_frag.items():
        gd = p / ".git"
        if gd.is_dir():
            url = _read_origin(gd)
            if url:
                confirmed_origins.setdefault(url, []).append(n)

    root_to_names: dict[str, list[str]] = {}
    for n, p in proj_folders.items():
        r = _resolved(p)
        root_to_names.setdefault(r, []).append(n)
    ambiguous_roots = {r for r, names in root_to_names.items() if len(names) > 1}

    root_to_name = {}
    for r, names in root_to_names.items():
        if len(names) == 1:
            root_to_name[r] = names[0]

    items = []
    for name in sorted(all_frags):
        path, parent_gd = all_frags[name]
        evidence, parent = None, None

        if parent_gd:
            frag_root = _resolved(path)
            if frag_root in ambiguous_roots:
                parent = None
                evidence = None
            else:
                parent = root_to_name.get(_resolved(parent_gd.parent))
                if parent and parent != name:
                    parent_root = _resolved(proj_folders[parent])
                    if frag_root == parent_root:
                        evidence = "guess"
                    elif (_resolved(parent_gd) == _resolved(proj_folders[parent] / ".git")
                          and _git_confirms_worktree(path, proj_folders[parent])):
                        evidence = "worktree"
                    else:
                        evidence = "guess"
                else:
                    parent = None

        if not parent and name in name_frags:
            cand = name_frags[name]
            frag_root = _resolved(path)
            cand_root = _resolved(proj_folders[cand])
            if frag_root in ambiguous_roots or cand_root in ambiguous_roots:
                evidence = "guess"
                parent = cand
            elif frag_root == cand_root:
                evidence = "guess"
                parent = cand
            else:
                own_url = _read_origin(path / ".git") if (path / ".git").is_dir() else None
                cand_url = _read_origin(proj_folders[cand] / ".git") if (proj_folders[cand] / ".git").is_dir() else None
                own_norm = _normalize_remote_url(own_url, path) if own_url else None
                cand_norm = _normalize_remote_url(cand_url, proj_folders[cand]) if cand_url else None
                evidence = "shared_remote" if own_norm and cand_norm and own_norm == cand_norm else "guess"
                parent = cand

        if not parent and not parent_gd:
            parent = _find_parent(name, None, confirmed_non_frag)
            if parent:
                evidence = "guess"

        if parent and evidence:
            # Remote equality cannot be proven from URLs alone, so only a registered worktree of the parent's own .git applies unreviewed.
            auto = evidence == "worktree"
            items.append({"folder": name, "proposed_parent": parent, "evidence": evidence, "auto_apply": auto})
    return items


def _git(args: list, cwd: Path) -> "str | None":
    try:
        out = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def _git_confirms_worktree(frag_root: Path, parent_root: Path) -> bool:
    """Git itself must list the fragment as a live worktree of the parent and resolve its HEAD and common dir."""
    listing = _git(["worktree", "list", "--porcelain"], parent_root)
    if listing is None:
        return False
    target, live = _resolved(frag_root), False
    for block in listing.strip().split("\n\n"):
        fields = dict((line.split(" ", 1) + [""])[:2] for line in block.splitlines())
        if "worktree" in fields and _resolved(Path(fields["worktree"])) == target:
            live = fields.get("HEAD", "").strip("0") != "" and "prunable" not in fields
    if not live or _git(["rev-parse", "--verify", "HEAD"], frag_root) is None:
        return False
    common = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], frag_root)
    return common is not None and _resolved(Path(common.strip())) == _resolved(parent_root / ".git")


def _resolved(p: Path) -> str:
    return os.path.normcase(str(p.resolve()))


def _find_parent(frag_name: str, parent_git_dir: Path | None, candidates: dict[str, Path]) -> str | None:
    if parent_git_dir:
        cur = parent_git_dir
        vault_1p = cur.parent
        while cur != vault_1p and cur != cur.parent:
            if (cur / ".git").is_dir():
                n = cur.name
                if n in candidates:
                    return n
            cur = cur.parent
    wf = _words(frag_name)
    exact = next((n for n in sorted(candidates) if _name_core(n) == wf), None)
    if exact:
        return exact
    for n in sorted(candidates):
        core = _name_core(n)
        if core and core < wf:
            return n
    return None


def _scan_leftovers(vault: Path) -> list[dict]:
    items = []
    for entry in sorted(vault.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith("."):
            continue
        if entry.name.startswith("_"):
            continue
        if any(entry.name.startswith(p) for p in PARA_PREFIXES):
            continue
        if entry.name in PARA_KNOWN:
            continue
        items.append({"folder": entry.name, "auto_apply": True})
    return items


def _scan_stale(vault: Path, proj_folders: dict[str, Path]) -> list[dict]:
    items = []
    proj_dir = vault / "1-Projects"
    if not proj_dir.is_dir():
        return items
    for note in sorted(_iter_md(proj_dir)):
        text = _read(note)
        if not text:
            continue
        block = _get_block(text, STATE_BEGIN, STATE_END)
        if not block:
            continue
        proj_name = note.parent.name
        cur = None
        if proj_name in proj_folders:
            gd = proj_folders[proj_name] / ".git"
            if gd.is_dir():
                cur = _read_head(gd)
        for line in block.splitlines():
            s = line.strip()
            if not s:
                continue
            if not HEX7.search(s):
                continue
            if any(kw in s.lower() for kw in REPO_KW):
                items.append({
                    "note": _rel(note, vault),
                    "line": s,
                    "current_head": cur,
                    "stale": cur is not None and cur[:7] not in s,
                    "auto_apply": True,
                })
    return items


def _scan_broken(vault: Path) -> list[dict]:
    items = []
    for md in sorted(_iter_md(vault)):
        text = _read(md)
        if not text:
            continue
        src = _rel(md, vault)
        seen = set()
        for target in WIKI_LINK.findall(text):
            if target in seen:
                continue
            seen.add(target)
            full = vault / target.replace("/", os.sep)
            if not full.exists() and not (full.with_suffix(".md")).exists():
                items.append({"source": src, "target": target})
    return items


def _scan_unsorted(vault: Path) -> list[dict]:
    items = []
    sess_dir = vault / "0-Sessions"
    if not sess_dir.is_dir():
        return items
    for md in sorted(_iter_md(sess_dir)):
        text = _read(md)
        if not text or not text.startswith("---"):
            continue
        end = text.find("---", 3)
        if end < 0:
            continue
        fm = text[: end + 3]
        m = re.search(r'(?m)^project:\s*"?([^"\r\n]+?)"?\s*$', fm)
        if not m:
            items.append({"session": _rel(md, vault), "suggested_project": None})
            continue
        proj = m.group(1).strip()
        if proj in ("Unsorted", ""):
            items.append({"session": _rel(md, vault), "suggested_project": None})
    return items


def _parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}
    fields = {}
    for line in text[4:end].splitlines():
        if not line or line[0].isspace() or ": " not in line:
            continue
        key, value = line.split(": ", 1)
        key = key.strip()
        value = value.strip()
        if value.startswith('"') and value.endswith('"') and len(value) >= 2:
            value = value[1:-1]
        fields[key] = value
    return fields


def _extract_artifact_paths(text: str) -> list[str]:
    match = re.search(r"(?ms)^## Artifacts and modified files\n\n(.*?)(?=\n## |\Z)", text)
    if not match:
        return []
    paths = []
    for line in match.group(1).splitlines():
        m = re.match(r"^- `(.+?)`$", line.strip())
        if m and m.group(1) not in ("none recorded", "none"):
            paths.append(m.group(1))
    return paths


def _extract_note_text(text: str) -> str:
    parts = []
    title = re.search(r"(?m)^# (.+)$", text)
    if title:
        parts.append(title.group(1))
    cmds = re.search(r"(?ms)^## Commands run\n\n(.*?)(?=\n## |\Z)", text)
    if cmds:
        for line in cmds.group(1).splitlines():
            m = re.match(r"^- `(.+?)`$", line.strip())
            if m and m.group(1) != "none":
                parts.append(m.group(1))
    return " ".join(parts)


def _scan_re_attribution(vault: Path) -> dict:
    from agentmem.projects import attribute
    map_path = vault / "_meta" / "projects.json"
    sess_dir = vault / "0-Sessions"
    if not map_path.exists() or not sess_dir.is_dir():
        return {"items": [], "calibration_sample": [],
                "note": "Re-scored using A-5 content-based attribution. auto_apply is false for all items."}
    items = []
    for md in sorted(_iter_md(sess_dir)):
        text = _read(md)
        if not text:
            continue
        fm = _parse_frontmatter(text)
        if fm.get("type") != "session":
            continue
        filed = fm.get("project", "").strip()
        if not filed:
            continue
        files = _extract_artifact_paths(text)
        note_text = _extract_note_text(text)
        try:
            result = attribute(cwd="", map_path=map_path, files=files, text=note_text, create=False)
        except Exception:
            continue
        if result.method != "content":
            continue
        if result.project == filed:
            continue
        items.append({
            "note": _rel(md, vault),
            "filed_project": filed,
            "proposed_project": result.project,
            "method": result.method,
            "signals": result.signals,
            "auto_apply": False,
        })
    rng = random.Random(1701)
    k = min(30, len(items))
    calibration = rng.sample(items, k) if k else []
    return {
        "items": items,
        "calibration_sample": calibration,
        "note": ("Re-scored using A-5 content-based attribution. "
                 "auto_apply is false for all items. "
                 "calibration_sample uses seed 1701; user must accept before auto-re-attribution."),
    }


def _repo_root(vault: Path, project: str) -> Path | None:
    """The project's first existing repository root from projects.json; repos live outside the vault."""
    try:
        mapping = json.loads((vault / "_meta" / "projects.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for entry in mapping.get("projects", []):
        if entry.get("name") == project:
            for root in entry.get("roots", []):
                if (Path(root) / ".git").exists():
                    return Path(root)
    return None


def build_plan(vault: Path) -> dict:
    proj_dir = vault / "1-Projects"
    proj_folders = {}
    if proj_dir.is_dir():
        for d in sorted(proj_dir.iterdir()):
            if d.is_dir() and not d.name.startswith("."):
                proj_folders[d.name] = d
    repo_dirs = {name: _repo_root(vault, name) or path for name, path in proj_folders.items()}
    return {
        "fragments": _scan_fragments(vault, repo_dirs),
        "leftover_folders": _scan_leftovers(vault),
        "stale_state_lines": _scan_stale(vault, repo_dirs),
        "broken_links": _scan_broken(vault),
        "unsorted_sessions": _scan_unsorted(vault),
        "re_attribution": _scan_re_attribution(vault),
    }


def _archive_dest(base: Path, name: str) -> Path:
    target = base / name
    if not target.exists():
        return target
    i = 2
    while True:
        candidate = base / f"{name}-{i}"
        if not candidate.exists():
            return candidate
        i += 1


def _block_text(text: str, begin: str, end: str) -> str:
    i, j = text.find(begin), text.find(end)
    if i < 0 or j < i:
        return ""
    return text[i + len(begin):j].strip()


def _append_to_pending(note: Path, heading: str, content: str) -> None:
    text = note.read_text(encoding="utf-8")
    i = text.find(PENDING_BEGIN)
    j = text.find(PENDING_END)
    if i < 0 or j < i:
        raise ValueError(f"no pending block in {note}")
    existing = text[i + len(PENDING_BEGIN):j]
    insertion = f"\n{heading}\n\n{content}\n"
    if existing.strip():
        new_text = text[:i + len(PENDING_BEGIN)] + existing.rstrip("\n") + "\n" + insertion + text[j:]
    else:
        new_text = text[:i + len(PENDING_BEGIN)] + insertion + text[j:]
    note.write_text(new_text, encoding="utf-8")


def _find_project_entry(data: dict, name: str, folder_path: Path | None = None) -> dict | None:
    for entry in data.get("projects", []):
        if entry.get("name") == name:
            return entry
    if folder_path is not None:
        folder_norm = os.path.normcase(str(folder_path.resolve()))
        for entry in data.get("projects", []):
            for root in entry.get("roots", []):
                if os.path.normcase(str(Path(root).resolve())) == folder_norm:
                    return entry
    return None


def _update_projects_json(vault: Path, frag_folder: str, parent_folder: str) -> None:
    pjson = vault / "_meta" / "projects.json"
    if not pjson.exists():
        return
    data = json.loads(pjson.read_text(encoding="utf-8"))
    frag_dir = vault / "1-Projects" / frag_folder
    parent_dir = vault / "1-Projects" / parent_folder
    frag_entry = _find_project_entry(data, frag_folder, frag_dir)
    parent_entry = _find_project_entry(data, parent_folder, parent_dir)
    if frag_entry and parent_entry:
        frag_roots = frag_entry.get("roots", [])
        parent_roots = parent_entry.setdefault("roots", [])
        for r in frag_roots:
            if r not in parent_roots:
                parent_roots.append(r)
        data["projects"] = [e for e in data["projects"] if e is not frag_entry]
    elif frag_entry and not parent_entry:
        data["projects"] = [e for e in data["projects"] if e is not frag_entry]
    pjson.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _apply_fragment(vault: Path, item: dict, date_str: str) -> dict:
    folder = item["folder"]
    parent = item["proposed_parent"]
    entry = {"action": "fragment_merge", "fragment": folder, "parent": parent}
    frag_dir = vault / "1-Projects" / folder
    if not frag_dir.exists():
        entry["status"] = "skip"
        entry["reason"] = "already merged"
        return entry
    parent_dir = vault / "1-Projects" / parent
    if not parent_dir.exists():
        entry["status"] = "error"
        entry["reason"] = "parent folder not found"
        return entry
    state_content = ""
    pending_content = ""
    frag_note = frag_dir / f"{folder}.md"
    if frag_note.is_file():
        try:
            frag_text = frag_note.read_text(encoding="utf-8")
            state_content = _block_text(frag_text, STATE_BEGIN, STATE_END)
            pending_content = _block_text(frag_text, PENDING_BEGIN, PENDING_END)
        except OSError as e:
            entry["status"] = "error"
            entry["reason"] = str(e)
            return entry
    dest = _archive_dest(vault / "4-Archive" / "merged", folder)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(frag_dir), str(dest))
    except OSError as e:
        entry["status"] = "error"
        entry["reason"] = f"move failed: {e}"
        return entry
    parent_note = parent_dir / f"{parent}.md"
    if parent_note.is_file() and (state_content or pending_content):
        heading = f"### Merged from {folder} ({date_str})"
        parts = [p for p in (state_content, pending_content) if p]
        try:
            _append_to_pending(parent_note, heading, "\n\n".join(parts))
        except (OSError, ValueError) as e:
            entry["status"] = "error"
            entry["reason"] = f"pending append failed: {e}"
            return entry
    try:
        _update_projects_json(vault, folder, parent)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        entry["status"] = "error"
        entry["reason"] = f"projects.json update failed: {e}"
        return entry
    entry["status"] = "ok"
    entry["dest"] = _rel(dest, vault)
    return entry


def _apply_leftover(vault: Path, item: dict) -> dict:
    folder = item["folder"]
    entry = {"action": "leftover_archive", "folder": folder}
    src = vault / folder
    if not src.exists():
        entry["status"] = "skip"
        entry["reason"] = "already archived"
        return entry
    dest = _archive_dest(vault / "4-Archive", folder)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
    except OSError as e:
        entry["status"] = "error"
        entry["reason"] = str(e)
        return entry
    entry["status"] = "ok"
    entry["dest"] = _rel(dest, vault)
    return entry


def _apply_stale_line(vault: Path, item: dict) -> dict:
    note_rel = item["note"]
    target_line = item["line"]
    entry = {"action": "stale_line", "note": note_rel, "line": target_line}
    note_path = vault / note_rel.replace("/", os.sep)
    if not note_path.is_file():
        entry["status"] = "skip"
        entry["reason"] = "file not found"
        return entry
    try:
        text = note_path.read_text(encoding="utf-8")
    except OSError as e:
        entry["status"] = "error"
        entry["reason"] = str(e)
        return entry
    lines = text.splitlines(keepends=True)
    found = False
    for idx, raw in enumerate(lines):
        if raw.strip() == target_line:
            if raw.lstrip().startswith(STALE_PREFIX):
                entry["status"] = "skip"
                entry["reason"] = "already prefixed"
                return entry
            indent = raw[: len(raw) - len(raw.lstrip())]
            new_line = indent + STALE_PREFIX + raw.lstrip()
            if not new_line.endswith("\n"):
                if raw.endswith("\n"):
                    new_line += "\n"
            lines[idx] = new_line
            found = True
            break
    if not found:
        entry["status"] = "skip"
        entry["reason"] = "line not found"
        return entry
    try:
        note_path.write_text("".join(lines), encoding="utf-8")
    except OSError as e:
        entry["status"] = "error"
        entry["reason"] = str(e)
        return entry
    entry["status"] = "ok"
    return entry


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _today_str() -> str:
    return datetime.date.today().isoformat()


def _count_files(path: Path) -> int:
    n = 0
    for _, _, files in os.walk(path):
        n += len(files)
    return n


def apply_plan(vault: Path, plan: dict, only_auto: bool = False, dry_run: bool = False) -> dict:
    vault = vault.resolve()
    if not (vault / ".git").exists():
        return {"error": "vault is not a git repository", "ok": False}
    pause_path = vault / "_meta" / "PAUSE"
    pause_existed = pause_path.exists()
    date_str = _today_str()
    actions = []
    for item in plan.get("fragments", []):
        if only_auto and not item.get("auto_apply"):
            continue
        actions.append(("fragment", item))
    for item in plan.get("leftover_folders", []):
        if only_auto and not item.get("auto_apply"):
            continue
        actions.append(("leftover", item))
    for item in plan.get("stale_state_lines", []):
        if only_auto and not item.get("auto_apply"):
            continue
        if not item.get("stale", False):
            continue
        actions.append(("stale", item))
    if dry_run:
        print("Dry run — no changes written.")
        if not pause_existed:
            print(f"  Would write {pause_path}")
        print(f"  Would commit snapshot (sweep snapshot {date_str})")
        for kind, item in actions:
            if kind == "fragment":
                print(f"  Would merge fragment {item['folder']} -> {item['proposed_parent']}")
            elif kind == "leftover":
                print(f"  Would archive leftover folder {item['folder']}")
            elif kind == "stale":
                print(f"  Would prefix stale line in {item['note']}: {item['line']}")
        log_path = vault / "_meta" / "logs" / f"sweep-{date_str}.jsonl"
        print(f"  Would write apply log {log_path}")
        if not pause_existed:
            print(f"  Would remove {pause_path}")
        return {"ok": True, "actions": len(actions), "dry_run": True}
    if not pause_existed:
        pause_path.parent.mkdir(parents=True, exist_ok=True)
        pause_path.write_text("", encoding="utf-8")
    _git(["add", "-A"], vault)
    commit_result = _git(["commit", "-q", "--allow-empty", "-m", f"sweep snapshot {date_str}"], vault)
    if commit_result is None:
        if not pause_existed and pause_path.exists():
            pause_path.unlink(missing_ok=True)
        return {"error": "snapshot commit failed", "ok": False}
    sha = _git(["rev-parse", "HEAD"], vault)
    if sha is None:
        return {"error": "could not read snapshot SHA", "ok": False}
    sha = sha.strip()
    print(f"sweep snapshot {date_str}: {sha}")
    log_entries = [{"ts": _now_iso(), "action": "snapshot", "sha": sha, "status": "ok"}]
    for kind, item in actions:
        if kind == "fragment":
            result = _apply_fragment(vault, item, date_str)
        elif kind == "leftover":
            result = _apply_leftover(vault, item)
        elif kind == "stale":
            result = _apply_stale_line(vault, item)
        else:
            continue
        result["ts"] = _now_iso()
        log_entries.append(result)
    log_path = vault / "_meta" / "logs" / f"sweep-{date_str}.jsonl"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as f:
            for entry in log_entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"warning: could not write apply log: {e}", file=sys.stderr)
    if not pause_existed:
        pause_path.unlink(missing_ok=True)
    return {"ok": True, "sha": sha, "actions": len(actions), "log_entries": log_entries}


def main():
    ap = argparse.ArgumentParser(prog="vault_sweep")
    sub = ap.add_subparsers(dest="command")
    pl = sub.add_parser("plan", help="Dry-run vault hygiene plan")
    pl.add_argument("--vault", required=True)
    pl.add_argument("--out", required=True)
    aply = sub.add_parser("apply", help="Apply an approved sweep plan")
    aply.add_argument("--vault", required=True)
    aply.add_argument("--plan", required=True)
    aply.add_argument("--only-auto", action="store_true")
    aply.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.command == "plan":
        vault = Path(args.vault).resolve()
        out = Path(args.out).resolve()
        if out.is_relative_to(vault):
            ap.error(f"--out must be outside the vault, got {out}")
        plan = build_plan(vault)
        plan["vault_path"] = str(vault)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return 0
    if args.command == "apply":
        vault = Path(args.vault).resolve()
        plan_path = Path(args.plan).resolve()
        if plan_path.is_relative_to(vault):
            ap.error(f"--plan must be outside the vault, got {plan_path}")
        if not plan_path.is_file():
            ap.error(f"plan file not found: {plan_path}")
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            ap.error(f"cannot read plan: {e}")
        plan_vault = plan.get("vault_path", "")
        if not plan_vault:
            ap.error("plan has no vault_path; regenerate with 'vault_sweep plan'")
        if os.path.normcase(str(Path(plan_vault).resolve())) != os.path.normcase(str(vault)):
            ap.error(f"plan was produced for a different vault: {plan_vault}")
        result = apply_plan(vault, plan, only_auto=args.only_auto, dry_run=args.dry_run)
        if not result.get("ok"):
            print(f"error: {result.get('error', 'unknown')}", file=sys.stderr)
            return 1
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
