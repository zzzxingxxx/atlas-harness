"""Lane and branch navigation.

Two properties matter here. Lanes share the session's read-only context but keep
their mutable state — queues and tool calls — to themselves. And navigation is
append-only: there is no operation on ``BranchService`` that removes history, and
these tests assert that forking and switching only ever add events.
"""

from __future__ import annotations

import pytest

from atlas_harness.events import EventStore, EventType
from atlas_harness.kernel.errors import EventValidationError
from atlas_harness.session import (
    BranchService,
    lane_queue_message_ids,
    lane_tool_call_ids,
)

SESSION_ID = "ses_lanes"


@pytest.fixture
def branches(store: EventStore) -> BranchService:
    store.append_new(
        EventType.SESSION_CREATED,
        session_id=SESSION_ID,
        payload={"title": "lanes", "workspace_root": "/tmp/ws"},
    )
    return BranchService(store)


def _operation(store: EventStore, operation_id: str, lane: str) -> None:
    store.append_new(
        EventType.OPERATION_STARTED,
        session_id=SESSION_ID,
        lane_id=lane,
        operation_id=operation_id,
        payload={"name": "work"},
    )


def _tool_call(store: EventStore, operation_id: str, lane: str, call_id: str) -> None:
    store.append_new(
        EventType.TOOL_STARTED,
        session_id=SESSION_ID,
        lane_id=lane,
        operation_id=operation_id,
        payload={"tool_name": "read_file", "call_id": call_id, "risk": "read", "idempotent": True},
    )


def _enqueue(store: EventStore, operation_id: str, lane: str, message_id: str) -> None:
    store.append_new(
        EventType.QUEUE_MESSAGE_ENQUEUED,
        session_id=SESSION_ID,
        lane_id=lane,
        operation_id=operation_id,
        payload={"queue": "user", "message_id": message_id, "content": "hi"},
    )


# ------------------------------------------------------------------- basics


def test_the_main_lane_exists_from_the_first_event(branches: BranchService) -> None:
    views = branches.lanes(SESSION_ID)

    assert [view.lane_id for view in views] == ["main"]
    assert views[0].is_root is True
    assert branches.current_lane(SESSION_ID) == "main"


def test_lanes_share_the_session_workspace(branches: BranchService) -> None:
    """Read-only context is shared. A lane never gets a workspace of its own."""

    branches.create_lane(SESSION_ID, "feature")

    roots = {view.lane_id: view.workspace_root for view in branches.lanes(SESSION_ID)}

    assert roots == {"main": "/tmp/ws", "feature": "/tmp/ws"}


def test_create_lane_records_its_parent(branches: BranchService) -> None:
    branches.create_lane(SESSION_ID, "feature", reason="try an alternative")

    lane = branches.lane(SESSION_ID, "feature")

    assert lane.parent_lane == "main"
    assert lane.is_root is False


def test_create_lane_rejects_a_duplicate(branches: BranchService) -> None:
    branches.create_lane(SESSION_ID, "feature")

    with pytest.raises(EventValidationError) as excinfo:
        branches.create_lane(SESSION_ID, "feature")

    assert excinfo.value.details["lane"] == "feature"


def test_create_lane_rejects_an_unknown_parent(branches: BranchService) -> None:
    with pytest.raises(EventValidationError):
        branches.create_lane(SESSION_ID, "feature", parent_lane="ghost")


def test_lane_rejects_an_unknown_id(branches: BranchService) -> None:
    with pytest.raises(EventValidationError) as excinfo:
        branches.lane(SESSION_ID, "ghost")

    assert excinfo.value.details["lane"] == "ghost"


# ------------------------------------------------------------------ branching


def test_branch_records_the_seq_it_forked_from(branches: BranchService, store: EventStore) -> None:
    _operation(store, "op1", "main")
    seq_before = store.load_state(SESSION_ID).last_seq

    branches.create_branch(SESSION_ID, "alt", label="alternative")

    lane = branches.lane(SESSION_ID, "alt")
    assert lane.forked_from_seq == seq_before
    assert lane.label == "alternative"
    assert lane.parent_lane == "main"


def test_branch_can_fork_from_an_earlier_seq(branches: BranchService, store: EventStore) -> None:
    _operation(store, "op1", "main")
    _operation(store, "op2", "main")

    branches.create_branch(SESSION_ID, "alt", from_seq=1)

    assert branches.lane(SESSION_ID, "alt").forked_from_seq == 1


