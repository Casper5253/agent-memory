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
    claude_transcripts_root: Path = field(default_factory=lambda: HOME / ".claude" / "projects")
    codex_transcripts_root: Path = field(default_factory=lambda: HOME / ".codex" / "sessions")
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
    knowledge_mode: str = "off"
    knowledge_db: str = "_meta/knowledge.sqlite"
    knowledge_project_ids: dict = field(default_factory=dict)
    project_privacy: dict = field(default_factory=dict)
    privacy_email_allowlist: list[str] = field(default_factory=list)
    knowledge_input_units: int = 12000
    knowledge_output_tokens: int = 2000
    knowledge_run_captures: int = 10
    knowledge_run_calls: int = 2
    knowledge_run_seconds: int = 300
    knowledge_qmd_timeout_ms: int = 1500
    knowledge_backup_dir: str = "_meta/knowledge-backups"

    def __post_init__(self):
        from .knowledge.policy import enum, integer, uuid_id
        self.claude_transcripts_root = Path(self.claude_transcripts_root)
        self.codex_transcripts_root = Path(self.codex_transcripts_root)
        enum(self.knowledge_mode, ("off", "shadow", "active"))
        for name in ("knowledge_input_units", "knowledge_output_tokens", "knowledge_run_captures",
                     "knowledge_run_calls", "knowledge_run_seconds", "knowledge_qmd_timeout_ms"):
            integer(getattr(self, name), 1)
        for name in ("knowledge_db", "knowledge_backup_dir"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or "\\" in value or ":" in value or "\x00" in value:
                raise ValueError("invalid_knowledge_path")
            path = Path(value)
            if path.is_absolute() or ".." in path.parts or path.parts[0] != "_meta":
                raise ValueError("invalid_knowledge_path")
            if not (self.vault / path).resolve().is_relative_to(self.vault.resolve()):
                raise ValueError("invalid_knowledge_path")
        if not isinstance(self.knowledge_project_ids, dict) or not isinstance(self.project_privacy, dict):
            raise ValueError("invalid_knowledge_mapping")
        if not isinstance(self.privacy_email_allowlist, list) or any(
                not isinstance(address, str) or not address.strip() for address in self.privacy_email_allowlist):
            raise ValueError("invalid_privacy_email_allowlist")
        for project_id in self.knowledge_project_ids.values():
            uuid_id(project_id)
        for privacy in self.project_privacy.values():
            enum(privacy, ("excluded", "local_only", "model_allowed"))

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
