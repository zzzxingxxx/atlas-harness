"""Replay must be deterministic and independent of any model.

The M8 sections at the bottom close the plan's completion condition: MCP,
sub-agents and the HTTP entry point are all verified by *this* suite rather than
by three parallel ones. That is the point of event sourcing here -- a server
handshake, a delegated task and an HTTP request are all just events, so a log
they wrote must fold the same way twice and rebuild without the index.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from atlas_harness.config import Settings
from atlas_harness.events import Event, EventStore, EventType, Reducer, replay
from atlas_harness.observability.export import build_replay_report
from atlas_harness.transport.http import build_app

StoreFactory = Callable[..., EventStore]
SessionSeeder = Callable[..., list[Event]]

M8_SESSION_ID = "ses_m8"


def seed_m8(store: EventStore, seed: SessionSeeder) -> list[Event]:
    """A session that connected a server, delegated a task and shut both down."""

    seed(store, M8_SESSION_ID)
    store.append_new(
        EventType.MCP_SERVER_CONNECTED,
        session_id=M8_SESSION_ID,
        payload={
            "server": "files",
            "transport": "stdio",
            "protocol_version": "2024-11-05",
            "tool_count": 2,
        },
    )
    store.append_new(
        EventType.MCP_TOOLS_REGISTERED,
        session_id=M8_SESSION_ID,
        payload={
            "server": "files",
            "tools": ["mcp_files_read_note"],
            "rejected": [{"tool": "write_note", "reason": "scope_not_granted"}],
            "granted_scopes": ["fs:read"],
        },
    )
    store.append_new(
        EventType.SUBAGENT_TASK_STARTED,
        session_id=M8_SESSION_ID,
        payload={
            "task_id": "task_1",
            "child_session_id": "ses_child",
            "objective": "summarize the notes",
            "allowed_tools": ["mcp_files_read_note"],
            "max_tokens": 2_000,
        },
    )
    store.append_new(
        EventType.SUBAGENT_TASK_FINISHED,
        session_id=M8_SESSION_ID,
        payload={
            "task_id": "task_1",
            "child_session_id": "ses_child",
            "outcome": "completed",
            "result": "three notes",
            "tool_calls": 1,
            "total_tokens": 640,
            "evidence_refs": ["ses_child#5"],
        },
    )
    store.append_new(
        EventType.MCP_SERVER_DISCONNECTED,
        session_id=M8_SESSION_ID,
        payload={"server": "files", "reason": "shutdown", "duration_ms": 12},
    )
    return store.read_events(M8_SESSION_ID)


def test_identical_streams_project_identical_state(seed: SessionSeeder, store: EventStore) -> None:
    events = seed(store, "ses_a")
    twin = [Event.model_validate(event.to_json_dict()) for event in events]

    first = replay(events)
    second = replay(twin)

    assert first == second
    assert first.state_hash() == second.state_hash()


def test_reopening_the_store_reproduces_the_state(
    store_factory: StoreFactory, seed: SessionSeeder
) -> None:
    store = store_factory()
    seed(store, "ses_a")
    before = store.load_state("ses_a")
    store.close()

    after = store_factory().load_state("ses_a")

    assert after == before
    assert after.state_hash() == before.state_hash()


def test_every_prefix_matches_incremental_reduction(store: EventStore, seed: SessionSeeder) -> None:
    events = seed(store, "ses_a")
    reducer = Reducer("ses_a")

    for index, event in enumerate(events, start=1):
        incremental = reducer.apply(event)
        prefix = replay(events[:index])
        assert prefix.state_hash() == incremental.state_hash()


def test_state_is_rebuilt_without_the_index(
    store_factory: StoreFactory, seed: SessionSeeder, tmp_path: Path
) -> None:
    store = store_factory()
    seed(store, "ses_a")
    expected = store.load_state("ses_a").state_hash()
    store.close()

    for path in tmp_path.glob("index.sqlite3*"):
        path.unlink()

    assert store_factory().load_state("ses_a").state_hash() == expected


def test_replay_ignores_unrelated_sessions(store: EventStore, seed: SessionSeeder) -> None:
    seed(store, "ses_a")
    seed(store, "ses_b")

    state_a = store.load_state("ses_a")
    state_b = store.load_state("ses_b")

    assert state_a.session_id == "ses_a"
    assert state_b.session_id == "ses_b"
    assert state_a.event_count == state_b.event_count == 7
    assert state_a.state_hash() != state_b.state_hash()


# --------------------------------------------------------------------------- #
# M8: the same replay verifies mcp, sub-agents and the http entrance
# --------------------------------------------------------------------------- #


def test_an_mcp_and_subagent_log_projects_identically_twice(
    store: EventStore, seed: SessionSeeder
) -> None:
    events = seed_m8(store, seed)
    twin = [Event.model_validate(event.to_json_dict()) for event in events]

    first = replay(events)
    second = replay(twin)

    assert first == second
    assert first.state_hash() == second.state_hash()


def test_the_m8_projections_are_derived_and_not_stored(
    store: EventStore, seed: SessionSeeder
) -> None:
    """The reducer answers every M8 question from the log alone."""

    state = replay(seed_m8(store, seed))

    assert state.mcp_tools["files"] == ["mcp_files_read_note"]
    assert state.subagent_task_ids == ["task_1"]
    assert state.subagent_sessions["task_1"] == "ses_child"
    assert state.subagent_outcomes["task_1"] == "completed"


def test_a_shut_down_server_and_a_finished_task_leave_nothing_open(
    store: EventStore, seed: SessionSeeder
) -> None:
    state = replay(seed_m8(store, seed))

    assert state.connected_mcp_servers == []
    assert state.mcp_servers["files"] == "shutdown"
    assert state.open_subagent_task_ids == []


def test_every_prefix_of_an_m8_log_matches_incremental_reduction(
    store: EventStore, seed: SessionSeeder
) -> None:
    """A crash between the started and finished events must still fold cleanly."""

    events = seed_m8(store, seed)
    reducer = Reducer(M8_SESSION_ID)

    for index, event in enumerate(events, start=1):
        incremental = reducer.apply(event)
        assert replay(events[:index]).state_hash() == incremental.state_hash()


def test_an_m8_log_is_rebuilt_without_the_index(
    store_factory: StoreFactory, seed: SessionSeeder, tmp_path: Path
) -> None:
    store = store_factory()
    seed_m8(store, seed)
    expected = store.load_state(M8_SESSION_ID).state_hash()
    store.close()

    for path in tmp_path.glob("index.sqlite3*"):
        path.unlink()

    assert store_factory().load_state(M8_SESSION_ID).state_hash() == expected


def test_a_dispatched_task_with_no_outcome_is_reported_rather_than_assumed(
    store: EventStore, seed: SessionSeeder
) -> None:
    """The replay report is the artefact an operator reads, so it must say so."""

    events = seed_m8(store, seed)
    truncated = [
        event for event in events if event.event_type is not EventType.SUBAGENT_TASK_FINISHED
    ]

    report = build_replay_report(truncated, session_id=M8_SESSION_ID)

    assert report.open_subagent_task_ids == ("task_1",)
    assert report.clean is False


def test_a_run_driven_over_http_replays_to_the_state_the_store_projects(
    tmp_path: Path,
) -> None:
    """The HTTP entrance writes the same log the CLI does, so it replays the same."""

    testclient = pytest.importorskip("fastapi.testclient")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    settings = Settings(
        data_dir=tmp_path / "runtime",
        workspace_root=workspace,
        model_provider="fake",
        model_name="fake-model",
    )

    with testclient.TestClient(build_app(settings)) as client:
        response = client.post("/runs", json={"prompt": "hello"})
        assert response.status_code == 200, response.text
        session_id = response.json()["summary"]["session_id"]

    with EventStore.from_settings(settings) as store:
        events = store.read_events(session_id)
        projected = store.load_state(session_id)

    replayed = replay(events, session_id=session_id)

    assert replayed.state_hash() == projected.state_hash()
    report = build_replay_report(events, expected_state_hash=projected.state_hash())
    assert report.deterministic
    assert report.clean
