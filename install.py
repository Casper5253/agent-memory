"""Deploy agentmem and wire it into Claude Code, Codex and OpenCode. Idempotent; backs up every file it edits.

python -I install.py                 deploy app, config, `mem` shim, hooks, plugin, skills, QMD index
python -I install.py --no-hooks      deploy without touching agent configs
python -I install.py --uninstall     remove hooks/plugin/shims (vault, app and data are left alone)
"""
import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent
H = Path.home()
RUNTIME = H / ".agents" / "memory"
APP = RUNTIME / "app"
BIN = H / ".local" / "bin"
VAULT = H / "Documents" / "Notes" / "FDE Knowledge"
CLAUDE_SETTINGS = H / ".claude" / "settings.json"
CODEX_HOOKS = H / ".codex" / "hooks.json"
OPENCODE_PLUGIN = H / ".config" / "opencode" / "plugins" / "agentmem.js"
SKILLS_LOCAL = H / ".agents" / "pstack" / "local"
MARK = "agents/memory/app/run.py"          # identifies our hook entries
LEGACY_MARK = "obsidian-session-sync"       # retired Codex capture hooks we replace
STAMP = time.strftime("%Y%m%d-%H%M%S")
PY = Path(sys.executable)


def backup(path: Path):
    if path.exists():
        dst = RUNTIME / "backups" / STAMP / path.relative_to(H)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dst)


def deploy_app():
    staging = RUNTIME / "app.new"
    shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(REPO / "agentmem", staging / "agentmem", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(REPO / "run.py", staging / "run.py")
    if APP.exists():
        old = RUNTIME / "app.old"
        shutil.rmtree(old, ignore_errors=True)
        APP.rename(old)
        staging.rename(APP)
        shutil.rmtree(old, ignore_errors=True)
    else:
        staging.rename(APP)
    (RUNTIME / "tmp").mkdir(parents=True, exist_ok=True)
    cfg = RUNTIME / "config.json"
    current = json.loads(cfg.read_text(encoding="utf-8")) if cfg.exists() else {}
    defaults = {"vault": str(VAULT), "summarizer": "claude-haiku", "embeddings": True, "settle_minutes": 20}
    cfg.write_text(json.dumps({**defaults, **current}, indent=2) + "\n", encoding="utf-8")
    BIN.mkdir(parents=True, exist_ok=True)
    runner = (APP / "run.py").as_posix()
    (BIN / "mem.cmd").write_text(f'@"{PY}" -I "{APP / "run.py"}" %*\r\n', encoding="utf-8")
    (BIN / "mem").write_text(f'#!/bin/sh\nexec "{PY.as_posix()}" -I "{runner}" "$@"\n', encoding="utf-8", newline="\n")
    print(f"app -> {APP}; shim -> {BIN / 'mem.cmd'}")


def _hook_cmd(agent: str, windows: bool = False) -> str:
    if windows:
        return f'& "{PY}" -I "{APP / "run.py"}" hook {agent}'
    return f'"{PY.as_posix()}" -I "{(APP / "run.py").as_posix()}" hook {agent}'


def _strip(groups: list, marks=(MARK,)) -> list:
    out = []
    for g in groups or []:
        hooks = [h for h in g.get("hooks", []) if not any(m in json.dumps(h).replace("\\\\", "/").replace("\\", "/") for m in marks)]
        if hooks:
            out.append({**g, "hooks": hooks})
    return out


def wire_claude(remove=False):
    backup(CLAUDE_SETTINGS)
    data = json.loads(CLAUDE_SETTINGS.read_text(encoding="utf-8")) if CLAUDE_SETTINGS.exists() else {}
    hooks = data.setdefault("hooks", {})
    for event in ("SessionStart", "Stop", "PreCompact", "SessionEnd"):
        hooks[event] = _strip(hooks.get(event))
        if not remove:
            hooks[event].append({"hooks": [{"type": "command", "command": _hook_cmd("claude"), "timeout": 15}]})
        if not hooks[event]:
            del hooks[event]
    if not hooks:
        data.pop("hooks")
    CLAUDE_SETTINGS.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"claude hooks {'removed' if remove else 'installed'}: {CLAUDE_SETTINGS}")


