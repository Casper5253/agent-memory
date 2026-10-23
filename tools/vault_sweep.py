#!/usr/bin/env python3
"""Vault hygiene sweep, dry-run plan (WP-S1). Stdlib only; reads a vault, writes a plan."""
import argparse
import json
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
                    elif _resolved(parent_gd) == _resolved(proj_folders[parent] / ".git"):
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


def main():
    ap = argparse.ArgumentParser(prog="vault_sweep")
    sub = ap.add_subparsers(dest="command")
    pl = sub.add_parser("plan", help="Dry-run vault hygiene plan")
    pl.add_argument("--vault", required=True)
    pl.add_argument("--out", required=True)
    args = ap.parse_args()
    if args.command != "plan":
        ap.print_help()
        return 1
    vault = Path(args.vault).resolve()
    out = Path(args.out).resolve()
    if out.is_relative_to(vault):
        ap.error(f"--out must be outside the vault, got {out}")
    plan = build_plan(vault)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
