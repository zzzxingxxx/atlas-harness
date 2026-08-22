"""The CLI's ``compact`` command over a real store.

An operator runs ``atlas compact`` to mark a point deliberately rather than wait
for a threshold. The command's contract is narrow and worth pinning exactly: it
records a summary, and it leaves every original event where it was.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from atlas_harness.events import EventStore, EventType
from atlas_harness.transport.cli import app

runner = CliRunner()

SESSION_ID = "ses_compact_cli"
OPERATION_ID = "op_compact_cli"


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An event log holding one finished operation with a tool call in it."""

    data_dir = tmp_path / "runtime"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with EventStore(data_dir) as store:
        store.append_new(
            EventType.SESSION_CREATED,
            session_id=SESSION_ID,
            payload={"title": "compact demo", "workspace_root": str(workspace)},
        )
        store.append_new(
            EventType.OPERATION_STARTED,
            session_id=SESSION_ID,
            operation_id=OPERATION_ID,
            payload={"name": "agent_run"},
        )
        store.append_new(
            EventType.TOOL_STARTED,
            session_id=SESSION_ID,
            operation_id=OPERATION_ID,
            payload={"tool_name": "read_file", "call_id": "c1", "risk": "read", "idempotent": True},
        )
        store.append_new(
            EventType.TOOL_RESULT,
            session_id=SESSION_ID,
            operation_id=OPERATION_ID,
            payload={
                "tool_name": "read_file",
                "call_id": "c1",
                "success": True,
                "output": {"path": "notes.txt", "content": "hello"},
            },
        )
        store.append_new(
            EventType.ASSISTANT_MESSAGE,
            session_id=SESSION_ID,
            operation_id=OPERATION_ID,
            payload={"content": "an answer worth keeping"},
        )
    monkeypatch.setenv("ATLAS_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("ATLAS_MODEL_PROVIDER", "fake")
    monkeypatch.setenv("ATLAS_MODEL_NAME", "fake-model")
    return data_dir


def test_compact_records_a_summary(runtime: Path) -> None:
    result = runner.invoke(app, ["compact", SESSION_ID, "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["session_id"] == SESSION_ID
    assert payload["compactions"] == 1
    assert payload["summary"]["current_objective"]


def test_compact_human_output_shows_the_summary(runtime: Path) -> None:
    result = runner.invoke(app, ["compact", SESSION_ID])

    assert result.exit_code == 0
    assert "compacted" in result.stdout
    assert "[compacted context]" in result.stdout


def test_compact_carries_every_plan_required_field(runtime: Path) -> None:
    """The plan's list, checked on the CLI's own output."""

    payload = json.loads(runner.invoke(app, ["compact", SESSION_ID, "--json"]).stdout)

    assert set(payload["summary"]) == {
        "current_objective",
        "task_progress",
        "blockers",
        "next_actions",
        "decisions",
        "tool_lessons",
        "failed_paths",
        "evidence_refs",
        "open_questions",
    }


def test_the_tool_result_becomes_progress_and_evidence(runtime: Path) -> None:
    payload = json.loads(runner.invoke(app, ["compact", SESSION_ID, "--json"]).stdout)

    summary = payload["summary"]
    assert any("read_file" in entry for entry in summary["task_progress"])
    assert any("notes.txt" in entry for entry in summary["evidence_refs"])


def test_compacting_does_not_remove_any_event(runtime: Path) -> None:
    """The whole safety argument, checked through the CLI.

    ``replay`` rebuilds the state from the log alone, so if compaction had
    removed anything the assistant message would be gone from it.
    """

    before = json.loads(runner.invoke(app, ["replay", SESSION_ID, "--json"]).stdout)
    runner.invoke(app, ["compact", SESSION_ID])
    after = json.loads(runner.invoke(app, ["replay", SESSION_ID, "--json"]).stdout)

    assert before["messages"] == ["an answer worth keeping"]
    assert after["messages"] == before["messages"]
    assert after["last_seq"] > before["last_seq"], "the compaction itself is an event"


def test_the_objective_can_be_overridden(runtime: Path) -> None:
    payload = json.loads(
        runner.invoke(
            app, ["compact", SESSION_ID, "--objective", "ship the release", "--json"]
        ).stdout
    )

    assert payload["summary"]["current_objective"] == "ship the release"


def test_compacting_an_unknown_session_is_a_structured_error(runtime: Path) -> None:
    result = runner.invoke(app, ["compact", "ses_missing"])

    assert result.exit_code == 9
    assert "session_not_found" in result.stderr


def test_compacting_twice_records_two_events(runtime: Path) -> None:
    """A deliberate second mark is a second event, not a no-op.

    An operator asked, so the answer belongs in the record even when the first
    compaction already summarized the same history.
    """

    runner.invoke(app, ["compact", SESSION_ID])
    payload = json.loads(runner.invoke(app, ["compact", SESSION_ID, "--json"]).stdout)

    assert payload["compactions"] == 2
    assert any("compacted earlier" in entry for entry in payload["summary"]["decisions"])
