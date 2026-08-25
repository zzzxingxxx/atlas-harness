"""Application configuration loaded from environment variables."""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Validated runtime settings for the application shell and the agent loop."""

    model_config = SettingsConfigDict(
        env_prefix="ATLAS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        protected_namespaces=(),
    )
    """``protected_namespaces`` is cleared so ``model_*`` settings keep their env names."""

    workspace_root: Path = Field(default_factory=Path.cwd)
    data_dir: Path = Path(".atlas")
    log_level: str = "INFO"
    json_logs: bool = False
    model_timeout_seconds: float = Field(default=120.0, gt=0)
    operation_timeout_seconds: float = Field(default=1800.0, gt=0)
    max_tool_output_bytes: int = Field(default=131_072, gt=0)
    max_read_bytes: int = Field(default=1_048_576, gt=0)
    approval_mode: Literal["auto", "on_request", "always", "never"] = "on_request"
    allow_network: bool = False

    model_provider: str = "openai"
    model_name: str = "gpt-4o-mini"
    model_base_url: str | None = None
    model_api_key: SecretStr | None = None
    """Never logged, never written to an event; providers read it at call time."""

    model_anthropic_version: str = "2023-06-01"
    """Sent as ``anthropic-version`` by the native Messages adapter, ignored by the
    others. Configurable but pinned by default: the header selects a wire contract,
    so following a newer one should be a deliberate act."""

    model_max_retries: int = Field(default=2, ge=0, le=8)
    model_max_output_tokens: int = Field(default=4_096, gt=0)
    max_iterations: int = Field(default=12, gt=0)
    max_tool_calls: int = Field(default=48, gt=0)

    snapshot_every_events: int = Field(default=50, gt=0)
    """Events between snapshots. Snapshots only shorten replay, so this is a
    performance knob: losing one costs time on recovery, never correctness."""

    context_prepare_ratio: float = Field(default=0.70, gt=0, le=1)
    context_compact_ratio: float = Field(default=0.85, gt=0, le=1)
    context_force_ratio: float = Field(default=0.95, gt=0, le=1)
    """The plan's three marks: prepare, compact automatically, force. Operator
    configurable because a model with a small window may need to compact earlier."""

    context_keep_recent_turns: int = Field(default=4, gt=0)
    """Turns kept verbatim after a compaction. The model needs the thread it was
    pulling on; everything older becomes the structured summary."""

    max_artifact_inline_bytes: int = Field(default=4_096, gt=0)
    """Tool outputs above this are stored as artifacts and referenced. Below it the
    reference would cost more context than the content it replaces."""

    auto_compact: bool = True
    """Compact automatically at an iteration boundary. Turning this off makes a
    long run fail on its token budget instead, which is occasionally what a test
    wants and never what a user does."""

    skills_dir: Path = Path(".atlas/skills")
    """Where ``atlas skills load`` reads YAML/JSON skill definitions from. Relative
    paths resolve below the workspace root, so a project's skills travel with it."""

    inject_capabilities: bool = True
    """Retrieve and inject Memory and Skill into each request. Off means the loop
    builds prompts exactly as it did before M6, which is the comparison an
    evaluation of the retrieval itself needs."""

    capability_token_budget: int = Field(default=1_500, gt=0)
    """Tokens the capability slot may spend. Separate from the context budget: this
    caps what retrieval may *add*, so a growing store cannot quietly crowd out the
    transcript even while the whole prompt still fits."""

    max_injected_memories: int = Field(default=5, gt=0)
    max_injected_skills: int = Field(default=3, gt=0)
    """Small on purpose. Recall improves with more candidates; the model's ability
    to act on them does not, and every one displaces conversation."""

    mcp_config_dir: Path = Path(".atlas")
    """Directory searched for ``mcp.yaml``/``mcp.yml``/``mcp.json``. Relative paths
    resolve below the workspace root, so a project's servers travel with it."""

    enable_mcp: bool = False
    """Off by default. An MCP server is an external process with a tool list this
    runtime did not write, so bringing one up has to be a deliberate act rather
    than a consequence of a config file existing."""

    enable_subagents: bool = False
    """Off by default for the same reason as the loop's other spend limits: a
    delegated task costs tokens the caller did not ask for one at a time."""

    subagent_max_tokens: int = Field(default=20_000, gt=0)
    subagent_deadline_ms: int = Field(default=120_000, gt=0)
    """Ceiling for a task contract, not the contract itself. A task may ask for
    less; asking for more is clamped, so a prompt cannot widen its own budget."""

    http_host: str = "127.0.0.1"
    """Loopback by default. The HTTP transport has no authentication of its own, so
    a default that listened on every interface would publish a shell."""

    http_port: int = Field(default=8765, gt=0, le=65_535)

    def resolved_mcp_config_dir(self) -> Path:
        """Resolve the MCP config directory below the workspace root when relative."""

        config_dir = self.mcp_config_dir.expanduser()
        if not config_dir.is_absolute():
            config_dir = self.resolved_workspace_root() / config_dir
        return config_dir.resolve()

    def resolved_skills_dir(self) -> Path:
        """Resolve the skill directory below the workspace root when relative."""

        skills_dir = self.skills_dir.expanduser()
        if not skills_dir.is_absolute():
            skills_dir = self.resolved_workspace_root() / skills_dir
        return skills_dir.resolve()

    def resolved_workspace_root(self) -> Path:
        """Return an absolute, normalized workspace root."""

        return self.workspace_root.expanduser().resolve()

    def resolved_data_dir(self) -> Path:
        """Resolve relative runtime data below the workspace root."""

        data_dir = self.data_dir.expanduser()
        if not data_dir.is_absolute():
            data_dir = self.resolved_workspace_root() / data_dir
        return data_dir.resolve()

    def api_key(self) -> str | None:
        """Return the raw key at the call site only; it is never stored elsewhere."""

        return None if self.model_api_key is None else self.model_api_key.get_secret_value()


def load_settings() -> Settings:
    """Load and validate settings without creating files or directories."""

    return Settings()