def wire_codex(remove=False):
    backup(CODEX_HOOKS)
    data = json.loads(CODEX_HOOKS.read_text(encoding="utf-8")) if CODEX_HOOKS.exists() else {"hooks": {}}
    hooks = data.setdefault("hooks", {})
    for event in ("SessionStart", "Stop", "PreCompact", "SessionEnd"):
        groups = _strip(hooks.get(event), marks=(MARK, LEGACY_MARK))
        if not remove:
            entry = {"type": "command", "command": _hook_cmd("codex"), "commandWindows": _hook_cmd("codex", windows=True),
                     "timeout": 15, "statusMessage": "agentmem"}
            if event == "SessionStart":
                entry["additionalContextLimit"] = 1500
            groups.insert(0, {"hooks": [entry], **({"matcher": "startup|resume|clear|compact"} if event == "SessionStart" else {})})
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    CODEX_HOOKS.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"codex hooks {'removed' if remove else 'installed'}: {CODEX_HOOKS} (Codex asks once to trust changed hooks)")


def wire_opencode(remove=False):
    if remove:
        OPENCODE_PLUGIN.unlink(missing_ok=True)
        print("opencode plugin removed")
        return
    text = (REPO / "integrations" / "opencode" / "agentmem.js").read_text(encoding="utf-8")
    text = text.replace("__PYTHON__", PY.as_posix()).replace("__RUNNER__", (APP / "run.py").as_posix())
    OPENCODE_PLUGIN.parent.mkdir(parents=True, exist_ok=True)
    OPENCODE_PLUGIN.write_text(text, encoding="utf-8", newline="\n")
    print(f"opencode plugin -> {OPENCODE_PLUGIN}")


def install_skills():
    """memory skill (ours) + qmd skill (pinned copy from the installed QMD) -> shared pstack sync -> all agents."""
    dst = SKILLS_LOCAL / "memory"
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO / "integrations" / "skill" / "memory" / "SKILL.md", dst / "SKILL.md")
    qmd = shutil.which("qmd")
    if qmd:
        src = Path(qmd).parent / "node_modules" / "@tobilu" / "qmd" / "skills" / "qmd"
        if src.is_dir():
            shutil.rmtree(SKILLS_LOCAL / "qmd", ignore_errors=True)
            shutil.copytree(src, SKILLS_LOCAL / "qmd")
    out = subprocess.run([str(PY), "-I", str(H / ".agents" / "pstack" / "sync.py")], capture_output=True, text=True)
    print("skills synced:", out.stdout.strip().replace("\n", " ")[:300] or out.stderr[-300:])


def build_index():
    out = subprocess.run([str(PY), "-I", str(APP / "run.py"), "index"], capture_output=True, text=True, timeout=1800)
    print("qmd index:", out.stdout.strip()[-300:] or out.stderr[-300:])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-hooks", action="store_true")
    ap.add_argument("--no-index", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    a = ap.parse_args()
    if a.uninstall:
        wire_claude(remove=True)
        wire_codex(remove=True)
        wire_opencode(remove=True)
        for s in ("mem.cmd", "mem"):
            (BIN / s).unlink(missing_ok=True)
        print("uninstalled hooks, plugin and shims; vault and ~/.agents/memory left in place")
        return
    tests = subprocess.run([str(PY), "-I", "-m", "unittest", "discover", "-s", str(REPO / "tests")], capture_output=True,
                           text=True, cwd=REPO, timeout=600)
    if tests.returncode != 0:
        sys.exit("tests failed; refusing to deploy a broken build into live hooks:\n" + tests.stderr[-2000:])
    deploy_app()
    if not a.no_hooks:
        wire_claude()
        wire_codex()
        wire_opencode()
    install_skills()
    if not a.no_index:
        build_index()
    print(f"backups of edited configs: {RUNTIME / 'backups' / STAMP}")


if __name__ == "__main__":
    main()
