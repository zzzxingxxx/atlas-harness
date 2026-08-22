"""The startup sequence and the session / operation lifecycle.

The startup order is the point of these tests: a crashed process has to be noticed
before new work is scheduled, so an unanswered question must block the operation from
opening rather than being discovered later.
"""

from __future__ import annotations

import pytest

from atlas_harness.events import EventStore, EventType
from atlas_harness.events.models import DEFAULT_LANE, SUSPENDED_STATUS
from atlas_harness.kernel.errors import EventValidationError, RecoveryError
from atlas_harness.session import SessionService

SESSION_ID = "ses_service"
OPERATION_ID = "op_service"


@pytest.fixture
def service(store: EventStore) -> SessionService:
    return SessionService(store)


def _open_write_call(store: EventStore, call_id: str = "c1") -> None:
    """Leave a non-idempotent write mid-flight: started, never resulted."""

    store.append_new(
        EventType.TOOL_STARTED,
        session_id=SESSION_ID,
        operation_id=OPERATION_ID,
        payload={
            "tool_name": "run_command",
            "call_id": call_id,
            "risk": "write",
            "idempotent": False,
            "idempotency_key": f"key-{call_id}",
        },
    )


def _open_read_call(store: EventStore, call_id: str = "r1") -> None:
    store.append_new(
        EventType.TOOL_STARTED,
        session_id=SESSION_ID,
        operation_id=OPERATION_ID,
        payload={
            "tool_name": "read_file",
            "call_id": call_id,
            "risk": "read",
            "idempotent": True,
            "idempotency_key": f"key-{call_id}",
        },
    )


# --------------------------------------------------------------------- sessions


def test_create_session_is_idempotent(service: SessionService) -> None:
    """Calling twice must not append a second session_created."""

    first = service.create_session(session_id=SESSION_ID, title="demo")
    second = service.create_session(session_id=SESSION_ID, title="ignored")

    assert first == second == SESSION_ID
    events = service.store.read_events(SESSION_ID)
    assert [event.event_type for event in events] == [EventType.SESSION_CREATED]
    assert events[0].payload.model_dump(mode="json")["title"] == "demo"


def test_create_session_generates_an_id_when_none_is_given(service: SessionService) -> None:
    session_id = service.create_session(title="auto")

    assert session_id.startswith("ses_")
    assert service.store.session_exists(session_id)


def test_create_session_rejects_a_malformed_id(service: SessionService) -> None:
    """A traversal id is refused before it can reach the filesystem."""

    with pytest.raises(EventValidationError) as excinfo:
        service.create_session(session_id="../escape")

    assert excinfo.value.details["session_id"] == "../escape"


# ------------------------------------------------------------------- lifecycle


def test_operation_lifecycle_reaches_finished(service: SessionService) -> None:
    service.create_session(session_id=SESSION_ID)
    operation_id = service.start_operation(SESSION_ID, name="chat", operation_id=OPERATION_ID)
    service.finish_operation(SESSION_ID, operation_id, result="done")

    operations = service.operations(SESSION_ID)
    assert [operation.status for operation in operations] == ["finished"]
    assert service.operations(SESSION_ID, status="started") == []


def test_fail_and_abort_are_both_terminal(service: SessionService) -> None:
    service.create_session(session_id=SESSION_ID)
    failed = service.start_operation(SESSION_ID, name="a", operation_id="op_a")
    aborted = service.start_operation(SESSION_ID, name="b", operation_id="op_b")

    service.fail_operation(SESSION_ID, failed, error="boom", error_code="tool_error")
    service.abort_operation(SESSION_ID, aborted, reason="operator")

    state = service.load_state(SESSION_ID)
    assert state.operations["op_a"].status == "failed"
    assert state.operations["op_b"].status == "aborted"
    assert state.unfinished_operation_ids == []


def test_start_operation_generates_an_id(service: SessionService) -> None:
    service.create_session(session_id=SESSION_ID)

    operation_id = service.start_operation(SESSION_ID, name="chat")

    assert operation_id.startswith("op_")


def test_create_lane_shows_up_in_lanes(service: SessionService) -> None:
    service.create_session(session_id=SESSION_ID)
    service.create_lane(SESSION_ID, "side", parent_lane=DEFAULT_LANE, reason="review")

    lanes = service.lanes(SESSION_ID)

    assert lanes["side"].parent_lane == DEFAULT_LANE


def test_suspended_operations_lists_only_suspended_ones(service: SessionService) -> None:
    service.create_session(session_id=SESSION_ID)
    service.start_operation(SESSION_ID, name="chat", operation_id=OPERATION_ID)
    _open_write_call(service.store)
    service.recovery.suspend(SESSION_ID, OPERATION_ID, reason="needs a human")

    suspended = service.suspended_operations(SESSION_ID)

    assert [operation.operation_id for operation in suspended] == [OPERATION_ID]


# ---------------------------------------------------------------------- startup


def test_startup_on_a_new_session_opens_an_operation(service: SessionService) -> None:
    report = service.startup(session_id=SESSION_ID, operation_name="agent_run", title="fresh")

    assert report.created_session is True
    assert report.operation_id is not None
    assert report.blocked is False
    assert report.recovery is None
    assert report.lane_id == DEFAULT_LANE


def test_startup_can_skip_opening_an_operation(service: SessionService) -> None:
    report = service.startup(session_id=SESSION_ID, start_operation=False)

    assert report.operation_id is None
    assert report.blocked is False


