"""Application configuration loaded from environment variables."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Validated runtime settings for the M0 application shell."""

    model_config = SettingsConfigDict(
        env_prefix="ATLAS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    workspace_root: Path = Field(default_factory=Path.cwd)
    data_dir: Path = Path(".atlas")
    log_level: str = "INFO"
    json_logs: bool = False
    model_timeout_seconds: float = Field(default=120.0, gt=0)
    operation_timeout_seconds: float = Field(default=1800.0, gt=0)
    max_tool_output_bytes: int = Field(default=131_072, gt=0)

    def resolved_workspace_root(self) -> Path:
        """Return an absolute, normalized workspace root."""

        return self.workspace_root.expanduser().resolve()

    def resolved_data_dir(self) -> Path:
        """Resolve relative runtime data below the workspace root."""

        data_dir = self.data_dir.expanduser()
        if not data_dir.is_absolute():
            data_dir = self.resolved_workspace_root() / data_dir
        return data_dir.resolve()


def load_settings() -> Settings:
    """Load and validate settings without creating files or directories."""

    return Settings()
