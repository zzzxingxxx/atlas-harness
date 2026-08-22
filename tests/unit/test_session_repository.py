"""The session-scoped index tables.

These rows are a projection, so the interesting property is not that a query returns
the right answer once — it is that ``sync`` converges on the log no matter what the
table held beforehand. Every test here either checks a query or checks that
convergence.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from atlas_harness.events import EventStore, EventType
from atlas_harness.session import SessionRepository, SessionService

SESSION_ID = "ses_repo"
OPERATION_ID = "op_repo"

StoreFactory = Callable[..., EventStore]


@pytest.fixture
def repository(store: EventStore) -> SessionRepository:
    SessionService(store).create_session(session_id=SESSION_ID, title="repo")
    return SessionRepository(store)


def _start_call(
    store: EventStore,
    call_id: str,
    *,
    tool_name: str = "write_file",
    risk: str = "write",
    idempotent: bool = True,
) -> None:
    store.append_new(
        EventType.TOOL_STARTED,
        session_id=SESSION_ID,
        operation_id=OPERATION_ID,
        payload={
            "tool_name": tool_name,
            "call_id": call_id,
            "arguments": {},
            "idempotency_key": f"key-{call_id}",
            "risk": risk,
            "idempotent": idempotent,
        },
    )


def _start_operation(
    store: EventStore, operation_id: str = OPERATION_ID, lane: str = "main"
) -> None:
    store.append_new(
        EventType.OPERATION_STARTED,
        session_id=SESSION_ID,
        lane_id=lane,
        operation_id=operation_id,
        payload={"name": "agent_run"},
    )


# ----------------------------------------------------------------------- lanes


def test_sync_records_every_lane(repository: SessionRepository) -> None:
    store = repository._store
    store.append_new(
        EventType.LANE_CREATED,
        session_id=SESSION_ID,
        lane_id="side",
        payload={"lane": "side", "parent_lane": "main"},
    )
    _start_operation(store)

    repository.sync(SESSION_ID)

    lanes = {row.lane: row for row in repository.lanes(SESSION_ID)}
    assert set(lanes) == {"main", "side"}
    assert lanes["side"].parent_lane == "main"
    assert lanes["main"].parent_lane is None


def test_lanes_are_scoped_to_one_session(repository: SessionRepository) -> None:
    store = repository._store
    _start_operation(store)
    other = "ses_other"
    SessionService(store).create_session(session_id=other)
    store.append_new(
        EventType.LANE_CREATED,
        session_id=other,
        lane_id="elsewhere",
        payload={"lane": "elsewhere"},
    )

    repository.sync(SESSION_ID)
    repository.sync(other)

    assert [row.lane for row in repository.lanes(SESSION_ID)] == ["main"]
    assert "elsewhere" in {row.lane for row in repository.lanes(other)}


# ------------------------------------------------------------------ operations


def test_operations_carry_status_and_timestamps(repository: SessionRepository) -> None:
    store = repository._store
    _start_operation(store)
    store.append_new(
        EventType.OPERATION_FINISHED,
        session_id=SESSION_ID,
        operation_id=OPERATION_ID,
        payload={"result": "done"},
    )

    repository.sync(SESSION_ID)

    (row,) = repository.operations(SESSION_ID)
    assert row.id == OPERATION_ID
    assert row.status == "finished"
    assert row.started_at is not None
    assert row.finished_at is not None


def test_operations_filter_by_status(repository: SessionRepository) -> None:
    store = repository._store
    _start_operation(store, "op_open")
    _start_operation(store, "op_done")
    store.append_new(
        EventType.OPERATION_FINISHED,
        session_id=SESSION_ID,
        operation_id="op_done",
        payload={"result": "done"},
    )

    repository.sync(SESSION_ID)

    assert [row.id for row in repository.operations(SESSION_ID, status="started")] == ["op_open"]
    assert [row.id for row in repository.operations(SESSION_ID, status="finished")] == ["op_done"]


def test_operations_record_the_lane_they_ran_on(repository: SessionRepository) -> None:
    store = repository._store
    store.append_new(
        EventType.LANE_CREATED,
        session_id=SESSION_ID,
        lane_id="side",
        payload={"lane": "side"},
    )
    _start_operation(store, "op_side", lane="side")

    repository.sync(SESSION_ID)

    (row,) = repository.operations(SESSION_ID)
    assert row.lane == "side"


# ------------------------------------------------------------------ tool calls


def test_tool_calls_keep_the_fields_recovery_triages_on(repository: SessionRepository) -> None:
    """risk and idempotent both have to survive the round trip.

    Recovery reads them off the projection rather than the registry, so a lost risk
    level would turn a triaged call into an unclassifiable one.
    """

    store = repository._store
    _start_operation(store)
    _start_call(store, "c1")

    repository.sync(SESSION_ID)

    (row,) = repository.tool_calls(SESSION_ID)
    assert row.tool_call_id == "c1"
    assert row.tool_name == "write_file"
    assert row.risk == "write"
    assert row.idempotent is True
    assert row.idempotency_key == "key-c1"
    assert row.status == "started"


def test_tool_calls_filter_by_operation_and_status(repository: SessionRepository) -> None:
    store = repository._store
    _start_operation(store, "op_a")
    _start_operation(store, "op_b")
    store.append_new(
        EventType.TOOL_STARTED,
        session_id=SESSION_ID,
        operation_id="op_a",
        payload={"tool_name": "read_file", "call_id": "c_a", "arguments": {}},
    )
    store.append_new(
        EventType.TOOL_RESULT,
        session_id=SESSION_ID,
        operation_id="op_a",
        payload={"tool_name": "read_file", "call_id": "c_a", "success": True, "output": "ok"},
    )
    store.append_new(
        EventType.TOOL_STARTED,
        session_id=SESSION_ID,
        operation_id="op_b",
        payload={"tool_name": "read_file", "call_id": "c_b", "arguments": {}},
    )

    repository.sync(SESSION_ID)

    assert [r.tool_call_id for r in repository.tool_calls(SESSION_ID, operation_id="op_a")] == [
        "c_a"
    ]
    assert [r.tool_call_id for r in repository.tool_calls(SESSION_ID, status="started")] == ["c_b"]
    assert [r.tool_call_id for r in repository.tool_calls(SESSION_ID, status="succeeded")] == [
        "c_a"
    ]


def test_a_failed_call_is_indexed_as_failed(repository: SessionRepository) -> None:
    store = repository._store
    _start_operation(store)
    _start_call(store, "c1")
    store.append_new(
        EventType.TOOL_RESULT,
        session_id=SESSION_ID,
        operation_id=OPERATION_ID,
        payload={"tool_name": "write_file", "call_id": "c1", "success": False, "error": "boom"},
    )

    repository.sync(SESSION_ID)

    (row,) = repository.tool_calls(SESSION_ID)
    assert row.status == "failed"


# -------------------------------------------------------------------- snapshots


def test_snapshots_are_ordered_by_seq(repository: SessionRepository) -> None:
    store = repository._store
    for index, last_seq in enumerate((9, 4, 7), start=1):
        store.append_new(
            EventType.SNAPSHOT_CREATED,
            session_id=SESSION_ID,
            payload={
                "snapshot_id": f"snap_{index}",
                "last_seq": last_seq,
                "path": f"snap_{index}.json",
                "checksum": "sha",
            },
        )

    repository.sync(SESSION_ID)

    assert [row.seq for row in repository.snapshots(SESSION_ID)] == [4, 7, 9]


def test_latest_snapshot_is_the_highest_seq_not_the_newest_row(
    repository: SessionRepository,
) -> None:
    """Ordering is by seq, so a late snapshot of older state does not win."""

    store = repository._store
    store.append_new(
        EventType.SNAPSHOT_CREATED,
        session_id=SESSION_ID,
        payload={"snapshot_id": "snap_high", "last_seq": 20, "path": "a.json"},
    )
    store.append_new(
        EventType.SNAPSHOT_CREATED,
        session_id=SESSION_ID,
        payload={"snapshot_id": "snap_low", "last_seq": 3, "path": "b.json"},
    )

    repository.sync(SESSION_ID)

    latest = repository.latest_snapshot(SESSION_ID)
    assert latest is not None
    assert latest.snapshot_id == "snap_high"


def test_latest_snapshot_is_none_without_any(repository: SessionRepository) -> None:
    repository.sync(SESSION_ID)

    assert repository.latest_snapshot(SESSION_ID) is None


def test_snapshots_filter_by_lane(repository: SessionRepository) -> None:
    store = repository._store
    store.append_new(
        EventType.LANE_CREATED,
        session_id=SESSION_ID,
        lane_id="side",
        payload={"lane": "side"},
    )
    store.append_new(
        EventType.SNAPSHOT_CREATED,
        session_id=SESSION_ID,
        payload={"snapshot_id": "snap_main", "last_seq": 2, "path": "a.json"},
    )
    store.append_new(
        EventType.SNAPSHOT_CREATED,
        session_id=SESSION_ID,
        lane_id="side",
        payload={"snapshot_id": "snap_side", "last_seq": 3, "path": "b.json"},
    )

    repository.sync(SESSION_ID)

    assert [r.snapshot_id for r in repository.snapshots(SESSION_ID, lane="side")] == ["snap_side"]
    assert [r.snapshot_id for r in repository.snapshots(SESSION_ID, lane="main")] == ["snap_main"]


# ----------------------------------------------------------------- convergence


def test_sync_is_idempotent(repository: SessionRepository) -> None:
    """Delete-then-insert, so syncing twice must not double the rows."""

    store = repository._store
    _start_operation(store)
    _start_call(store, "c1")

    repository.sync(SESSION_ID)
    first = repository.tool_calls(SESSION_ID)
    repository.sync(SESSION_ID)

    assert repository.tool_calls(SESSION_ID) == first
    assert len(repository.operations(SESSION_ID)) == 1


def test_sync_removes_a_row_the_log_no_longer_supports(repository: SessionRepository) -> None:
    """The log wins. A row without a matching event has to disappear.

    Upsert-only would leave it behind forever, which is how an index starts lying
    about state that was rolled back.
    """

    store = repository._store
    _start_operation(store)
    repository.sync(SESSION_ID)
    store.index.connection.execute(
        "INSERT INTO tool_calls (tool_call_id, session_id, operation_id, tool_name, status)"
        " VALUES (?, ?, ?, ?, ?)",
        ("ghost", SESSION_ID, OPERATION_ID, "write_file", "started"),
    )
    store.index.connection.commit()
    assert [row.tool_call_id for row in repository.tool_calls(SESSION_ID)] == ["ghost"]

    repository.sync(SESSION_ID)

    assert repository.tool_calls(SESSION_ID) == []


def test_sync_accepts_a_state_the_caller_already_projected(repository: SessionRepository) -> None:
    """The state argument is a shortcut, not a different source of truth."""

    store = repository._store
    _start_operation(store)
    state = store.load_state(SESSION_ID)

    returned = repository.sync(SESSION_ID, state)

    assert returned is state
    assert [row.id for row in repository.operations(SESSION_ID)] == [OPERATION_ID]


def test_queries_return_nothing_for_an_unknown_session(repository: SessionRepository) -> None:
    assert repository.lanes("ses_missing") == []
    assert repository.operations("ses_missing") == []
    assert repository.tool_calls("ses_missing") == []
    assert repository.snapshots("ses_missing") == []
