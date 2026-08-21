import pytest

from atlas_harness.events import Event, EventType, Reducer, replay
from atlas_harness.events.models import DEFAULT_LANE
from atlas_harness.kernel import EventValidationError, FrozenClock, IdFactory

SESSION_ID = "ses_r"
OPERATION_ID = "op_r"


@pytest.fixture
def factory() -> IdFactory:
    return IdFactory(FrozenClock(1_000))


def event(
    factory: IdFactory,
    event_type: EventType,
    seq: int,
    payload: dict[str, object] | None = None,
    *,
    session_id: str = SESSION_ID,
    operation_id: str | None = None,
    lane_id: str = DEFAULT_LANE,
) -> Event:
    return Event.create(
        event_type,
        session_id=session_id,
        seq=seq,
        payload=payload,
        factory=factory,
        lane_id=lane_id,
        operation_id=operation_id,
    )


def script(factory: IdFactory) -> list[Event]:
    return [
        event(factory, EventType.SESSION_CREATED, 1, {"title": "t", "workspace_root": "/ws"}),
        event(factory, EventType.OPERATION_STARTED, 2, {"name": "chat"}, operation_id=OPERATION_ID),
        event(
            factory,
            EventType.MODEL_REQUESTED,
            3,
            {"provider": "stub", "model": "m1"},
            operation_id=OPERATION_ID,
        ),
        event(
            factory,
            EventType.APPROVAL_REQUESTED,
            4,
            {"approval_id": "ap1", "reason": "write"},
            operation_id=OPERATION_ID,
        ),
        event(
            factory,
            EventType.APPROVAL_RESOLVED,
            5,
            {"approval_id": "ap1", "approved": True},
            operation_id=OPERATION_ID,
        ),
        event(
            factory,
            EventType.TOOL_STARTED,
            6,
            {"tool_name": "fs_write", "call_id": "c1", "arguments": {"path": "a"}},
            operation_id=OPERATION_ID,
        ),
        event(
            factory,
            EventType.TOOL_RESULT,
            7,
            {"tool_name": "fs_write", "call_id": "c1", "success": True, "output": "ok"},
            operation_id=OPERATION_ID,
        ),
        event(
            factory, EventType.ASSISTANT_MESSAGE, 8, {"content": "done"}, operation_id=OPERATION_ID
        ),
        event(factory, EventType.SNAPSHOT_CREATED, 9, {"snapshot_id": "snap1"}),
        event(
            factory, EventType.OPERATION_FINISHED, 10, {"result": "ok"}, operation_id=OPERATION_ID
        ),
    ]


def test_full_projection(factory: IdFactory) -> None:
    state = replay(script(factory))

    assert state.status == "active"
    assert state.title == "t"
    assert state.workspace_root == "/ws"
    assert state.last_seq == 10
    assert state.event_count == 10
    assert state.messages == ["done"]
    assert state.approvals == {"ap1": True}
    assert state.pending_approval_ids == []
    assert state.snapshots == ["snap1"]

    lane = state.lanes[DEFAULT_LANE]
    assert lane.status == "idle"
    assert lane.current_operation_id is None
    assert lane.operation_ids == [OPERATION_ID]

    operation = state.operations[OPERATION_ID]
    assert operation.status == "finished"
    assert operation.result == "ok"
    assert operation.provider == "stub"
    assert operation.model == "m1"
    assert operation.model_requests == 1
    assert operation.messages == ["done"]
    assert operation.approvals == {"ap1": True}
    assert operation.open_tool_call_ids == []
    assert state.open_operation_ids == []

    call = operation.tool_calls["c1"]
    assert call.status == "succeeded"
    assert call.output == "ok"
    assert call.arguments == {"path": "a"}
    assert call.finished_at_ms == 1_000


def test_lane_is_running_until_a_terminal_event(factory: IdFactory) -> None:
    events = script(factory)[:6]

    state = replay(events)

    lane = state.lanes[DEFAULT_LANE]
    assert lane.status == "running"
    assert lane.current_operation_id == OPERATION_ID
    assert state.open_operation_ids == [OPERATION_ID]
    assert state.pending_approval_ids == []


def test_failed_and_aborted_operations(factory: IdFactory) -> None:
    events = [
        event(factory, EventType.SESSION_CREATED, 1),
        event(factory, EventType.OPERATION_STARTED, 2, operation_id="op_a"),
        event(
            factory,
            EventType.OPERATION_FAILED,
            3,
            {"error": "boom", "error_code": "tool_error"},
            operation_id="op_a",
        ),
        event(factory, EventType.OPERATION_STARTED, 4, operation_id="op_b"),
        event(
            factory,
            EventType.OPERATION_ABORTED,
            5,
            {"reason": "cancelled"},
            operation_id="op_b",
        ),
    ]

    state = replay(events)

    assert state.operations["op_a"].status == "failed"
    assert state.operations["op_a"].error == "boom"
    assert state.operations["op_b"].status == "aborted"
    assert state.operations["op_b"].error == "cancelled"
    assert state.lanes[DEFAULT_LANE].status == "idle"


