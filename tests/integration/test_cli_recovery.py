"""CLI commands that drive crash recovery: recover, resume, abort, lanes.

These go through the real Typer app against a real on-disk log, so they cover the
part the unit tests cannot: that the operator-facing surface reports the triage
faithfully and exits non-zero while a decision is still owed.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from atlas_harness.events import EventStore, EventType
from atlas_harness.kernel.errors import RecoveryError
from atlas_harness.transport.cli import app

runner = CliRunner()
StoreFactory = Callable[..., EventStore]

SESSION_ID = "ses_cli_recover"
OPERATION_ID = "op_cli"


def _seed_crashed_session(store: EventStore) -> None:
    """A session left mid-write: tool_started with no tool_result."""

    store.append_new(
        EventType.SESSION_CREATED,
        session_id=SESSION_ID,
        payload={"title": "crashed", "workspace_root": "/tmp/ws"},
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
        payload={
            "tool_name": "write_file",
            "call_id": "call_write",
            "arguments": {"path": "notes.txt"},
            "idempotency_key": "key-write",
            "risk": "write",
            "idempotent": True,
        },
    )


@pytest.fixture
def data_dir(store_factory: StoreFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    runtime = tmp_path / "runtime"
    store = store_factory(runtime)
    _seed_crashed_session(store)
    store.close()
    monkeypatch.setenv("ATLAS_DATA_DIR", str(runtime))
    return runtime


# ---------------------------------------------------------------------- recover


def test_recover_reports_the_pending_confirmation(data_dir: Path) -> None:
    result = runner.invoke(app, ["recover", SESSION_ID, "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["needs_confirmation"] is True
    assert payload["operations"][0]["confirm"] == ["call_write"]


def test_recover_prints_the_next_command(data_dir: Path) -> None:
    """The plan tells the operator exactly what to type, ids included."""

    result = runner.invoke(app, ["recover", SESSION_ID])

    assert result.exit_code == 0
    assert f"next: atlas resume {SESSION_ID} --confirm call_write" in result.stdout


def test_recover_explains_why_the_call_is_held(data_dir: Path) -> None:
    result = runner.invoke(app, ["recover", SESSION_ID])

    assert result.exit_code == 0
    assert "confirm call_write (write_file)" in result.stdout
    assert "idempotent but write risk" in result.stdout


def test_recover_writes_nothing(data_dir: Path, store_factory: StoreFactory) -> None:
    """Inspection is read-only, so it can be run freely before deciding."""

    before = store_factory(data_dir).load_state(SESSION_ID).last_seq

    runner.invoke(app, ["recover", SESSION_ID])

    assert store_factory(data_dir).load_state(SESSION_ID).last_seq == before


def test_recover_on_an_unknown_session_exits_with_a_recovery_error(data_dir: Path) -> None:
    result = runner.invoke(app, ["recover", "ses_missing"])

    assert result.exit_code == RecoveryError.exit_code


# ----------------------------------------------------------------------- resume


def test_resume_without_confirmation_exits_non_zero(data_dir: Path) -> None:
    """A held call is not a success. The exit code has to say so for scripts."""

    result = runner.invoke(app, ["resume", SESSION_ID, "--json"])

    assert result.exit_code == RecoveryError.exit_code
    payload = json.loads(result.stdout)
    assert payload["needs_confirmation"] is True


def test_resume_with_the_confirmation_succeeds(data_dir: Path) -> None:
    result = runner.invoke(app, ["resume", SESSION_ID, "--confirm", "call_write", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["needs_confirmation"] is False
    assert payload["operations"][0]["replay"] == ["call_write"]


def test_resume_records_the_confirmation_in_the_log(
    data_dir: Path, store_factory: StoreFactory
) -> None:
    runner.invoke(app, ["resume", SESSION_ID, "--confirm", "call_write"])

    state = store_factory(data_dir).load_state(SESSION_ID)
    assert state.operations[OPERATION_ID].tool_calls["call_write"].confirmed is True


def test_resume_does_not_execute_the_tool(data_dir: Path, store_factory: StoreFactory) -> None:
    """Resuming authorizes a replay; it does not perform one.

    The distinction matters: if resume ran the tool, an operator inspecting a
    session would trigger side effects by looking at it.
    """

    runner.invoke(app, ["resume", SESSION_ID, "--confirm", "call_write"])

    events = store_factory(data_dir).read_events(SESSION_ID)
    assert [event for event in events if event.event_type is EventType.TOOL_RESULT] == []


def test_resume_rejects_an_unknown_call_id(data_dir: Path) -> None:
    result = runner.invoke(app, ["resume", SESSION_ID, "--confirm", "call_nope"])

    assert result.exit_code == RecoveryError.exit_code
    assert "unknown_tool_call_ids" in result.stderr


def test_resume_is_idempotent_once_confirmed(data_dir: Path) -> None:
    """A second resume finds nothing owed and stays quiet rather than re-asking."""

    runner.invoke(app, ["resume", SESSION_ID, "--confirm", "call_write"])

    result = runner.invoke(app, ["resume", SESSION_ID, "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["needs_confirmation"] is False


# ------------------------------------------------------------------------ abort


def test_abort_closes_the_unfinished_operation(data_dir: Path) -> None:
    result = runner.invoke(app, ["abort", SESSION_ID, "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["aborted_operations"] == [OPERATION_ID]


def test_abort_records_the_reason(data_dir: Path, store_factory: StoreFactory) -> None:
    runner.invoke(app, ["abort", SESSION_ID, "--reason", "operator gave up"])

    events = store_factory(data_dir).read_events(SESSION_ID)
    aborted = [event for event in events if event.event_type is EventType.OPERATION_ABORTED]
    assert aborted[-1].payload.model_dump(mode="json")["reason"] == "operator gave up"


def test_abort_leaves_nothing_to_recover(data_dir: Path) -> None:
    runner.invoke(app, ["abort", SESSION_ID])

    result = runner.invoke(app, ["recover", SESSION_ID])

    assert result.exit_code == 0
    assert "nothing unfinished" in result.stdout


def test_abort_does_not_delete_history(data_dir: Path, store_factory: StoreFactory) -> None:
    """Aborting appends a closing event. The tool_started stays on the record."""

    runner.invoke(app, ["abort", SESSION_ID])

    events = store_factory(data_dir).read_events(SESSION_ID)
    started = [event for event in events if event.event_type is EventType.TOOL_STARTED]
    assert len(started) == 1


# ------------------------------------------------------------------------ lanes


def test_lanes_lists_the_main_lane(data_dir: Path) -> None:
    result = runner.invoke(app, ["lanes", SESSION_ID, "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["current_lane"] == "main"
    assert [lane["lane_id"] for lane in payload["lanes"]] == ["main"]


def test_lanes_marks_the_current_lane(data_dir: Path, store_factory: StoreFactory) -> None:
    store = store_factory(data_dir)
    store.append_new(
        EventType.BRANCH_CREATED,
        session_id=SESSION_ID,
        lane_id="side",
        payload={"lane": "side", "parent_lane": "main", "from_seq": 2, "label": "retry"},
    )
    store.close()

    result = runner.invoke(app, ["lanes", SESSION_ID])

    assert result.exit_code == 0
    assert "* main" in result.stdout
    assert "  side" in result.stdout
    assert "parent=main from_seq=2" in result.stdout
