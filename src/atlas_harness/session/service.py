"""Session, Operation and Lane lifecycle.

This is the layer the transport and the agent share. It owns the startup sequence
from the plan: open the session, open the operation, read the latest snapshot,
replay what came after it, and report anything left suspended — in that order, so a
crashed process is always noticed before new work is scheduled.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict

from atlas_harness.events.models import (
    DEFAULT_LANE,
    SUSPENDED_STATUS,
    Event,
    EventType,
    LaneCreated,
    OperationAborted,
    OperationFailed,
    OperationFinished,
    OperationStarted,
    SessionCreated,
)
from atlas_harness.events.reducer import LaneState, OperationState, SessionState
from atlas_harness.events.store import EventStore
from atlas_harness.kernel.errors import RecoveryError
from atlas_harness.kernel.ids import new_id, validate_session_id
from atlas_harness.session.recovery import RecoveryPlan, RecoveryService
from atlas_harness.session.repository import SessionRepository


class StartupReport(BaseModel):
    """What the startup sequence found. `blocked` means do not schedule new work."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    operation_id: str | None = None
    lane_id: str = DEFAULT_LANE
    created_session: bool = False
    recovery: RecoveryPlan | None = None

    @property
    def suspended_operation_ids(self) -> list[str]:
        if self.recovery is None:
            return []
        return [
            operation.operation_id
            for operation in self.recovery.operations
            if operation.needs_confirmation
        ]

    @property
    def blocked(self) -> bool:
        """True when a human decision is owed before the session may continue."""

        return bool(self.suspended_operation_ids)

    def summary(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "operation_id": self.operation_id,
            "lane": self.lane_id,
            "created_session": self.created_session,
            "blocked": self.blocked,
            "suspended_operations": self.suspended_operation_ids,
            "recovery": None if self.recovery is None else self.recovery.summary(),
        }