def test_failed_tool_result(factory: IdFactory) -> None:
    events = [
        event(factory, EventType.SESSION_CREATED, 1),
        event(factory, EventType.OPERATION_STARTED, 2, operation_id="op_a"),
        event(
            factory,
            EventType.TOOL_RESULT,
            3,
            {"tool_name": "fs_write", "call_id": "c9", "success": False, "error": "denied"},
            operation_id="op_a",
        ),
    ]

    call = replay(events).operations["op_a"].tool_calls["c9"]

    assert call.status == "failed"
    assert call.error == "denied"


def test_parallel_lanes_are_tracked_separately(factory: IdFactory) -> None:
    events = [
        event(factory, EventType.SESSION_CREATED, 1),
        event(factory, EventType.OPERATION_STARTED, 2, operation_id="op_a"),
        event(factory, EventType.OPERATION_STARTED, 3, operation_id="op_b", lane_id="lane_b"),
        event(factory, EventType.OPERATION_FINISHED, 4, operation_id="op_a"),
    ]

    state = replay(events)

    assert state.lanes[DEFAULT_LANE].status == "idle"
    assert state.lanes["lane_b"].status == "running"
    assert state.lanes["lane_b"].current_operation_id == "op_b"


def test_duplicate_seq_is_rejected(factory: IdFactory) -> None:
    reducer = Reducer(SESSION_ID)
    reducer.apply(event(factory, EventType.SESSION_CREATED, 1))

    with pytest.raises(EventValidationError) as excinfo:
        reducer.apply(event(factory, EventType.ASSISTANT_MESSAGE, 1, {"content": "x"}))

    assert excinfo.value.details["expected_seq"] == 2
    assert excinfo.value.details["actual_seq"] == 1
    assert excinfo.value.details["last_valid_seq"] == 1


def test_missing_seq_is_rejected(factory: IdFactory) -> None:
    reducer = Reducer(SESSION_ID)
    reducer.apply(event(factory, EventType.SESSION_CREATED, 1))

    with pytest.raises(EventValidationError) as excinfo:
        reducer.apply(event(factory, EventType.ASSISTANT_MESSAGE, 3, {"content": "x"}))

    assert excinfo.value.details["expected_seq"] == 2
    assert excinfo.value.details["last_valid_seq"] == 1


def test_out_of_order_events_are_rejected(factory: IdFactory) -> None:
    events = script(factory)
    reordered = [events[0], events[2], events[1]]

    with pytest.raises(EventValidationError):
        replay(reordered)


def test_foreign_session_is_rejected(factory: IdFactory) -> None:
    reducer = Reducer(SESSION_ID)

    with pytest.raises(EventValidationError) as excinfo:
        reducer.apply(event(factory, EventType.SESSION_CREATED, 1, session_id="ses_other"))

    assert excinfo.value.details == {"expected": SESSION_ID, "actual": "ses_other"}


def test_unknown_operation_is_rejected(factory: IdFactory) -> None:
    events = [
        event(factory, EventType.SESSION_CREATED, 1),
        event(factory, EventType.ASSISTANT_MESSAGE, 2, {"content": "x"}, operation_id="op_ghost"),
    ]

    with pytest.raises(EventValidationError) as excinfo:
        replay(events)

    assert excinfo.value.details == {"operation_id": "op_ghost", "seq": 2}


def test_non_strict_reducer_tolerates_gaps(factory: IdFactory) -> None:
    reducer = Reducer(SESSION_ID, strict_seq=False)
    reducer.apply(event(factory, EventType.SESSION_CREATED, 1))
    reducer.apply(event(factory, EventType.ASSISTANT_MESSAGE, 5, {"content": "x"}))

    assert reducer.state.last_seq == 5

    with pytest.raises(EventValidationError):
        reducer.apply(event(factory, EventType.ASSISTANT_MESSAGE, 5, {"content": "y"}))


def test_replay_requires_a_session_id_for_an_empty_stream() -> None:
    with pytest.raises(EventValidationError):
        replay([])

    assert replay([], session_id=SESSION_ID).session_id == SESSION_ID


def test_state_hash_matches_for_identical_streams(factory: IdFactory) -> None:
    events = script(factory)

    assert replay(events).state_hash() == replay(list(events)).state_hash()


def test_state_hash_changes_with_content(factory: IdFactory) -> None:
    events = script(factory)
    shorter = events[:-1]

    assert replay(events).state_hash() != replay(shorter).state_hash()
