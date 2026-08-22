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

    model_max_retries: int = Field(default=2, ge=0, le=8)
    model_max_output_tokens: int = Field(default=4_096, gt=0)
    max_iterations: int = Field(default=12, gt=0)
    max_tool_calls: int = Field(default=48, gt=0)

    snapshot_every_events: int = Field(default=50, gt=0)
    """Events between snapshots. Snapshots only shorten replay, so this is a
    performance knob: losing one costs time on recovery, never correctness."""

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
