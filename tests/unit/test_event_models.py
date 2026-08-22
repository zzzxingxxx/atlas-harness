import pytest
from pydantic import ValidationError

from atlas_harness.events import (
    CURRENT_SCHEMA_VERSION,
    DEFAULT_LANE,
    SUPPORTED_SCHEMA_VERSIONS,
    AssistantMessage,
    Event,
    EventType,
    SessionCreated,
    payload_for_event,
)
from atlas_harness.kernel import EventValidationError, FrozenClock, IdFactory


@pytest.fixture
def factory() -> IdFactory:
    return IdFactory(FrozenClock(1_700_000_000_000))


def make_event(factory: IdFactory, **overrides: object) -> Event:
    kwargs: dict[str, object] = {
        "event_type": EventType.ASSISTANT_MESSAGE,
        "session_id": "ses_a",
        "seq": 1,
        "payload": {"content": "hi"},
        "factory": factory,
    }
    kwargs.update(overrides)
    event_type = kwargs.pop("event_type")
    assert isinstance(event_type, EventType)
    return Event.create(event_type, **kwargs)  # type: ignore[arg-type]


def test_create_fills_envelope(factory: IdFactory) -> None:
    event = make_event(factory)

    assert event.schema_version == CURRENT_SCHEMA_VERSION
    assert event.event_id.startswith("evt_")
    assert event.lane_id == DEFAULT_LANE
    assert event.timestamp_ms == 1_700_000_000_000
    assert event.idempotency_key
    assert event.type is EventType.ASSISTANT_MESSAGE


def test_payload_is_typed_from_event_type(factory: IdFactory) -> None:
    event = make_event(factory, event_type=EventType.SESSION_CREATED, payload={"title": "t"})

    assert isinstance(event.payload, SessionCreated)
    assert event.payload.title == "t"


def test_subclass_payload_fields_survive_serialization(factory: IdFactory) -> None:
    event = make_event(factory, payload=AssistantMessage(content="hello 世界"))

    dumped = event.to_json_dict()

    assert dumped["payload"] == {"content": "hello 世界", "role": "assistant"}


def test_roundtrip_through_json_dict(factory: IdFactory) -> None:
    event = make_event(factory, event_type=EventType.TOOL_STARTED, payload={"tool_name": "fs_read"})

    restored = Event.model_validate(event.to_json_dict())

    assert restored == event


def test_idempotency_key_is_stable_for_the_same_slot(factory: IdFactory) -> None:
    first = make_event(factory)
    second = make_event(factory)
    other_seq = make_event(factory, seq=2)

    assert first.idempotency_key == second.idempotency_key
    assert first.idempotency_key != other_seq.idempotency_key
    assert first.event_id != second.event_id


def test_unsupported_schema_version_is_rejected(factory: IdFactory) -> None:
    data = make_event(factory).to_json_dict()
    data["schema_version"] = CURRENT_SCHEMA_VERSION + 1

    with pytest.raises(EventValidationError) as excinfo:
        Event.model_validate(data)

    assert excinfo.value.details == {
        "schema_version": CURRENT_SCHEMA_VERSION + 1,
        "supported": sorted(SUPPORTED_SCHEMA_VERSIONS),
    }


def test_previous_schema_version_stays_readable(factory: IdFactory) -> None:
    """M1/M2 wrote v1 logs; this build must still replay them."""

    data = make_event(factory).to_json_dict()
    data["schema_version"] = 1

    assert Event.model_validate(data).schema_version == 1


def test_unknown_fields_are_rejected(factory: IdFactory) -> None:
    data = make_event(factory).to_json_dict()
    data["surprise"] = 1

    with pytest.raises(ValidationError):
        Event.model_validate(data)


def test_events_are_frozen(factory: IdFactory) -> None:
    event = make_event(factory)

    with pytest.raises(ValidationError):
        event.seq = 2  # type: ignore[misc]


def test_seq_must_be_positive(factory: IdFactory) -> None:
    with pytest.raises(ValidationError):
        make_event(factory, seq=0)


def test_type_alias_is_normalized(factory: IdFactory) -> None:
    data = make_event(factory).to_json_dict()
    data["type"] = data.pop("event_type")

    event = Event.model_validate(data)

    assert event.event_type is EventType.ASSISTANT_MESSAGE
    assert "type" not in event.model_dump()


def test_unknown_event_type_is_rejected(factory: IdFactory) -> None:
    data = make_event(factory).to_json_dict()
    data["event_type"] = "not_a_real_event"

    with pytest.raises(ValidationError):
        Event.model_validate(data)


def test_payload_for_event_reports_domain_error() -> None:
    with pytest.raises(EventValidationError) as excinfo:
        payload_for_event(EventType.APPROVAL_REQUESTED, {})

    assert "approval_requested" in excinfo.value.message


def test_payload_keeps_unknown_keys() -> None:
    payload = payload_for_event(EventType.ASSISTANT_MESSAGE, {"content": "x", "extra": 1})

    assert payload.model_dump()["extra"] == 1
