"""Lane and branch navigation.

A lane is a line of work inside one session. Lanes share the session's read-only
context — the workspace root, the settings, the event log itself — and keep their own
mutable state: the operation queue and the tool calls belonging to their operations.

Navigation is append-only. Switching lanes, or forking a new one at an earlier seq,
writes an event saying so; it never rewrites or removes what came before. There is
deliberately no delete: the log is the audit trail, and a history you can edit is not
one.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from atlas_harness.events.models import (
    DEFAULT_LANE,
    BranchCreated,
    BranchSwitched,
    Event,
    EventType,
    LaneCreated,
)
from atlas_harness.events.reducer import LaneState, SessionState
from atlas_harness.events.store import EventStore
from atlas_harness.kernel.errors import EventValidationError


class LaneView(BaseModel):
    """One lane plus the read-only session context it shares."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    lane_id: str
    status: str = "idle"
    parent_lane: str | None = None
    forked_from_seq: int | None = None
    label: str | None = None
    operation_ids: list[str] = []
    current_operation_id: str | None = None
    workspace_root: str | None = None
    """Shared, read-only. Lanes never get their own workspace."""

    @property
    def is_root(self) -> bool:
        return self.parent_lane is None


class BranchService:
    """Create lanes, fork branches and record navigation."""

    def __init__(self, store: EventStore) -> None:
        self._store = store

    def lanes(self, session_id: str) -> list[LaneView]:
        state = self._store.load_state(session_id)
        return [self._view(state, lane) for lane in state.lanes.values()]

    def lane(self, session_id: str, lane_id: str) -> LaneView:
        state = self._store.load_state(session_id)
        lane = state.lanes.get(lane_id)
        if lane is None:
            raise EventValidationError(
                "unknown lane",
                details={"session_id": session_id, "lane": lane_id},
            )
        return self._view(state, lane)

    def current_lane(self, session_id: str) -> str:
        return self._store.load_state(session_id).current_lane_id

    def create_lane(
        self,
        session_id: str,
        lane_id: str,
        *,
        parent_lane: str | None = None,
        reason: str | None = None,
    ) -> Event:
        state = self._store.load_state(session_id)
        if lane_id in state.lanes:
            raise EventValidationError(
                "lane already exists",
                details={"session_id": session_id, "lane": lane_id},
            )
        parent = parent_lane or state.current_lane_id
        self._require_lane(state, parent)
        return self._store.append_new(
            EventType.LANE_CREATED,
            session_id=session_id,
            lane_id=lane_id,
            payload=LaneCreated(lane=lane_id, parent_lane=parent, reason=reason),
        )

    def create_branch(
        self,
        session_id: str,
        lane_id: str,
        *,
        from_seq: int | None = None,
        parent_lane: str | None = None,
        label: str | None = None,
    ) -> Event:
        """Fork a new lane off an existing one at a seq the log actually contains."""

        state = self._store.load_state(session_id)
        if lane_id in state.lanes:
            raise EventValidationError(
                "lane already exists",
                details={"session_id": session_id, "lane": lane_id},
            )
        parent = parent_lane or state.current_lane_id
        self._require_lane(state, parent)
        seq = state.last_seq if from_seq is None else from_seq
        if seq < 0 or seq > state.last_seq:
            raise EventValidationError(
                "cannot branch from a seq the log does not contain",
                details={
                    "session_id": session_id,
                    "from_seq": seq,
                    "last_valid_seq": state.last_seq,
                },
            )
        return self._store.append_new(
            EventType.BRANCH_CREATED,
            session_id=session_id,
            lane_id=lane_id,
            payload=BranchCreated(
                lane=lane_id,
                parent_lane=parent,
                from_seq=seq,
                label=label,
            ),
        )

    def switch(self, session_id: str, lane_id: str) -> Event:
        """Point the session at another existing lane. History is untouched."""

        state = self._store.load_state(session_id)
        self._require_lane(state, lane_id)
        return self._store.append_new(
            EventType.BRANCH_SWITCHED,
            session_id=session_id,
            lane_id=lane_id,
            payload=BranchSwitched(
                lane=lane_id,
                from_lane=state.current_lane_id,
                at_seq=state.last_seq,
            ),
        )

    def ancestry(self, session_id: str, lane_id: str) -> list[str]:
        """Lane ids from the root down to ``lane_id``."""

        state = self._store.load_state(session_id)
        self._require_lane(state, lane_id)
        chain: list[str] = []
        seen: set[str] = set()
        cursor: str | None = lane_id
        while cursor is not None and cursor not in seen:
            seen.add(cursor)
            chain.append(cursor)
            lane = state.lanes.get(cursor)
            cursor = lane.parent_lane if lane is not None else None
        chain.reverse()
        return chain

    def lane_events(self, session_id: str, lane_id: str) -> list[Event]:
        """Events written on one lane, in order. Read-only, like everything else."""

        return [event for event in self._store.iter_events(session_id) if event.lane_id == lane_id]

    def _require_lane(self, state: SessionState, lane_id: str) -> None:
        if lane_id in state.lanes:
            return
        if lane_id == DEFAULT_LANE:
            # The main lane exists implicitly from the first event onward.
            return
        raise EventValidationError(
            "unknown lane",
            details={"session_id": state.session_id, "lane": lane_id},
        )

    def _view(self, state: SessionState, lane: LaneState) -> LaneView:
        return LaneView(
            session_id=state.session_id,
            lane_id=lane.lane_id,
            status=lane.status,
            parent_lane=lane.parent_lane,
            forked_from_seq=lane.forked_from_seq,
            label=lane.label,
            operation_ids=list(lane.operation_ids),
            current_operation_id=lane.current_operation_id,
            workspace_root=state.workspace_root,
        )


def lane_tool_call_ids(state: SessionState, lane_id: str) -> list[str]:
    """Tool calls owned by one lane. Tool state is per-lane, never shared."""

    return [
        call_id
        for operation in state.operations.values()
        if operation.lane_id == lane_id
        for call_id in operation.tool_calls
    ]


def lane_queue_message_ids(state: SessionState, lane_id: str, queue: str) -> Sequence[str]:
    """Unconsumed queue messages for one lane. Queues are isolated per lane."""

    return [
        message.message_id
        for operation in state.operations.values()
        if operation.lane_id == lane_id
        for message in operation.pending_queue_messages(queue)
    ]
