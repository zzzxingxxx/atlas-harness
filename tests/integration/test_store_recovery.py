"""Crash and recovery behaviour of the event store."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from atlas_harness.events import (
    FAULT_AFTER_INDEX_COMMIT,
    FAULT_AFTER_LOG_WRITE,
    FAULT_BEFORE_INDEX_COMMIT,
    FAULT_BEFORE_LOG_WRITE,
    Event,
    EventStore,
    EventType,
)
from atlas_harness.events.store import INDEX_FILENAME
from atlas_harness.kernel import FaultInjected, FaultInjector

SESSION_ID = "ses_crash"
OPERATION_ID = "op_crash"

SCRIPT: tuple[tuple[EventType, dict[str, Any], str | None], ...] = (
    (EventType.SESSION_CREATED, {"title": "demo", "workspace_root": "/ws"}, None),
    (EventType.OPERATION_STARTED, {"name": "chat"}, OPERATION_ID),
    (EventType.MODEL_REQUESTED, {"provider": "stub", "model": "m1"}, OPERATION_ID),
    (EventType.ASSISTANT_MESSAGE, {"content": "hi"}, OPERATION_ID),
    (EventType.OPERATION_FINISHED, {"result": "ok"}, OPERATION_ID),
)

StoreFactory = Callable[..., EventStore]

# A crash after the log write leaves a durable event the index has not seen.
SURVIVING_FAULTS = {FAULT_AFTER_LOG_WRITE, FAULT_AFTER_INDEX_COMMIT}


def append_step(store: EventStore, index: int, session_id: str = SESSION_ID) -> Event:
    event_type, payload, operation_id = SCRIPT[index]
    return store.append_new(
        event_type,
        session_id=session_id,
        payload=payload,
        operation_id=operation_id,
    )


def append_range(store: EventStore, start: int, stop: int) -> None:
    for index in range(start, stop):
        append_step(store, index)


@pytest.fixture
def clean_state_hash(store_factory: StoreFactory, tmp_path: Path) -> str:
    """Hash of the projection produced by an uninterrupted run of SCRIPT."""

    store = store_factory(tmp_path / "clean")
    append_range(store, 0, len(SCRIPT))
    return store.load_state(SESSION_ID).state_hash()


@pytest.mark.parametrize(
    "fault_point",
    [
        FAULT_BEFORE_LOG_WRITE,
        FAULT_AFTER_LOG_WRITE,
        FAULT_BEFORE_INDEX_COMMIT,
        FAULT_AFTER_INDEX_COMMIT,
    ],
)
@pytest.mark.parametrize("crash_at", range(len(SCRIPT)))
def test_restart_after_a_crash_rebuilds_state(
    store_factory: StoreFactory,
    tmp_path: Path,
    clean_state_hash: str,
    fault_point: str,
    crash_at: int,
) -> None:
    injector = FaultInjector()
    crashed = store_factory(tmp_path / "crashed", injector=injector)
    append_range(crashed, 0, crash_at)

    injector.arm(fault_point)
    with pytest.raises(FaultInjected):
        append_step(crashed, crash_at)
    crashed.close()

    durable = crash_at + 1 if fault_point in SURVIVING_FAULTS else crash_at
    restarted = store_factory(tmp_path / "crashed")

    assert restarted.last_seq(SESSION_ID) == durable
    assert len(restarted.read_events(SESSION_ID)) == durable
    if durable:
        state = restarted.load_state(SESSION_ID)
        assert state.event_count == durable
        summary = restarted.index.get_session(SESSION_ID)
        assert summary is not None
        assert summary.last_seq == durable
        assert summary.event_count == durable

    append_range(restarted, durable, len(SCRIPT))

    assert restarted.load_state(SESSION_ID).state_hash() == clean_state_hash


def test_index_is_repaired_from_the_log(store_factory: StoreFactory, tmp_path: Path) -> None:
    injector = FaultInjector()
    crashed = store_factory(injector=injector)
    append_range(crashed, 0, 2)
    injector.arm(FAULT_AFTER_LOG_WRITE)
    with pytest.raises(FaultInjected):
        append_step(crashed, 2)

    assert crashed.index.last_seq(SESSION_ID) == 2
    crashed.close()

    restarted = store_factory()

    assert restarted.last_seq(SESSION_ID) == 3
    assert restarted.index.last_seq(SESSION_ID) == 3


def test_failed_index_write_truncates_the_log(
    store_factory: StoreFactory, faults: FaultInjector
) -> None:
    store = store_factory()
    append_range(store, 0, 2)
    log_size = store.log_path(SESSION_ID).stat().st_size

    faults.arm(FAULT_BEFORE_INDEX_COMMIT)
    with pytest.raises(FaultInjected):
        append_step(store, 2)

    assert store.log_path(SESSION_ID).stat().st_size == log_size
    assert len(store.read_events(SESSION_ID)) == 2
    assert store.index.last_seq(SESSION_ID) == 2

    append_step(store, 2)

    assert store.last_seq(SESSION_ID) == 3


def test_index_can_be_deleted_and_rebuilt(store_factory: StoreFactory, tmp_path: Path) -> None:
    store = store_factory()
    append_range(store, 0, len(SCRIPT))
    expected = store.list_sessions()
    store.close()

    for suffix in ("", "-wal", "-shm"):
        path = tmp_path / f"{INDEX_FILENAME}{suffix}"
        if path.exists():
            path.unlink()

    rebuilt = store_factory()

    assert rebuilt.list_sessions() == expected
    assert rebuilt.next_seq(SESSION_ID) == len(SCRIPT) + 1


def test_stale_index_rows_are_dropped(store_factory: StoreFactory, tmp_path: Path) -> None:
    store = store_factory()
    append_range(store, 0, len(SCRIPT))
    log = store.log_path(SESSION_ID)
    lines = log.read_text(encoding="utf-8").splitlines(keepends=True)
    store.close()

    # Simulate a log rolled back further than the index knows about.
    log.write_text("".join(lines[:2]), encoding="utf-8")
    reopened = store_factory()

    assert reopened.last_seq(SESSION_ID) == 2
    summary = reopened.index.get_session(SESSION_ID)
    assert summary is not None
    assert summary.event_count == 2
    assert reopened.load_state(SESSION_ID).event_count == 2
