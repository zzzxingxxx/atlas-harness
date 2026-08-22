"""A damaged log must produce a locatable error instead of a silent recovery."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from atlas_harness.events import (
    CURRENT_SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    EventStore,
    EventType,
)
from atlas_harness.kernel import EventLogCorruptionError

SESSION_ID = "ses_bad"
StoreFactory = Callable[..., EventStore]


@pytest.fixture
def log(store_factory: StoreFactory) -> Path:
    """A three-event log, written by a store that is closed afterwards."""

    store = store_factory()
    store.append_new(EventType.SESSION_CREATED, session_id=SESSION_ID, payload={"title": "t"})
    for index in range(2):
        store.append_new(
            EventType.ASSISTANT_MESSAGE,
            session_id=SESSION_ID,
            payload={"content": f"m{index}"},
        )
    path = store.log_path(SESSION_ID)
    store.close()
    return path


def append_raw(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)


def read_corruption(store: EventStore) -> EventLogCorruptionError:
    with pytest.raises(EventLogCorruptionError) as excinfo:
        store.read_events(SESSION_ID)
    error = excinfo.value
    assert error.code == "event_log_corruption"
    assert error.exit_code == 8
    assert "last_valid_seq" in error.details
    return error


def test_truncated_final_line(log: Path, store_factory: StoreFactory) -> None:
    text = log.read_text(encoding="utf-8")
    log.write_text(text[: -len(text.splitlines(keepends=True)[-1]) // 2], encoding="utf-8")

    error = read_corruption(store_factory())

    assert error.message == "event log ends with a partial line"
    assert error.details["line"] == 3
    assert error.details["last_valid_seq"] == 2


def test_invalid_json_line(log: Path, store_factory: StoreFactory) -> None:
    append_raw(log, "{not json}\n")

    error = read_corruption(store_factory())

    assert error.message == "event log line is not valid json"
    assert error.details == {
        "session_id": SESSION_ID,
        "line": 4,
        "last_valid_seq": 3,
        "error": error.details["error"],
    }


def test_json_that_is_not_an_object(log: Path, store_factory: StoreFactory) -> None:
    append_raw(log, "[1, 2, 3]\n")

    assert read_corruption(store_factory()).message == "event log line is not a json object"


def test_blank_line(log: Path, store_factory: StoreFactory) -> None:
    append_raw(log, "\n")

    error = read_corruption(store_factory())

    assert error.message == "blank line in event log"
    assert error.details["line"] == 4


def test_unsupported_schema_version(log: Path, store_factory: StoreFactory) -> None:
    last = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    append_raw(
        log,
        json.dumps({**last, "seq": 4, "schema_version": CURRENT_SCHEMA_VERSION + 1}) + "\n",
    )

    error = read_corruption(store_factory())

    assert error.message == "unsupported event schema version"
    assert error.details["schema_version"] == CURRENT_SCHEMA_VERSION + 1
    assert error.details["supported"] == sorted(SUPPORTED_SCHEMA_VERSIONS)
    assert error.details["last_valid_seq"] == 3


def test_event_that_does_not_match_the_schema(log: Path, store_factory: StoreFactory) -> None:
    last = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    del last["idempotency_key"]
    append_raw(log, json.dumps({**last, "seq": 4}) + "\n")

    assert read_corruption(store_factory()).message == "event does not match the current schema"


def test_event_from_another_session(log: Path, store_factory: StoreFactory) -> None:
    last = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    append_raw(log, json.dumps({**last, "seq": 4, "session_id": "ses_other"}) + "\n")

    error = read_corruption(store_factory())

    assert error.message == "event belongs to a different session"
    assert error.details["event_session_id"] == "ses_other"


def test_seq_gap_in_the_log(log: Path, store_factory: StoreFactory) -> None:
    last = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    append_raw(log, json.dumps({**last, "seq": 9, "event_id": "evt_gap"}) + "\n")

    error = read_corruption(store_factory())

    assert error.message == "event seq is duplicated, missing or out of order"
    assert error.details["expected_seq"] == 4
    assert error.details["actual_seq"] == 9
    assert error.details["last_valid_seq"] == 3


def test_duplicate_seq_in_the_log(log: Path, store_factory: StoreFactory) -> None:
    last = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    append_raw(log, json.dumps(last) + "\n")

    assert read_corruption(store_factory()).details["actual_seq"] == 3


def test_duplicate_event_id_in_the_log(log: Path, store_factory: StoreFactory) -> None:
    lines = log.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    last = json.loads(lines[-1])
    append_raw(
        log,
        json.dumps({**last, "seq": 4, "event_id": first["event_id"], "idempotency_key": "k4"})
        + "\n",
    )

    error = read_corruption(store_factory())

    assert error.message == "duplicate event id in log"
    assert error.details["event_id"] == first["event_id"]


def test_duplicate_idempotency_key_in_the_log(log: Path, store_factory: StoreFactory) -> None:
    lines = log.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    last = json.loads(lines[-1])
    append_raw(
        log,
        json.dumps(
            {
                **last,
                "seq": 4,
                "event_id": "evt_dup_key",
                "idempotency_key": first["idempotency_key"],
            }
        )
        + "\n",
    )

    error = read_corruption(store_factory())

    assert error.message == "duplicate idempotency key in log"
    assert error.details["idempotency_key"] == first["idempotency_key"]


def test_corruption_blocks_further_appends(log: Path, store_factory: StoreFactory) -> None:
    append_raw(log, "{not json}\n")
    store = store_factory()

    with pytest.raises(EventLogCorruptionError):
        store.append_new(
            EventType.ASSISTANT_MESSAGE, session_id=SESSION_ID, payload={"content": "x"}
        )
    with pytest.raises(EventLogCorruptionError):
        store.load_state(SESSION_ID)
    with pytest.raises(EventLogCorruptionError):
        store.list_sessions()


def test_a_healthy_session_is_unaffected_by_a_broken_neighbour(
    log: Path, store_factory: StoreFactory
) -> None:
    append_raw(log, "{not json}\n")
    store = store_factory()

    store.append_new(EventType.SESSION_CREATED, session_id="ses_ok", payload={"title": "ok"})

    assert store.load_state("ses_ok").event_count == 1
    assert {summary.session_id for summary in store.list_sessions(sync=False)} == {
        SESSION_ID,
        "ses_ok",
    }