def test_branch_refuses_a_seq_the_log_does_not_contain(branches: BranchService) -> None:
    """Forking from a future seq would fabricate a history that never happened."""

    with pytest.raises(EventValidationError) as excinfo:
        branches.create_branch(SESSION_ID, "alt", from_seq=999)

    assert excinfo.value.details["from_seq"] == 999
    assert excinfo.value.details["last_valid_seq"] == 1


def test_branch_refuses_a_negative_seq(branches: BranchService) -> None:
    with pytest.raises(EventValidationError):
        branches.create_branch(SESSION_ID, "alt", from_seq=-1)


def test_branching_only_appends(branches: BranchService, store: EventStore) -> None:
    """The whole navigation API is additive. Nothing it does can shorten the log."""

    _operation(store, "op1", "main")
    before = store.read_events(SESSION_ID)

    branches.create_branch(SESSION_ID, "alt", from_seq=1)
    branches.switch(SESSION_ID, "alt")
    branches.switch(SESSION_ID, "main")

    after = store.read_events(SESSION_ID)
    assert [event.seq for event in after[: len(before)]] == [event.seq for event in before]
    assert len(after) == len(before) + 3


def test_branch_service_has_no_delete() -> None:
    """A history you can edit is not an audit trail. Guard against adding one."""

    removals = [
        name
        for name in dir(BranchService)
        if any(word in name for word in ("delete", "remove", "drop", "prune", "rewrite"))
    ]

    assert removals == []


# ------------------------------------------------------------------ switching


def test_switch_moves_the_current_lane(branches: BranchService) -> None:
    branches.create_lane(SESSION_ID, "feature")

    branches.switch(SESSION_ID, "feature")

    assert branches.current_lane(SESSION_ID) == "feature"


def test_switch_records_where_it_came_from(branches: BranchService) -> None:
    branches.create_lane(SESSION_ID, "feature")

    event = branches.switch(SESSION_ID, "feature")

    payload = event.payload.model_dump(mode="json")
    assert payload["from_lane"] == "main"
    assert payload["lane"] == "feature"


def test_switch_rejects_an_unknown_lane(branches: BranchService) -> None:
    with pytest.raises(EventValidationError):
        branches.switch(SESSION_ID, "ghost")


def test_ancestry_runs_root_first(branches: BranchService) -> None:
    branches.create_lane(SESSION_ID, "feature")
    branches.create_lane(SESSION_ID, "nested", parent_lane="feature")

    assert branches.ancestry(SESSION_ID, "nested") == ["main", "feature", "nested"]
    assert branches.ancestry(SESSION_ID, "main") == ["main"]


def test_lane_events_returns_only_that_lane(branches: BranchService, store: EventStore) -> None:
    branches.create_lane(SESSION_ID, "feature")
    _operation(store, "op_main", "main")
    _operation(store, "op_feature", "feature")

    types = [event.event_type for event in branches.lane_events(SESSION_ID, "feature")]

    assert types == [EventType.LANE_CREATED, EventType.OPERATION_STARTED]


# ------------------------------------------------------- isolated mutable state


def test_tool_state_is_per_lane(branches: BranchService, store: EventStore) -> None:
    branches.create_lane(SESSION_ID, "feature")
    _operation(store, "op_main", "main")
    _operation(store, "op_feature", "feature")
    _tool_call(store, "op_main", "main", "c_main")
    _tool_call(store, "op_feature", "feature", "c_feature")

    state = store.load_state(SESSION_ID)

    assert lane_tool_call_ids(state, "main") == ["c_main"]
    assert lane_tool_call_ids(state, "feature") == ["c_feature"]


def test_queues_are_isolated_per_lane(branches: BranchService, store: EventStore) -> None:
    branches.create_lane(SESSION_ID, "feature")
    _operation(store, "op_main", "main")
    _operation(store, "op_feature", "feature")
    _enqueue(store, "op_main", "main", "m1")
    _enqueue(store, "op_feature", "feature", "m2")

    state = store.load_state(SESSION_ID)

    assert list(lane_queue_message_ids(state, "main", "user")) == ["m1"]
    assert list(lane_queue_message_ids(state, "feature", "user")) == ["m2"]


def test_consumed_messages_leave_the_lane_queue(branches: BranchService, store: EventStore) -> None:
    _operation(store, "op_main", "main")
    _enqueue(store, "op_main", "main", "m1")
    store.append_new(
        EventType.QUEUE_MESSAGE_CONSUMED,
        session_id=SESSION_ID,
        operation_id="op_main",
        payload={"queue": "user", "message_id": "m1", "iteration": 1},
    )

    state = store.load_state(SESSION_ID)

    assert list(lane_queue_message_ids(state, "main", "user")) == []
