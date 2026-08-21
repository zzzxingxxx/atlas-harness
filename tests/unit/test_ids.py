import re

import pytest

from atlas_harness.kernel import EventValidationError, FrozenClock, IdFactory
from atlas_harness.kernel.ids import (
    SESSION_ID_PATTERN,
    idempotency_key,
    new_id,
    validate_session_id,
)


def test_new_id_is_prefixed_and_unique() -> None:
    first = new_id("evt")
    second = new_id("evt")

    assert first.startswith("evt_")
    assert re.fullmatch(r"evt_[0-9a-f]{32}", first)
    assert first != second


def test_new_id_without_prefix() -> None:
    assert re.fullmatch(r"[0-9a-f]{32}", new_id())


def test_idempotency_key_is_deterministic() -> None:
    assert idempotency_key("ses", "main", 1, "x") == idempotency_key("ses", "main", 1, "x")
    assert idempotency_key("ses", "main", 1, "x") != idempotency_key("ses", "main", 2, "x")


def test_idempotency_key_separates_parts() -> None:
    assert idempotency_key("ab", "c") != idempotency_key("a", "bc")


def test_validate_session_id_accepts_generated_ids() -> None:
    session_id = new_id("ses")

    assert validate_session_id(session_id) == session_id
    assert SESSION_ID_PATTERN.match(session_id)


def test_validate_session_id_rejects_path_segments() -> None:
    with pytest.raises(EventValidationError) as excinfo:
        validate_session_id("../escape")

    assert excinfo.value.details["session_id"] == "../escape"
    assert excinfo.value.code == "event_validation_error"


def test_id_factory_uses_injected_clock() -> None:
    clock = FrozenClock(1_000)
    factory = IdFactory(clock)

    assert factory.timestamp_ms() == 1_000
    clock.advance(500)
    assert factory.timestamp_ms() == 1_500


def test_id_factory_prefixes() -> None:
    factory = IdFactory(FrozenClock())

    assert factory.event_id().startswith("evt_")
    assert factory.session_id().startswith("ses_")
    assert factory.lane_id().startswith("lane_")
    assert factory.operation_id().startswith("op_")
    assert factory.idempotency_key("a", 1) == idempotency_key("a", 1)
