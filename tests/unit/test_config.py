from pathlib import Path

from atlas_harness.config import Settings


def test_settings_resolve_relative_data_dir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("ATLAS_DATA_DIR", ".runtime")

    settings = Settings()

    assert settings.resolved_workspace_root() == tmp_path.resolve()
    assert settings.resolved_data_dir() == (tmp_path / ".runtime").resolve()


def test_settings_validate_positive_budgets() -> None:
    settings = Settings(
        model_timeout_seconds=3,
        operation_timeout_seconds=4,
        max_tool_output_bytes=5,
    )

    assert settings.model_timeout_seconds == 3
    assert settings.operation_timeout_seconds == 4
    assert settings.max_tool_output_bytes == 5