def test_startup_on_a_clean_existing_session_opens_a_second_operation(
    service: SessionService,
) -> None:
    first = service.startup(session_id=SESSION_ID)
    assert first.operation_id is not None
    service.finish_operation(SESSION_ID, first.operation_id)

    second = service.startup(session_id=SESSION_ID)

    assert second.created_session is False
    assert second.operation_id is not None
    assert second.operation_id != first.operation_id
    assert second.blocked is False
    assert second.recovery is not None
    assert second.recovery.operations == []


def test_startup_blocks_and_opens_nothing_when_a_call_needs_confirmation(
    service: SessionService,
) -> None:
    """The plan's ordering: notice the crash before scheduling new work.

    Opening an operation here would bury the question under fresh events.
    """

    service.create_session(session_id=SESSION_ID)
    service.start_operation(SESSION_ID, name="chat", operation_id=OPERATION_ID)
    _open_write_call(service.store)

    report = service.startup(session_id=SESSION_ID)

    assert report.blocked is True
    assert report.operation_id is None
    assert report.suspended_operation_ids == [OPERATION_ID]
    assert report.recovery is not None
    assert report.recovery.command() == f"atlas resume {SESSION_ID} --confirm c1"


def test_startup_writes_the_suspension_it_reports(service: SessionService) -> None:
    """The report is re-planned after suspending, so status reflects what was written."""

    service.create_session(session_id=SESSION_ID)
    service.start_operation(SESSION_ID, name="chat", operation_id=OPERATION_ID)
    _open_write_call(service.store)

    report = service.startup(session_id=SESSION_ID)

    assert report.recovery is not None
    assert report.recovery.operations[0].status == SUSPENDED_STATUS
    types = [event.event_type for event in service.store.read_events(SESSION_ID)]
    assert EventType.OPERATION_SUSPENDED in types
    assert types.count(EventType.OPERATION_STARTED) == 1


def test_startup_is_idempotent_across_repeated_crashes(service: SessionService) -> None:
    """A second restart must not append a second suspension for the same question."""

    service.create_session(session_id=SESSION_ID)
    service.start_operation(SESSION_ID, name="chat", operation_id=OPERATION_ID)
    _open_write_call(service.store)

    service.startup(session_id=SESSION_ID)
    before = service.store.load_state(SESSION_ID).last_seq
    second = service.startup(session_id=SESSION_ID)

    assert second.blocked is True
    assert service.store.load_state(SESSION_ID).last_seq == before


def test_startup_does_not_block_on_a_replayable_read(service: SessionService) -> None:
    """An idempotent read is not a question, so work continues."""

    service.create_session(session_id=SESSION_ID)
    service.start_operation(SESSION_ID, name="chat", operation_id=OPERATION_ID)
    _open_read_call(service.store)

    report = service.startup(session_id=SESSION_ID)

    assert report.blocked is False
    assert report.operation_id is not None


def test_startup_summary_carries_the_next_command(service: SessionService) -> None:
    service.create_session(session_id=SESSION_ID)
    service.start_operation(SESSION_ID, name="chat", operation_id=OPERATION_ID)
    _open_write_call(service.store)

    summary = service.startup(session_id=SESSION_ID).summary()

    assert summary["blocked"] is True
    assert summary["suspended_operations"] == [OPERATION_ID]
    assert summary["recovery"]["command"] == f"atlas resume {SESSION_ID} --confirm c1"


def test_startup_syncs_the_index_tables(service: SessionService) -> None:
    report = service.startup(session_id=SESSION_ID)

    rows = service.repository.operations(SESSION_ID)
    assert [row.id for row in rows] == [report.operation_id]


# ------------------------------------------------------- require_resumable / resume


def test_require_resumable_refuses_a_session_with_nothing_open(service: SessionService) -> None:
    report = service.startup(session_id=SESSION_ID)
    assert report.operation_id is not None
    service.finish_operation(SESSION_ID, report.operation_id)

    with pytest.raises(RecoveryError) as excinfo:
        service.require_resumable(SESSION_ID)

    assert excinfo.value.details["session_id"] == SESSION_ID


def test_require_resumable_accepts_an_unfinished_operation(service: SessionService) -> None:
    service.create_session(session_id=SESSION_ID)
    service.start_operation(SESSION_ID, name="chat", operation_id=OPERATION_ID)
    _open_write_call(service.store)

    plan = service.require_resumable(SESSION_ID)

    assert plan.unfinished_operation_ids == [OPERATION_ID]


def test_resume_with_the_confirmation_clears_the_block(service: SessionService) -> None:
    service.create_session(session_id=SESSION_ID)
    service.start_operation(SESSION_ID, name="chat", operation_id=OPERATION_ID)
    _open_write_call(service.store)
    service.startup(session_id=SESSION_ID)

    plan = service.resume(SESSION_ID, confirm=["c1"])

    assert plan.needs_confirmation is False
    assert service.repository.operations(SESSION_ID, status=SUSPENDED_STATUS) == []


def test_abort_closes_the_operation_and_refreshes_the_index(service: SessionService) -> None:
    service.create_session(session_id=SESSION_ID)
    service.start_operation(SESSION_ID, name="chat", operation_id=OPERATION_ID)
    _open_write_call(service.store)

    written = service.abort(SESSION_ID, reason="operator gave up")

    assert [event.operation_id for event in written] == [OPERATION_ID]
    rows = service.repository.operations(SESSION_ID)
    assert [row.status for row in rows] == ["aborted"]
