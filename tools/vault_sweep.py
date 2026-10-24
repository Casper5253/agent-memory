#!/usr/bin/env python3
"""Vault hygiene sweep, dry-run plan and apply (WP-S1). Stdlib only."""
import argparse
import datetime
import hashlib
import json
import shutil
import subprocess
import os
import random
import re
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentmem.fsutil import atomic_write
from agentmem.redact import findings as sensitive_findings

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
STALE_REF = re.compile(
    r"(?:(?P<ref>[A-Za-z0-9_][A-Za-z0-9_./-]*)\s+at\s+(?P<sha>[0-9a-f]{7,})"
    r"|HEAD\s+(?P<head_sha>[0-9a-f]{7,}))",
    re.IGNORECASE,
)
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


def _is_inside_vault_path(p: Path, vault: Path) -> bool:
    try:
        p.resolve().relative_to(vault.resolve())
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _read(p: Path) -> str | None:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write bytes via a sibling temp file and os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _block_bytes(data: bytes, begin: bytes, end: bytes) -> bytes:
    i, j = data.find(begin), data.find(end)
    if i < 0 or j < i:
        return b""
    return data[i + len(begin):j]


def _git_run(args: list, cwd: Path, check: bool = True) -> subprocess.CompletedProcess | None:
    """Run git with CRLF disabled so line endings are preserved exactly."""
    cmd = ["git", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false", "-C", str(cwd), *args]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        if check:
            raise RuntimeError(f"git failed: {e}") from e
        return None
    if check and result.returncode != 0:
        msg = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"git {' '.join(args)} failed (rc={result.returncode}): {msg}")
    return result


def _embedded_repos(vault: Path) -> list[str]:
    """Return .git dirs/files inside the vault that are not the vault's own .git
    or a .git inside a direct project folder under 1-Projects/4-Archive.
    """
    bad = []
    vault_git = vault / ".git"
    try:
        vault_git_resolved = vault_git.resolve()
    except OSError:
        vault_git_resolved = vault_git
    for root, dirs, files in os.walk(vault):
        if ".git" in dirs:
            git_path = Path(root) / ".git"
            rel = Path(root).relative_to(vault)
            allowed = len(rel.parts) >= 1 and rel.parts[0] in ("1-Projects", "4-Archive")
            try:
                is_vault_git = git_path.resolve() == vault_git_resolved
            except OSError:
                is_vault_git = False
            if not is_vault_git and not allowed:
                bad.append(str(git_path))
            dirs.remove(".git")
        elif ".git" in files:
            git_file = Path(root) / ".git"
            rel = git_file.relative_to(vault)
            allowed = len(rel.parts) >= 2 and rel.parts[0] in ("1-Projects", "4-Archive")
            if not allowed:
                bad.append(str(git_file))
    return bad


