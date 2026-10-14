"""Paths and tunables. Everything resolves from the vault root; nothing else is written."""
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path.home()
RUNTIME = Path(os.environ.get("AGENTMEM_HOME", HOME / ".agents" / "memory"))
CAPTURE_ENV = "AGENTMEM_CAPTURE"  # set inside any process the pipeline launches; hooks no-op when present


@dataclass
class Config:
    vault: Path
    summarizer: str = "claude-haiku"      # "claude-haiku" | "none"
    settle_minutes: int = 20               # no new transcript bytes for this long == session over
    min_user_prompts: int = 2
    min_assistant_chars: int = 1500
    digest_char_cap: int = 45_000          # ~12k tokens fed to the summarizer, never the raw transcript
    llm_timeout_s: int = 180
    lock_timeout_s: float = 30.0
    max_attempts: int = 5
    embeddings: bool = True
    auto_index: bool = True                # refresh QMD after a capture
    qmd_index: str = ""                    # named QMD index ("" = default, shared with the qmd skill/MCP)
    excluded_roots: list = field(default_factory=list)

    @property
    def meta(self) -> Path:
        return self.vault / "_meta"

    @property
    def sessions_dir(self) -> Path:
        return self.vault / "0-Sessions"

    @property
    def projects_dir(self) -> Path:
        return self.vault / "1-Projects"

    @property
    def manifest_path(self) -> Path:
        return self.meta / "manifest.sqlite"

    @property
    def queue_dir(self) -> Path:
        return self.meta / "queue"

    @property
    def locks_dir(self) -> Path:
        return self.meta / "locks"

    @property
    def log_path(self) -> Path:
        return self.meta / "logs" / "capture.jsonl"

    @property
    def pause_file(self) -> Path:
        return self.meta / "PAUSE"

    @property
    def projects_map(self) -> Path:
        return self.meta / "projects.json"

    def paused(self) -> bool:
        return self.pause_file.exists()


def load(vault: "str | Path | None" = None) -> Config:
    """Vault comes from the argument, then OBSIDIAN_VAULT_PATH, then ~/.agents/memory/config.json."""
    settings = {}
    cfg_file = RUNTIME / "config.json"
    if cfg_file.exists():
        settings = json.loads(cfg_file.read_text(encoding="utf-8"))
    root = vault or os.environ.get("OBSIDIAN_VAULT_PATH") or settings.pop("vault", None)
    settings.pop("vault", None)
    if not root:
        raise SystemExit("agentmem: no vault configured (set OBSIDIAN_VAULT_PATH)")
    root = Path(root)
    if not (root / "_meta").is_dir():
        raise SystemExit(f"agentmem: {root} has no _meta/ folder; not an agentmem vault")
    known = Config.__dataclass_fields__
    return Config(vault=root, **{k: v for k, v in settings.items() if k in known})