class SessionService:
    """Create and query sessions, lanes and operations."""

    def __init__(
        self,
        store: EventStore,
        *,
        recovery: RecoveryService | None = None,
        repository: SessionRepository | None = None,
    ) -> None:
        self._store = store
        self._recovery = recovery or RecoveryService(store)
        self._repository = repository or SessionRepository(store)

    @property
    def store(self) -> EventStore:
        return self._store

    @property
    def recovery(self) -> RecoveryService:
        return self._recovery

    @property
    def repository(self) -> SessionRepository:
        return self._repository

    # ------------------------------------------------------------------ sessions

    def create_session(
        self,
        *,
        session_id: str | None = None,
        title: str | None = None,
        workspace_root: str | None = None,
    ) -> str:
        target = session_id or self._store.new_session_id()
        validate_session_id(target)
        if not self._store.session_exists(target):
            self._store.append_new(
                EventType.SESSION_CREATED,
                session_id=target,
                payload=SessionCreated(title=title, workspace_root=workspace_root),
            )
        return target

    def load_state(self, session_id: str) -> SessionState:
        return self._store.load_state(session_id)

    def sync_index(self, session_id: str) -> SessionState:
        """Refresh the lane / operation / tool-call / snapshot rows from the log."""

        return self._repository.sync(session_id)

    # --------------------------------------------------------------------- lanes

    def create_lane(
        self,
        session_id: str,
        lane: str,
        *,
        parent_lane: str | None = None,
        reason: str | None = None,
    ) -> Event:
        return self._store.append_new(
            EventType.LANE_CREATED,
            session_id=session_id,
            lane_id=lane,
            payload=LaneCreated(lane=lane, parent_lane=parent_lane, reason=reason),
        )

    def lanes(self, session_id: str) -> dict[str, LaneState]:
        return self.load_state(session_id).lanes

    # ---------------------------------------------------------------- operations

    def start_operation(
        self,
        session_id: str,
        *,
        name: str,
        lane_id: str = DEFAULT_LANE,
        operation_id: str | None = None,
        deadline_ms: int | None = None,
    ) -> str:
        target = operation_id or new_id("op")
        self._store.append_new(
            EventType.OPERATION_STARTED,
            session_id=session_id,
            lane_id=lane_id,
            operation_id=target,
            payload=OperationStarted(name=name, deadline_ms=deadline_ms),
        )
        return target

    def finish_operation(
        self,
        session_id: str,
        operation_id: str,
        *,
        result: Any = None,
        lane_id: str = DEFAULT_LANE,
    ) -> Event:
        return self._store.append_new(
            EventType.OPERATION_FINISHED,
            session_id=session_id,
            lane_id=lane_id,
            operation_id=operation_id,
            payload=OperationFinished(result=result),
        )

    def fail_operation(
        self,
        session_id: str,
        operation_id: str,
        *,
        error: str,
        error_code: str | None = None,
        lane_id: str = DEFAULT_LANE,
    ) -> Event:
        return self._store.append_new(
            EventType.OPERATION_FAILED,
            session_id=session_id,
            lane_id=lane_id,
            operation_id=operation_id,
            payload=OperationFailed(error=error, error_code=error_code),
        )

    def abort_operation(
        self,
        session_id: str,
        operation_id: str,
        *,
        reason: str | None = None,
        lane_id: str = DEFAULT_LANE,
    ) -> Event:
        return self._store.append_new(
            EventType.OPERATION_ABORTED,
            session_id=session_id,
            lane_id=lane_id,
            operation_id=operation_id,
            payload=OperationAborted(reason=reason),
        )

    def operations(self, session_id: str, *, status: str | None = None) -> list[OperationState]:
        operations = list(self.load_state(session_id).operations.values())
        if status is None:
            return operations
        return [operation for operation in operations if operation.status == status]

    def suspended_operations(self, session_id: str) -> list[OperationState]:
        return self.operations(session_id, status=SUSPENDED_STATUS)

    # ------------------------------------------------------------------- startup

    def startup(
        self,
        *,
        session_id: str | None = None,
        operation_name: str = "agent_run",
        lane_id: str = DEFAULT_LANE,
        title: str | None = None,
        workspace_root: str | None = None,
        start_operation: bool = True,
    ) -> StartupReport:
        """Run the plan's startup sequence.

        A brand new session has nothing to recover, so the operation opens straight
        away. An existing session is replayed first, and if anything is waiting on a
        confirmation no new operation is opened at all — the caller has to resolve the
        suspension before the session accepts more work.
        """

        existed = session_id is not None and self._store.session_exists(session_id)
        target = self.create_session(
            session_id=session_id, title=title, workspace_root=workspace_root
        )
        if not existed:
            operation_id = (
                self.start_operation(target, name=operation_name, lane_id=lane_id)
                if start_operation
                else None
            )
            self._repository.sync(target)
            return StartupReport(
                session_id=target,
                operation_id=operation_id,
                lane_id=lane_id,
                created_session=True,
            )

        plan = self._recovery.plan(target)
        self._recovery.suspend_from_plan(plan)
        if plan.needs_confirmation:
            # Re-plan so the report shows the suspensions that were just written.
            plan = self._recovery.plan(target)
            self._repository.sync(target)
            return StartupReport(
                session_id=target,
                operation_id=None,
                lane_id=lane_id,
                created_session=False,
                recovery=plan,
            )

        operation_id = (
            self.start_operation(target, name=operation_name, lane_id=lane_id)
            if start_operation
            else None
        )
        self._repository.sync(target)
        return StartupReport(
            session_id=target,
            operation_id=operation_id,
            lane_id=lane_id,
            created_session=False,
            recovery=plan,
        )

    def require_resumable(self, session_id: str) -> RecoveryPlan:
        """Plan a recovery, refusing when nothing is actually unfinished."""

        plan = self._recovery.plan(session_id)
        if not plan.operations:
            raise RecoveryError(
                "session has no unfinished operation to resume",
                details={"session_id": session_id, "last_valid_seq": plan.last_valid_seq},
            )
        return plan

    def resume(self, session_id: str, *, confirm: Sequence[str] = ()) -> RecoveryPlan:
        plan = self._recovery.resume(session_id, confirm=confirm)
        self._repository.sync(session_id)
        return plan

    def abort(self, session_id: str, *, reason: str = "aborted by operator") -> list[Event]:
        written = self._recovery.abort(session_id, reason=reason)
        self._repository.sync(session_id)
        return written
