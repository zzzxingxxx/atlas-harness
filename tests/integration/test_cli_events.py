"""CLI commands that read the event log."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from atlas_harness.events import Event, EventStore
from atlas_harness.transport.cli import app, main

runner = CliRunner()
StoreFactory = Callable[..., EventStore]
SessionSeeder = Callable[..., list[Event]]


@pytest.fixture
def data_dir(
    store_factory: StoreFactory,
    seed: SessionSeeder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    store = store_factory(tmp_path / "runtime")
    seed(store, "ses_cli")
    store.close()
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path / "runtime"))
    return tmp_path / "runtime"


def test_sessions_lists_the_seeded_session(data_dir: Path) -> None:
    result = runner.invoke(app, ["sessions"])

    assert result.exit_code == 0
    assert "ses_cli" in result.stdout
    assert "events=7" in result.stdout


def test_sessions_json(data_dir: Path) -> None:
    result = runner.invoke(app, ["sessions", "--json"])

    assert result.exit_code == 0
    rows = json.loads(result.stdout)
    assert [row["session_id"] for row in rows] == ["ses_cli"]
    assert rows[0]["last_seq"] == 7
    assert rows[0]["title"] == "demo"


def test_sessions_without_any_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path / "empty"))

    result = runner.invoke(app, ["sessions"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "no sessions"


def test_inspect_json(data_dir: Path) -> None:
    result = runner.invoke(app, ["inspect", "ses_cli", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["session_id"] == "ses_cli"
    assert payload["status"] == "active"
    assert payload["last_seq"] == 7
    assert payload["event_count"] == 7
    assert payload["operations"] == 1
    assert payload["open_operations"] == []
    assert payload["state_hash"]


def test_inspect_human_output(data_dir: Path) -> None:
    result = runner.invoke(app, ["inspect", "ses_cli"])

    assert result.exit_code == 0
    assert "session_id: ses_cli" in result.stdout
    assert "event_count: 7" in result.stdout


def test_replay_rebuilds_the_full_state(data_dir: Path) -> None:
    result = runner.invoke(app, ["replay", "ses_cli", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["operations"]["op_demo"]["status"] == "finished"
    assert payload["messages"] == ["hello 世界"]
    assert payload["state_hash"]


def test_replay_human_output(data_dir: Path) -> None:
    result = runner.invoke(app, ["replay", "ses_cli"])

    assert result.exit_code == 0
    assert "replayed 7 events for ses_cli" in result.stdout


def test_replay_and_inspect_agree_on_the_state_hash(data_dir: Path) -> None:
    inspected = json.loads(runner.invoke(app, ["inspect", "ses_cli", "--json"]).stdout)
    replayed = json.loads(runner.invoke(app, ["replay", "ses_cli", "--json"]).stdout)

    assert inspected["state_hash"] == replayed["state_hash"]


def test_unknown_session_exits_with_a_structured_error(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["atlas", "inspect", "ses_missing"])

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 9
    payload = json.loads(capsys.readouterr().err)
    assert payload["error"] == "session_not_found"
    assert payload["details"] == {"session_id": "ses_missing"}


def test_invalid_session_id_exits_with_a_validation_error(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["atlas", "inspect", "../escape"])

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 5
    assert json.loads(capsys.readouterr().err)["error"] == "event_validation_error"


def test_tool_check_redacts_the_diff_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(tmp_path))
    args = json.dumps({"path": "secret.txt", "content": "api_key=sk-abcdefghijklmnop123456"})

    result = runner.invoke(app, ["tool-check", "write_file", "--args", args, "--json"])

    assert result.exit_code == 0
    assert "sk-abcdefghijklmnop123456" not in result.stdout
    assert "[redacted]" in json.loads(result.stdout)["preview"]


def test_approval_mode_never_cannot_be_overridden_by_yes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ATLAS_APPROVAL_MODE", "never")
    args = json.dumps({"path": "blocked.txt", "content": "blocked"})

    result = runner.invoke(
        app,
        ["tool-run", "write_file", "--args", args, "--yes", "--json"],
    )

    assert result.exit_code == 11
    assert json.loads(result.stdout)["error_code"] == "approval_denied"
    assert not (tmp_path / "blocked.txt").exists()