def _snapshot(vault: Path, date_str: str) -> str:
    """Commit a snapshot, excluding _meta/PAUSE, and verify the tree is clean afterwards."""
    emb = _embedded_repos(vault)
    if emb:
        raise RuntimeError(f"embedded repository in vault: {emb[0]}")
    head = _git_run(["rev-parse", "--verify", "HEAD"], vault, check=False)
    if head is not None and head.returncode == 0:
        _git_run(["read-tree", "HEAD"], vault, check=True)
    _git_run(["add", "-A", "--", ":(exclude)_meta/PAUSE"], vault, check=True)
    diff = _git_run(["diff", "--cached", "--stat"], vault, check=True)
    if diff.stdout.strip():
        _git_run(["commit", "-q", "-m", f"sweep snapshot {date_str}"], vault, check=True)
    else:
        _git_run(["commit", "-q", "--allow-empty", "-m", f"sweep snapshot {date_str}"], vault, check=True)
    porcelain = _git_run(["status", "--porcelain", "--ignore-submodules=all", "--", ":(exclude)_meta/PAUSE"], vault, check=True).stdout.splitlines()
    if porcelain:
        raise RuntimeError(f"snapshot left working tree dirty: {porcelain}")
    sha = _git_run(["rev-parse", "HEAD"], vault, check=True).stdout.strip()
    return sha


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
        repo = proj_folders.get(proj_name)
        current_head = _git(["rev-parse", "--verify", "HEAD^{commit}"], repo) if repo else None
        cur = current_head.strip() if current_head else None
        for line_index, line in enumerate(block.splitlines()):
            s = line.strip()
            searchable = re.sub(r"\[\[[^\]]*\]\]", "", s)
            match = STALE_REF.search(searchable)
            if not match:
                continue
            ref = match.group("ref") or "HEAD"
            sha = match.group("sha") or match.group("head_sha")
            resolved_ref = _git(["rev-parse", "--verify", f"{ref}^{{commit}}"], repo) if repo else None
            resolved_sha = _git(["rev-parse", "--verify", f"{sha}^{{commit}}"], repo) if repo else None
            unresolved = not resolved_ref or not resolved_sha
            items.append({
                "note": _rel(note, vault),
                "line": line,
                "line_index": line_index,
                "current_head": cur,
                "stale": not unresolved and resolved_ref.strip() != resolved_sha.strip(),
                "auto_apply": not unresolved,
                **({"reason": "unresolved"} if unresolved else {}),
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
        # Legacy notes list files inside the session folder relative to it, and that folder was the filed project's root.
        filed_root = _repo_root(vault, filed) or _first_root(vault, filed)
        files = [f if os.path.isabs(f) or not filed_root else str(filed_root / f) for f in _extract_artifact_paths(text)]
        try:
            result = attribute(cwd=str(filed_root) if filed_root else "", map_path=map_path, files=files,
                               text="", create=False)
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


def _first_root(vault: Path, project: str) -> Path | None:
    try:
        mapping = json.loads((vault / "_meta" / "projects.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for entry in mapping.get("projects", []):
        if entry.get("name") == project and entry.get("roots"):
            return Path(entry["roots"][0])
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


def privacy_scan(vault: Path) -> dict:
    categories = ("secret", "iban", "payment_card", "bsn", "phone", "email")
    totals = {category: 0 for category in categories}
    matches = []
    for note in sorted(_iter_md(vault)):
        try:
            text = note.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for finding in sensitive_findings(text):
            totals[finding.category] += 1
            matches.append({
                "category": finding.category,
                "path": _rel(note, vault),
                "line": text.count("\n", 0, finding.start) + 1,
            })
    return {"findings": matches, "totals_by_category": totals}


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
    data = note.read_bytes()
    begin_b = PENDING_BEGIN.encode("utf-8")
    end_b = PENDING_END.encode("utf-8")
    insertion_b = f"\n{heading}\n\n{content}\n".encode("utf-8")
    i = data.find(begin_b)
    j = data.find(end_b)
    if i < 0 or j < i:
        if data and not data.endswith(b"\n"):
            data += b"\n"
        data += b"\n## Pending\n\n" + begin_b + insertion_b + end_b + b"\n"
    else:
        existing = data[i + len(begin_b):j]
        merge_marker = heading.partition(" (")[0].encode("utf-8") + b" ("
        if any(line.startswith(merge_marker) for line in existing.splitlines()):
            return
        if existing.strip():
            new_data = data[:i + len(begin_b)] + existing.rstrip(b"\n") + b"\n" + insertion_b + data[j:]
        else:
            new_data = data[:i + len(begin_b)] + insertion_b + data[j:]
        data = new_data
    _atomic_write_bytes(note, data)


def _make_parent_dirs(path: Path) -> list[Path]:
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    created = []
    for directory in reversed(missing):
        directory.mkdir()
        created.append(directory)
    return created


def _remove_empty_dirs(paths: list[Path]) -> None:
    for path in reversed(paths):
        try:
            path.rmdir()
        except OSError:
            pass


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
    # Match parent case-insensitively, consistent with agentmem/projects.py.
    parent_entry = None
    for entry in data.get("projects", []):
        if entry.get("name", "").lower() == parent_folder.lower():
            parent_entry = entry
            break
    if parent_entry is None:
        parent_norm = os.path.normcase(str(parent_dir.resolve()))
        for entry in data.get("projects", []):
            for root in entry.get("roots", []):
                if os.path.normcase(str(Path(root).resolve())) == parent_norm:
                    parent_entry = entry
                    break
            if parent_entry:
                break
    if parent_entry is None:
        raise ValueError(f"parent project not found in projects.json: {parent_folder}")
    if frag_entry and parent_entry:
        frag_roots = frag_entry.get("roots", [])
        parent_roots = parent_entry.setdefault("roots", [])
        for r in frag_roots:
            if r not in parent_roots:
                parent_roots.append(r)
        frag_aliases = frag_entry.get("aliases", [])
        parent_aliases = parent_entry.setdefault("aliases", [])
        for a in frag_aliases:
            if a not in parent_aliases:
                parent_aliases.append(a)
        data["projects"] = [e for e in data["projects"] if e is not frag_entry]
    atomic_write(pjson, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


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
    parent_note = parent_dir / f"{parent}.md"
    if not parent_note.is_file():
        entry["status"] = "error"
        entry["reason"] = "parent note not found"
        return entry
    state_content = b""
    pending_content = b""
    frag_note = frag_dir / f"{folder}.md"
    if frag_note.is_file():
        try:
            frag_bytes = frag_note.read_bytes()
            state_content = _block_bytes(frag_bytes, STATE_BEGIN.encode("utf-8"), STATE_END.encode("utf-8")).strip()
            pending_content = _block_bytes(frag_bytes, PENDING_BEGIN.encode("utf-8"), PENDING_END.encode("utf-8")).strip()
        except OSError as e:
            entry["status"] = "error"
            entry["reason"] = str(e)
            return entry
    pjson = vault / "_meta" / "projects.json"
    try:
        pjson_before = pjson.read_bytes() if pjson.is_file() else None
        parent_note_before = parent_note.read_bytes()
    except OSError as e:
        entry["status"] = "error"
        entry["reason"] = f"could not read merge inputs: {e}"
        return entry
    dest = _archive_dest(vault / "4-Archive" / "merged", folder)
    created_dirs = _make_parent_dirs(dest.parent)
    try:
        os.rename(frag_dir, dest)
    except OSError as e:
        _remove_empty_dirs(created_dirs)
        entry["status"] = "error"
        entry["reason"] = f"move failed: {e}"
        return entry
    try:
        _update_projects_json(vault, folder, parent)
        if state_content or pending_content:
            heading = f"### Merged from {folder} ({date_str})"
            parts = [p.decode("utf-8", errors="replace") for p in (state_content, pending_content) if p]
            _append_to_pending(parent_note, heading, "\n\n".join(parts))
    except (OSError, ValueError, json.JSONDecodeError) as e:
        rollback_errors = []
        try:
            if pjson_before is None:
                pjson.unlink(missing_ok=True)
            else:
                _atomic_write_bytes(pjson, pjson_before)
        except OSError as rollback_error:
            rollback_errors.append(f"projects.json rollback failed: {rollback_error}")
        try:
            _atomic_write_bytes(parent_note, parent_note_before)
        except OSError as rollback_error:
            rollback_errors.append(f"parent note rollback failed: {rollback_error}")
        try:
            os.rename(dest, frag_dir)
            _remove_empty_dirs(created_dirs)
        except OSError as rollback_error:
            rollback_errors.append(f"folder rollback failed: {rollback_error}")
        entry["status"] = "error"
        entry["reason"] = f"merge update failed: {e}" + ("; " + "; ".join(rollback_errors) if rollback_errors else "")
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
    created_dirs = _make_parent_dirs(dest.parent)
    try:
        os.rename(src, dest)
    except OSError as e:
        _remove_empty_dirs(created_dirs)
        entry["status"] = "error"
        entry["reason"] = f"move failed: {e}"
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
        data = note_path.read_bytes()
    except OSError as e:
        entry["status"] = "error"
        entry["reason"] = str(e)
        return entry
    begin_b = STATE_BEGIN.encode("utf-8")
    end_b = STATE_END.encode("utf-8")
    prefix_b = STALE_PREFIX.encode("utf-8")
    target_b = target_line.encode("utf-8")
    line_index = item.get("line_index")
    if not isinstance(line_index, int) or isinstance(line_index, bool) or line_index < 0:
        entry["status"] = "error"
        entry["reason"] = "missing or invalid state-block line index"
        return entry
    i = data.find(begin_b)
    j = data.find(end_b)
    if i < 0 or j < i:
        entry["status"] = "error"
        entry["reason"] = "state block not found"
        return entry
    block = data[i + len(begin_b):j]
    lines = block.splitlines(keepends=True)
    if line_index >= len(lines):
        entry["status"] = "skip"
        entry["reason"] = "line index outside state block"
        return entry
    raw = lines[line_index]
    body = raw[:-2] if raw.endswith(b"\r\n") else raw[:-1] if raw.endswith((b"\n", b"\r")) else raw
    indent_b = target_b[: len(target_b) - len(target_b.lstrip())]
    content_b = target_b[len(indent_b):]
    prefixed_b = indent_b + prefix_b + content_b
    if body == prefixed_b:
        entry["status"] = "skip"
        entry["reason"] = "already prefixed"
        return entry
    if body != target_b:
        entry["status"] = "skip"
        entry["reason"] = "line text at planned index changed"
        return entry
    newline = raw[len(body):]
    lines[line_index] = prefixed_b + newline
    new_block = b"".join(lines)
    new_data = data[:i + len(begin_b)] + new_block + data[j:]
    try:
        _atomic_write_bytes(note_path, new_data)
    except OSError as e:
        entry["status"] = "error"
        entry["reason"] = str(e)
        return entry
    entry["status"] = "ok"
    return entry


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _log_timestamp() -> str:
    """Timestamp safe for filenames, including microseconds so each run gets its own log file."""
    return datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S.%f")


def _today_str() -> str:
    return datetime.date.today().isoformat()


def _utc_stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path_inventory(base: Path, roots: list[dict]) -> tuple[dict[str, dict], list[str]]:
    files: dict[str, dict] = {}
    directories: set[str] = set()

    def add_file(path: Path, rel: str) -> None:
        if path.is_symlink():
            target = os.readlink(path)
            files[rel] = {"kind": "symlink", "target": target,
                          "sha256": hashlib.sha256(os.fsencode(target)).hexdigest()}
        else:
            files[rel] = {"kind": "file", "sha256": _hash_file(path)}

    for item in roots:
        if not item["exists"]:
            continue
        rel = item["path"]
        path = base / Path(rel)
        if path.is_symlink() or path.is_file():
            add_file(path, rel)
            continue
        if not path.is_dir():
            raise OSError(f"backup path is not a file or directory: {path}")
        directories.add(rel)
        for dirpath, dirnames, filenames in os.walk(path, topdown=True, followlinks=False):
            current = Path(dirpath)
            for name in list(dirnames):
                child = current / name
                child_rel = child.relative_to(base).as_posix()
                if child.is_symlink():
                    add_file(child, child_rel)
                    dirnames.remove(name)
                else:
                    directories.add(child_rel)
            for name in filenames:
                child = current / name
                add_file(child, child.relative_to(base).as_posix())
    return files, sorted(directories)


def _copy_path(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        os.symlink(os.readlink(source), target, target_is_directory=source.is_dir())
    elif source.is_dir():
        shutil.copytree(source, target, symlinks=True)
    elif source.is_file():
        shutil.copy2(source, target)
    else:
        raise OSError(f"backup source is not a file or directory: {source}")


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        try:
            path.unlink()
        except PermissionError:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            path.unlink()
    elif path.is_dir():
        def remove_readonly(func, raw_path, _exc_info):
            os.chmod(raw_path, stat.S_IWRITE | stat.S_IREAD)
            func(raw_path)
        shutil.rmtree(path, onerror=remove_readonly)


def _create_verified_backup(vault: Path, paths: list[Path]) -> tuple[Path, dict]:
    root_map: dict[str, bool] = {}
    for path in paths:
        rel = path.relative_to(vault).as_posix()
        root_map[rel] = path.exists() or path.is_symlink()
    roots = [{"path": rel, "exists": exists} for rel, exists in sorted(root_map.items())]
    parent_dirs: dict[str, bool] = {}
    for item in roots:
        parent = (vault / Path(item["path"])).parent
        while parent != vault and _is_inside_vault_path(parent, vault):
            rel = parent.relative_to(vault).as_posix()
            parent_dirs[rel] = parent.is_dir()
            parent = parent.parent
    expected_files, expected_dirs = _path_inventory(vault, roots)
    backup = vault.parent / f"{vault.name}-sweep-backup-{_utc_stamp()}"
    backup.mkdir()
    payload = backup / "payload"
    try:
        for item in roots:
            if item["exists"]:
                _copy_path(vault / Path(item["path"]), payload / Path(item["path"]))
        copied_files, copied_dirs = _path_inventory(payload, roots)
        if copied_files != expected_files or copied_dirs != expected_dirs:
            raise OSError("backup verification mismatch: file list, hashes, or directory list differ")
        manifest = {
            "version": 1,
            "vault_path": str(vault),
            "roots": roots,
            "parent_dirs": parent_dirs,
            "files": expected_files,
            "directories": expected_dirs,
        }
        (backup / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
        )
        return backup, manifest
    except BaseException:
        _remove_path(backup)
        raise


def _restore_backup(backup: Path, vault: Path) -> dict:
    backup = backup.resolve()
    vault = vault.resolve()
    if _is_inside_vault_path(backup, vault):
        raise ValueError("backup directory must be outside the vault")
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("version") != 1:
        raise ValueError("unsupported backup manifest version")
    if os.path.normcase(str(Path(manifest.get("vault_path", "")).resolve())) != os.path.normcase(str(vault)):
        raise ValueError("backup was created for a different vault")
    roots = manifest.get("roots")
    if not isinstance(roots, list):
        raise ValueError("backup manifest has no roots list")
    allowed = []
    for item in roots:
        rel = item.get("path", "")
        parts = Path(rel).parts
        if (not isinstance(rel, str) or not rel or Path(rel).is_absolute() or
                any(part in (".", "..") for part in parts) or "\\" in rel):
            raise ValueError(f"unsafe path in backup manifest: {rel!r}")
        if not ((parts and parts[0] in ("1-Projects", "4-Archive") and len(parts) >= 2)
                or rel == "_meta/projects.json"):
            raise ValueError(f"backup path is outside supported vault data: {rel}")
        target = vault / Path(rel)
        if not _is_inside_vault_path(target.parent, vault):
            raise ValueError(f"backup parent resolves outside vault: {rel}")
        allowed.append(item)
    parent_dirs = manifest.get("parent_dirs", {})
    if not isinstance(parent_dirs, dict):
        raise ValueError("backup manifest has invalid parent directory metadata")
    for rel in parent_dirs:
        parts = Path(rel).parts
        if (Path(rel).is_absolute() or any(part in (".", "..") for part in parts) or "\\" in rel or
                not ((parts and parts[0] in ("1-Projects", "4-Archive") and len(parts) >= 1)
                     or rel == "_meta")):
            raise ValueError(f"unsafe parent directory in backup manifest: {rel}")
        if not _is_inside_vault_path((vault / Path(rel)).parent, vault):
            raise ValueError(f"parent directory resolves outside vault: {rel}")
    files, dirs = _path_inventory(backup / "payload", allowed)
    if files != manifest.get("files") or dirs != manifest.get("directories"):
        raise ValueError("backup verification failed: payload does not match manifest hashes")

    # Changes made after the sweep (for example new captures) would otherwise be lost, so keep the current state first.
    pre_restore = backup.parent / f"{backup.name}-pre-restore-{_utc_stamp()}"
    for item in allowed:
        current = vault / Path(item["path"])
        if current.exists() or current.is_symlink():
            _copy_path(current, pre_restore / Path(item["path"]))
    for item in sorted(allowed, key=lambda entry: len(Path(entry["path"]).parts), reverse=True):
        target = vault / Path(item["path"])
        _remove_path(target)
    for item in sorted(allowed, key=lambda entry: len(Path(entry["path"]).parts)):
        if item["exists"]:
            _copy_path(backup / "payload" / Path(item["path"]), vault / Path(item["path"]))
        else:
            parent = (vault / Path(item["path"])).parent
            while parent != vault and _is_inside_vault_path(parent, vault):
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent

    for rel, existed in sorted(parent_dirs.items(), key=lambda pair: len(Path(pair[0]).parts)):
        directory = vault / Path(rel)
        if existed:
            directory.mkdir(parents=True, exist_ok=True)
        elif directory.is_dir():
            try:
                directory.rmdir()
            except OSError:
                pass

    restored_files, restored_dirs = _path_inventory(vault, allowed)
    if restored_files != manifest.get("files") or restored_dirs != manifest.get("directories"):
        raise OSError("restore verification failed: restored paths do not match backup hashes")
    return {"ok": True, "backup": str(backup), "files": len(restored_files)}


def _backup_targets(vault: Path, actions: list[tuple[str, dict]]) -> list[Path]:
    targets: set[Path] = set()
    for kind, item in actions:
        if kind == "fragment":
            folder, parent = item["folder"], item["proposed_parent"]
            src = vault / "1-Projects" / folder
            dest = _archive_dest(vault / "4-Archive" / "merged", folder)
            targets.update((src, vault / "1-Projects" / parent / f"{parent}.md",
                            vault / "_meta" / "projects.json", dest))
        elif kind == "leftover":
            folder = item["folder"]
            src = vault / folder
            dest = _archive_dest(vault / "4-Archive", folder)
            targets.update((src, dest))
        elif kind == "stale":
            targets.add(vault / item["note"].replace("/", os.sep))
    ordered = sorted(targets, key=lambda path: (len(path.parts), str(path)))
    roots: list[Path] = []
    for path in ordered:
        if any(parent.is_dir() and path != parent and parent in path.parents for parent in roots):
            continue
        roots.append(path)
    return roots


def _count_files(path: Path) -> int:
    n = 0
    for _, _, files in os.walk(path):
        n += len(files)
    return n


def _is_under_dir(child: Path, parent: Path) -> bool:
    """True if child.resolve() is inside parent.resolve()."""
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _safe_child_name(name: object) -> bool:
    return (isinstance(name, str) and bool(name) and name not in (".", "..")
            and "/" not in name and "\\" not in name and ":" not in name and "\0" not in name
            and Path(name).name == name
            and not Path(name).is_absolute())


def _validate_item_paths(vault: Path, kind: str, item: dict) -> str | None:
    """Return an error message if a plan item resolves to an unsafe path, otherwise None."""
    if kind == "fragment":
        folder = item.get("folder", "")
        parent = item.get("proposed_parent", "")
        if not _safe_child_name(folder) or not _safe_child_name(parent):
            return "missing folder or parent"
        if folder.casefold() == parent.casefold():
            return "fragment and parent names must differ"
        src = vault / "1-Projects" / folder
        parent_dir = vault / "1-Projects" / parent
        projects_dir = vault / "1-Projects"
        if src.parent.resolve() != projects_dir.resolve() or not _is_inside_vault_path(src, vault):
            return "fragment source must be under 1-Projects"
        if parent_dir.parent.resolve() != projects_dir.resolve() or not _is_inside_vault_path(parent_dir, vault):
            return "fragment parent must be under 1-Projects"
        if _is_under_dir(src, vault / "0-Sessions") or _is_under_dir(parent_dir, vault / "0-Sessions"):
            return "fragment path under 0-Sessions"
        fragment_dest = _archive_dest(vault / "4-Archive" / "merged", folder)
        if not _is_under_dir(fragment_dest, vault / "4-Archive") or _is_under_dir(fragment_dest, vault / "0-Sessions"):
            return "fragment target must be under 4-Archive"
        return None
    if kind == "leftover":
        folder = item.get("folder", "")
        if not _safe_child_name(folder):
            return "missing folder"
        src = vault / folder
        if src.parent.resolve() != vault.resolve() or not _is_inside_vault_path(src, vault):
            return "leftover must be a direct child of the vault root"
        if _is_under_dir(src, vault / "0-Sessions"):
            return "source under 0-Sessions"
        archive_dest = _archive_dest(vault / "4-Archive", folder)
        if not _is_under_dir(archive_dest, vault / "4-Archive") or _is_under_dir(archive_dest, vault / "0-Sessions"):
            return "target must be under 4-Archive"
        rel = str(src.relative_to(vault))
        if rel.startswith("_meta" + os.sep) or rel == ".git":
            return "leftover source is protected"
        if any(rel.startswith(p) for p in PARA_PREFIXES) or rel in PARA_KNOWN:
            return "leftover source is a PARA directory"
        return None
    if kind == "stale":
        note = item.get("note", "")
        if not isinstance(note, str) or not note:
            return "missing note"
        note_path = vault / note.replace("/", os.sep)
        if not (_is_under_dir(note_path, vault / "1-Projects") or _is_under_dir(note_path, vault / "4-Archive")):
            return "note must be under 1-Projects or 4-Archive"
        if _is_under_dir(note_path, vault / "0-Sessions"):
            return "note under 0-Sessions"
        return None
    return None


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
        err = _validate_item_paths(vault, "fragment", item)
        if err:
            actions.append(("skip", {"action": "fragment_merge", "fragment": item.get("folder"), "status": "error", "reason": f"refused: {err}"}))
        else:
            actions.append(("fragment", item))
    for item in plan.get("leftover_folders", []):
        if only_auto and not item.get("auto_apply"):
            continue
        err = _validate_item_paths(vault, "leftover", item)
        if err:
            actions.append(("skip", {"action": "leftover_archive", "folder": item.get("folder"), "status": "error", "reason": f"refused: {err}"}))
        else:
            actions.append(("leftover", item))
    for item in plan.get("stale_state_lines", []):
        if only_auto and not item.get("auto_apply"):
            continue
        if not item.get("stale", False):
            continue
        err = _validate_item_paths(vault, "stale", item)
        if err:
            actions.append(("skip", {"action": "stale_line", "note": item.get("note"), "status": "error", "reason": f"refused: {err}"}))
        else:
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
            elif kind == "skip":
                print(f"  Would skip unsafe item: {item.get('reason')}")
        log_path = vault / "_meta" / "logs" / f"sweep-{_log_timestamp()}.jsonl"
        print(f"  Would write apply log {log_path}")
        if not pause_existed:
            print(f"  Would remove {pause_path}")
        return {"ok": True, "actions": len(actions), "dry_run": True}
    pause_created = False
    sha = None
    log_entries = []
    try:
        backup, backup_manifest = _create_verified_backup(vault, _backup_targets(vault, actions))
    except Exception as e:
        print(f"error: backup failed before changes: {e}", file=sys.stderr)
        return {"error": f"backup failed before changes: {e}", "ok": False, "log_entries": []}
    print(f"sweep backup: {backup}")
    log_entries.append({"ts": _now_iso(), "action": "backup", "path": str(backup),
                        "files": len(backup_manifest["files"]), "sha": None, "status": "ok"})
    try:
        if not pause_existed:
            pause_path.parent.mkdir(parents=True, exist_ok=True)
            pause_path.write_bytes(b"")
            pause_created = True
        try:
            sha = _snapshot(vault, date_str)
            print(f"sweep snapshot {date_str}: {sha}")
            log_entries.append({"ts": _now_iso(), "action": "snapshot", "sha": sha, "status": "ok"})
        except Exception as e:
            message = f"git snapshot warning, verified backup is available: {type(e).__name__}: {e}"
            print(message, file=sys.stderr)
            log_entries.append({"ts": _now_iso(), "action": "snapshot", "sha": None,
                                "status": "warning", "reason": message})
        for kind, item in actions:
            try:
                if kind == "fragment":
                    result = _apply_fragment(vault, item, date_str)
                elif kind == "leftover":
                    result = _apply_leftover(vault, item)
                elif kind == "stale":
                    result = _apply_stale_line(vault, item)
                elif kind == "skip":
                    result = item
                else:
                    continue
            except Exception as e:
                result = {"action": kind, "status": "error", "reason": f"{type(e).__name__}: {e}"}
            result["ts"] = _now_iso()
            result["sha"] = sha
            log_entries.append(result)
    except Exception as e:
        log_entries.append({"ts": _now_iso(), "action": "abort", "sha": sha, "status": "error", "reason": f"{type(e).__name__}: {e}"})
        return {"error": str(e), "ok": False, "log_entries": log_entries}
    finally:
        log_path = vault / "_meta" / "logs" / f"sweep-{_log_timestamp()}.jsonl"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as f:
                for entry in log_entries:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as e:
            print(f"warning: could not write apply log: {e}", file=sys.stderr)
        if pause_created:
            try:
                pause_path.unlink(missing_ok=True)
            except OSError:
                pass
    return {"ok": True, "sha": sha, "backup_dir": str(backup),
            "actions": len(actions), "log_entries": log_entries}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
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
    restore = sub.add_parser("restore", help="Restore paths from a verified sweep backup")
    restore.add_argument("--backup", required=True)
    restore.add_argument("--vault", required=True)
    privacy = sub.add_parser("privacy", help="Read-only privacy scan of vault Markdown files")
    privacy.add_argument("--vault", required=True)
    privacy.add_argument("--out", required=True)
    args = ap.parse_args()
    if args.command == "privacy":
        vault = Path(args.vault).resolve()
        out = Path(args.out).resolve()
        if out.is_relative_to(vault):
            ap.error("--out must be outside the vault")
        report = privacy_scan(vault)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return 0
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
    if args.command == "restore":
        try:
            result = _restore_backup(Path(args.backup), Path(args.vault))
        except (OSError, ValueError, json.JSONDecodeError) as e:
            print(f"error: restore failed: {e}", file=sys.stderr)
            return 1
        print(f"restored {result['files']} files from {result['backup']}")
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
