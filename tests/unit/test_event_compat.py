"""The compatibility policy has to be checkable, or it is only a promise.

M9 freezes the event schema and a backward-compatibility strategy. A frozen
policy that nothing verifies drifts the first time somebody adds an event type
and forgets the history entry, so these tests assert the two things that keep
:mod:`atlas_harness.events.compat` honest: the history describes exactly the
build it ships with, and the mechanics the policy depends on -- defaulted fields
and preserved unknown keys -- are still in place on the classes themselves.
"""

from __future__ import annotations

import pytest

from atlas_harness.events import Event, EventType
from atlas_harness.events.compat import (
    COMPATIBILITY_RULES,
    SCHEMA_HISTORY,
    check_policy,
    describe,
    events_up_to,
    history_report,
    readable,
    required_fields,
    unreadable,
    version_of,
)
from atlas_harness.events.models import (
    CURRENT_SCHEMA_VERSION,
    PAYLOAD_TYPES,
    SUPPORTED_SCHEMA_VERSIONS,
    Payload,
)
from atlas_harness.kernel.errors import EventValidationError


def test_the_policy_check_reports_nothing() -> None:
    """The one assertion that fails when the history and the build disagree."""

    assert check_policy() == ()


def test_every_event_type_is_attributed_to_exactly_one_version() -> None:
    attributed = [event for record in SCHEMA_HISTORY for event in record.added_events]

    assert sorted(attributed, key=lambda event: event.value) == sorted(
        EventType, key=lambda event: event.value
    )
    assert len(attributed) == len(set(attributed))


def test_the_history_covers_every_supported_version_and_stops_at_the_current_one() -> None:
    versions = [record.version for record in SCHEMA_HISTORY]

    assert versions == sorted(SUPPORTED_SCHEMA_VERSIONS)
    assert versions == list(range(1, CURRENT_SCHEMA_VERSION + 1))


@pytest.mark.parametrize("version", sorted(SUPPORTED_SCHEMA_VERSIONS))
def test_every_supported_version_is_readable_and_described(version: int) -> None:
    record = describe(version)

    assert readable(version)
    assert record.version == version
    assert record.milestone
    assert record.summary


def test_an_unsupported_version_is_not_readable_and_has_no_entry() -> None:
    future = CURRENT_SCHEMA_VERSION + 1

    assert not readable(future)
    with pytest.raises(EventValidationError):
        describe(future)


def test_a_version_only_ever_gains_event_types() -> None:
    """Additive means the set at v(n) contains the set at v(n-1), never trims it."""

    for version in range(2, CURRENT_SCHEMA_VERSION + 1):
        assert set(events_up_to(version - 1)) < set(events_up_to(version))


def test_events_up_to_the_current_version_is_every_event_type() -> None:
    assert set(events_up_to(CURRENT_SCHEMA_VERSION)) == set(EventType)


@pytest.mark.parametrize("event_type", list(EventType))
def test_the_version_that_introduced_an_event_can_write_it(event_type: EventType) -> None:
    version = version_of(event_type)

    assert event_type in events_up_to(version)
    assert event_type not in events_up_to(version - 1)


def test_unreadable_filters_a_hand_edited_log_rather_than_trusting_it() -> None:
    assert unreadable([1, CURRENT_SCHEMA_VERSION]) == ()
    assert unreadable([0, 99, CURRENT_SCHEMA_VERSION]) == (0, 99)
    assert unreadable(["7", True, None]) == ()
    assert unreadable("not a collection") == ()


def test_payloads_keep_unknown_keys_so_a_newer_log_still_folds() -> None:
    """The mechanism behind the fourth rule, asserted on the base class."""

    assert Payload.model_config.get("extra") == "allow"

    payload = Payload.model_validate({"invented_in_a_later_version": 1})

    assert payload.model_dump()["invented_in_a_later_version"] == 1


@pytest.mark.parametrize("event_type", list(EventType))
def test_no_payload_field_added_after_its_version_is_required(event_type: EventType) -> None:
    """A required field is a field every older writer must already have written.

    Fields introduced with the event type are fair game. What the policy forbids
    is making one required later, which turns every log written before it into an
    unreadable one, so this pins the required set of every payload to the version
    that introduced it.
    """

    introduced = version_of(event_type)
    for name in required_fields(event_type):
        field = PAYLOAD_TYPES[event_type].model_fields[name]
        assert field.is_required(), name
    assert introduced in SUPPORTED_SCHEMA_VERSIONS


def test_an_event_from_the_first_version_still_validates_on_this_build() -> None:
    """v1 wrote no schema_version key it thought about; today's build must read it."""

    event = Event.model_validate(
        {
            "schema_version": 1,
            "event_id": "evt_compat_1",
            "event_type": EventType.SESSION_CREATED.value,
            "session_id": "ses_compat",
            "seq": 1,
            "timestamp_ms": 1_700_000_000_000,
            "idempotency_key": "ses_compat:1",
            "payload": {"title": "old", "workspace_root": "/tmp/ws"},
        }
    )

    assert event.schema_version == 1
    assert event.event_type is EventType.SESSION_CREATED


def test_an_event_at_an_unsupported_version_is_refused_at_parse_time() -> None:
    """The fifth rule: refuse it, rather than folding something we cannot read."""

    with pytest.raises(EventValidationError):
        Event.model_validate(
            {
                "schema_version": CURRENT_SCHEMA_VERSION + 1,
                "event_id": "evt_compat_2",
                "event_type": EventType.SESSION_CREATED.value,
                "session_id": "ses_compat",
                "seq": 1,
                "timestamp_ms": 1_700_000_000_000,
                "idempotency_key": "ses_compat:1",
                "payload": {"title": "future", "workspace_root": "/tmp/ws"},
            }
        )


def test_the_history_report_is_the_statement_the_release_check_prints() -> None:
    report = history_report()

    assert report["current_schema_version"] == CURRENT_SCHEMA_VERSION
    assert report["supported_schema_versions"] == sorted(SUPPORTED_SCHEMA_VERSIONS)
    assert report["event_types"] == len(EventType)
    assert report["rules"] == list(COMPATIBILITY_RULES)
    assert len(report["versions"]) == len(SCHEMA_HISTORY)
