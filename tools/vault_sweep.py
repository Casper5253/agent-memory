#!/usr/bin/env python3
"""Vault hygiene sweep, dry-run plan (WP-S1). Stdlib only; reads a vault, writes a plan."""
import argparse
import json
import os
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
    return _resolve_git_dir(resolved)


def _norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


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

    all_frags = {n: (p, None) for n, p in name_frags.items()}
    all_frags.update({n: (p, gd) for n, (p, gd) in worktree_frags.items()})
    confirmed_non_frag = {n: p for n, p in remaining.items() if n not in name_frags}

    confirmed_origins = {}
    for n, p in confirmed_non_frag.items():
        gd = p / ".git"
        if gd.is_dir():
            url = _read_origin(gd)
            if url:
                confirmed_origins.setdefault(url, []).append(n)

    items = []
    for name in sorted(all_frags):
        path, parent_gd = all_frags[name]
        evidence, parent = None, None

        if parent_gd:
            evidence = "worktree"
            parent_url = _read_origin(parent_gd)
            if parent_url and parent_url in confirmed_origins:
                candidates = confirmed_origins[parent_url]
                parent = candidates[0] if len(candidates) == 1 else next(
                    (c for c in candidates if _name_core(c) < _words(name)), candidates[0]
                )
            if not parent:
                parent = _find_parent(name, parent_gd, confirmed_non_frag)
                if parent:
                    evidence = "shared_remote"

        if not parent and name in name_frags:
            evidence, parent = "guess", name_frags[name]

        if not parent and not parent_gd:
            parent = _find_parent(name, None, confirmed_non_frag)
            if parent:
                evidence = "guess"

        if parent and evidence:
            auto = evidence in ("worktree", "shared_remote")
            items.append({"folder": name, "proposed_parent": parent, "evidence": evidence, "auto_apply": auto})
    return items


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


def build_plan(vault: Path) -> dict:
    proj_dir = vault / "1-Projects"
    proj_folders = {}
    if proj_dir.is_dir():
        for d in sorted(proj_dir.iterdir()):
            if d.is_dir() and not d.name.startswith("."):
                proj_folders[d.name] = d
    return {
        "fragments": _scan_fragments(vault, proj_folders),
        "leftover_folders": _scan_leftovers(vault),
        "stale_state_lines": _scan_stale(vault, proj_folders),
        "broken_links": _scan_broken(vault),
        "unsorted_sessions": _scan_unsorted(vault),
        "re_attribution": {
            "items": [],
            "note": "TODO: requires WP-01 A-5 content-based scoring, not yet merged.",
        },
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
    plan = build_plan(vault)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
