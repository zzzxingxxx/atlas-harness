import json
from collections.abc import Callable
from pathlib import Path

import pytest

from atlas_harness.config import Settings
from atlas_harness.events import (
    Event,
    EventBus,
    EventStore,
    EventType,
    replay,
)
from atlas_harness.events.store import INDEX_FILENAME, LOG_FILENAME, SESSIONS_DIRNAME
from atlas_harness.kernel import (
    EventStoreError,
    EventValidationError,
    SessionNotFoundError,
)

SessionSeeder = Callable[..., list[Event]]


def test_append_writes_one_json_line_per_event(
    store: EventStore, seed: SessionSeeder, tmp_path: Path
) -> None:
    events = seed(store)
    log = tmp_path / SESSIONS_DIRNAME / "ses_demo" / LOG_FILENAME

    lines = log.read_text(encoding="utf-8").splitlines()

    assert len(lines) == len(events) == 7
    assert json.loads(lines[0])["event_type"] == EventType.SESSION_CREATED.value
    assert [json.loads(line)["seq"] for line in lines] == list(range(1, 8))


def test_log_keeps_unicode_readable(store: EventStore, seed: SessionSeeder, tmp_path: Path) -> None:
    seed(store)
    log = tmp_path / SESSIONS_DIRNAME / "ses_demo" / LOG_FILENAME

    assert "hello 世界" in log.read_text(encoding="utf-8")


def test_read_events_returns_the_appended_events(seeded: tuple[EventStore, str]) -> None:
    store, session_id = seeded

    events = store.read_events(session_id)

    assert [event.seq for event in events] == list(range(1, 8))
    assert events[-1].event_type is EventType.OPERATION_FINISHED


def test_seq_allocation(store: EventStore, seed: SessionSeeder) -> None:
    assert store.last_seq("ses_demo") == 0
    assert store.next_seq("ses_demo") == 1

    seed(store)

    assert store.last_seq("ses_demo") == 7
    assert store.next_seq("ses_demo") == 8


def test_out_of_order_append_is_rejected(store: EventStore) -> None:
    store.append_new(EventType.SESSION_CREATED, session_id="ses_a")
    event = Event.create(
        EventType.ASSISTANT_MESSAGE,
        session_id="ses_a",
        seq=5,
        payload={"content": "x"},
        factory=store.ids,
    )

    with pytest.raises(EventValidationError) as excinfo:
        store.append(event)

    assert excinfo.value.details == {
        "session_id": "ses_a",
        "expected_seq": 2,
        "actual_seq": 5,
        "last_valid_seq": 1,
    }
    assert store.last_seq("ses_a") == 1


def test_duplicate_seq_append_is_rejected(store: EventStore) -> None:
    store.append_new(EventType.SESSION_CREATED, session_id="ses_a")
    event = Event.create(
        EventType.ASSISTANT_MESSAGE,
        session_id="ses_a",
        seq=1,
        payload={"content": "x"},
        factory=store.ids,
    )

    with pytest.raises(EventValidationError):
        store.append(event)

    assert len(store.read_events("ses_a")) == 1


def test_duplicate_event_id_is_rejected(store: EventStore) -> None:
    first = store.append_new(EventType.SESSION_CREATED, session_id="ses_a")
    clone = first.model_copy(update={"seq": 2, "idempotency_key": "other-key"})

    with pytest.raises(EventStoreError) as excinfo:
        store.append(clone)

    assert excinfo.value.details == {"event_id": first.event_id}
    assert len(store.read_events("ses_a")) == 1


def test_duplicate_idempotency_key_is_rejected(store: EventStore) -> None:
    first = store.append_new(EventType.SESSION_CREATED, session_id="ses_a")

    with pytest.raises(EventStoreError) as excinfo:
        store.append_new(
            EventType.ASSISTANT_MESSAGE,
            session_id="ses_a",
            payload={"content": "x"},
            idempotency_key_value=first.idempotency_key,
        )

    assert excinfo.value.details["idempotency_key"] == first.idempotency_key
    assert len(store.read_events("ses_a")) == 1


def test_unsupported_schema_version_is_refused_on_append(store: EventStore) -> None:
    event = Event.create(
        EventType.SESSION_CREATED, session_id="ses_a", seq=1, factory=store.ids
    ).model_copy(update={"schema_version": 99})

    with pytest.raises(EventValidationError) as excinfo:
        store.append(event)

    assert excinfo.value.details == {"schema_version": 99, "supported": [1, 2, 3, 4]}
    assert not store.session_exists("ses_a")


def test_load_state_matches_a_manual_replay(seeded: tuple[EventStore, str]) -> None:
    store, session_id = seeded

    state = store.load_state(session_id)

    assert state.state_hash() == replay(store.read_events(session_id)).state_hash()
    assert state.event_count == 7
    assert state.operations["op_demo"].status == "finished"


def test_load_state_of_an_unknown_session(store: EventStore) -> None:
    with pytest.raises(SessionNotFoundError) as excinfo:
        store.load_state("ses_missing")

    assert excinfo.value.details == {"session_id": "ses_missing"}
    assert excinfo.value.exit_code == 9


def test_session_listing(store: EventStore, seed: SessionSeeder) -> None:
    seed(store, "ses_one")
    store.append_new(EventType.SESSION_CREATED, session_id="ses_two", payload={"title": "two"})

    assert store.list_session_ids() == ["ses_one", "ses_two"]

    summaries = {summary.session_id: summary for summary in store.list_sessions()}

    assert summaries["ses_one"].event_count == 7
    assert summaries["ses_one"].last_seq == 7
    assert summaries["ses_one"].title == "demo"
    assert summaries["ses_one"].status == "active"
    assert summaries["ses_two"].event_count == 1
    assert summaries["ses_two"].title == "two"


def test_session_exists(store: EventStore, seed: SessionSeeder) -> None:
    assert not store.session_exists("ses_demo")

    seed(store)

    assert store.session_exists("ses_demo")


def test_iter_events_is_empty_for_a_missing_log(store: EventStore) -> None:
    assert list(store.iter_events("ses_nothing")) == []
    assert store.read_events("ses_nothing") == []


def test_append_publishes_to_the_bus(store_factory: Callable[..., EventStore]) -> None:
    bus = EventBus()
    published: list[Event] = []
    bus.subscribe(published.append)
    store = store_factory(bus=bus)

    store.append_new(EventType.SESSION_CREATED, session_id="ses_a")
    store.append_new(EventType.ASSISTANT_MESSAGE, session_id="ses_a", payload={"content": "x"})

    assert [event.seq for event in published] == [1, 2]


def test_from_settings_uses_the_resolved_data_dir(tmp_path: Path) -> None:
    settings = Settings(workspace_root=tmp_path, data_dir=Path("state"))

    with EventStore.from_settings(settings) as store:
        store.append_new(EventType.SESSION_CREATED, session_id="ses_a")

    assert (tmp_path / "state" / INDEX_FILENAME).exists()
    assert (tmp_path / "state" / SESSIONS_DIRNAME / "ses_a" / LOG_FILENAME).exists()


def test_new_session_id_is_usable_as_a_path_segment(store: EventStore) -> None:
    session_id = store.new_session_id()

    store.append_new(EventType.SESSION_CREATED, session_id=session_id)

    assert store.log_path(session_id).exists()
