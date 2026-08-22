"""Snapshots, crash recovery and the suspended state.

The rule this module exists to enforce: a tool that already produced a ``tool_result``
is never executed again, and a tool whose side effect cannot be proven safe is never
retried without a human saying so. Everything else — projections, indexes, summaries —
is rebuildable and therefore uninteresting by comparison.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from atlas_harness.events.models import (
    SUPPORTED_SCHEMA_VERSIONS,
    SUSPENDED_STATUS,
    Event,
    EventType,
    OperationResumed,
    OperationSuspended,
    SnapshotCreated,
)
from atlas_harness.events.reducer import Reducer, SessionState, ToolCallState
from atlas_harness.events.store import EventStore
from atlas_harness.kernel.errors import EventValidationError, RecoveryError
from atlas_harness.kernel.faults import FaultInjector
from atlas_harness.kernel.ids import new_id, validate_session_id

SNAPSHOTS_DIRNAME = "snapshots"

FAULT_BEFORE_SNAPSHOT_CREATED = "recovery.before_snapshot_created"
FAULT_AFTER_SNAPSHOT_CREATED = "recovery.after_snapshot_created"

REPLAY = "replay"
"""The call may be re-run under its original idempotency key."""

RESTORE = "restore"
"""The call already has a result. Restore the projection, run nothing."""

CONFIRM = "confirm"
"""A side effect may or may not have landed. A human decides."""


class ToolCallDecision(BaseModel):
    """What recovery intends to do about one interrupted tool call."""

    model_config = ConfigDict(frozen=True)

    call_id: str
    tool_name: str
    action: str
    reason: str
    risk: str | None = None
    idempotent: bool = False
    idempotency_key: str | None = None

    @property
    def needs_confirmation(self) -> bool:
        return self.action == CONFIRM


def classify_tool_call(call: ToolCallState) -> ToolCallDecision:
    """Decide replay / restore / confirm for one call, from the projection alone.

    Ordering matters. A finished call is settled by its result and never re-run, no
    matter how risky it was — that is the completion condition for M4. Only then does
    the risk level get a say, and it can veto an ``idempotent`` flag: ``write_file``
    declares itself idempotent yet still owes a confirmation, because the harness
    cannot know whether the write landed before the crash.
    """

    if call.status in {"succeeded", "failed"}:
        return ToolCallDecision(
            call_id=call.call_id,
            tool_name=call.tool_name,
            action=RESTORE,
            reason="tool_result already recorded; the call is settled",
            risk=call.risk,
            idempotent=call.idempotent,
            idempotency_key=call.idempotency_key,
        )
    if call.replayable:
        return ToolCallDecision(
            call_id=call.call_id,
            tool_name=call.tool_name,
            action=REPLAY,
            reason="idempotent read; safe to repeat under the original key",
            risk=call.risk,
            idempotent=call.idempotent,
            idempotency_key=call.idempotency_key,
        )
    if call.confirmed:
        # A human took responsibility for this one. Note that this branch sits after
        # the settled check, so a confirmation can never resurrect a call that already
        # recorded a result — it only unblocks one that never got that far.
        return ToolCallDecision(
            call_id=call.call_id,
            tool_name=call.tool_name,
            action=REPLAY,
            reason="a human confirmed this call; replaying under the original key",
            risk=call.risk,
            idempotent=call.idempotent,
            idempotency_key=call.idempotency_key,
        )
    if call.risk is None:
        reason = "tool declared no risk level; refusing to guess"
    elif call.idempotent:
        reason = f"idempotent but {call.risk} risk; the side effect may already have landed"
    else:
        reason = f"non-idempotent {call.risk} call; replay could duplicate the side effect"
    return ToolCallDecision(
        call_id=call.call_id,
        tool_name=call.tool_name,
        action=CONFIRM,
        reason=reason,
        risk=call.risk,
        idempotent=call.idempotent,
        idempotency_key=call.idempotency_key,
    )


class OperationRecovery(BaseModel):
    """Recovery plan for one unfinished operation."""

    model_config = ConfigDict(frozen=True)

    operation_id: str
    lane_id: str
    status: str
    decisions: list[ToolCallDecision] = []
    model_request_incomplete: bool = False
    """A model_requested has no matching response; the request is retried, not resumed."""

    @property
    def confirm_call_ids(self) -> list[str]:
        return [d.call_id for d in self.decisions if d.action == CONFIRM]

    @property
    def replay_call_ids(self) -> list[str]:
        return [d.call_id for d in self.decisions if d.action == REPLAY]

    @property
    def restore_call_ids(self) -> list[str]:
        return [d.call_id for d in self.decisions if d.action == RESTORE]

    @property
    def needs_confirmation(self) -> bool:
        return bool(self.confirm_call_ids)


class RecoveryPlan(BaseModel):
    """The full picture after replaying one session's log."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    last_valid_seq: int
    resumed_from_seq: int
    """Snapshot seq the replay started from. 0 when no snapshot was usable."""

    snapshot_id: str | None = None
    operations: list[OperationRecovery] = []
    state: SessionState

    @property
    def needs_confirmation(self) -> bool:
        return any(operation.needs_confirmation for operation in self.operations)

    @property
    def unfinished_operation_ids(self) -> list[str]:
        return [operation.operation_id for operation in self.operations]

    def command(self) -> str:
        """The exact command an operator should run next."""

        if not self.operations:
            return f"atlas inspect {self.session_id}"
        pending = [
            call_id for operation in self.operations for call_id in operation.confirm_call_ids
        ]
        if pending:
            flags = " ".join(f"--confirm {call_id}" for call_id in pending)
            return f"atlas resume {self.session_id} {flags}"
        return f"atlas resume {self.session_id}"

    def summary(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "last_valid_seq": self.last_valid_seq,
            "resumed_from_seq": self.resumed_from_seq,
            "snapshot_id": self.snapshot_id,
            "needs_confirmation": self.needs_confirmation,
            "operations": [
                {
                    "operation_id": operation.operation_id,
                    "lane": operation.lane_id,
                    "status": operation.status,
                    "confirm": operation.confirm_call_ids,
                    "replay": operation.replay_call_ids,
                    "restore": operation.restore_call_ids,
                    "model_request_incomplete": operation.model_request_incomplete,
                }
                for operation in self.operations
            ],
            "command": self.command(),
        }


@dataclass
class SnapshotRecord:
    """A snapshot file plus the event that announced it."""

    snapshot_id: str
    session_id: str
    lane_id: str
    last_seq: int
    path: Path
    checksum: str
    state_hash: str | None = None
    event_count: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class RecoveryService:
    """Write snapshots, replay logs and drive suspend / resume / abort.

    Recovery always takes an explicit session id. Inferring "the last session" from
    directory mtimes is exactly the kind of guess that turns a crash into data loss.
    """

    def __init__(
        self,
        store: EventStore,
        *,
        faults: FaultInjector | None = None,
        snapshot_every: int = 25,
    ) -> None:
        self._store = store
        self._faults = faults or store.faults
        self._snapshot_every = max(1, snapshot_every)

    @property
    def store(self) -> EventStore:
        return self._store

    def snapshot_dir(self, session_id: str) -> Path:
        validate_session_id(session_id)
        return self._store.log_path(session_id).parent / SNAPSHOTS_DIRNAME

    # ---------------------------------------------------------------- snapshots

    def create_snapshot(
        self,
        session_id: str,
        *,
        lane_id: str | None = None,
        state: SessionState | None = None,
    ) -> SnapshotRecord:
        """Fold the log into a snapshot file and announce it with an event.

        The file is written before the event, so a crash in between leaves an orphan
        file that nothing points at. The reverse order would leave an event pointing at
        a file that does not exist, which recovery would have to refuse.
        """

        projected = state if state is not None else self._store.load_state(session_id)
        last_seq = projected.last_seq
        lane = lane_id or projected.current_lane_id
        snapshot_id = new_id("snap")
        directory = self.snapshot_dir(session_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{snapshot_id}.json"
        state_hash = projected.state_hash()
        document: dict[str, Any] = {
            "snapshot_id": snapshot_id,
            "session_id": session_id,
            "lane": lane,
            "last_seq": last_seq,
            "schema_version": projected.schema_version,
            "state_hash": state_hash,
            "event_count": projected.event_count,
            "state": projected.model_dump(mode="json"),
        }
        body = json.dumps(document, sort_keys=True, ensure_ascii=False)
        checksum = hashlib.sha256(body.encode("utf-8")).hexdigest()
        self._faults.check(FAULT_BEFORE_SNAPSHOT_CREATED)
        path.write_text(body, encoding="utf-8", newline="\n")
        event = self._store.append_new(
            EventType.SNAPSHOT_CREATED,
            session_id=session_id,
            lane_id=lane,
            payload=SnapshotCreated(
                snapshot_id=snapshot_id,
                state_hash=state_hash,
                last_seq=last_seq,
                path=path.name,
                checksum=checksum,
                event_count=projected.event_count,
            ),
        )
        self._faults.check(FAULT_AFTER_SNAPSHOT_CREATED)
        return SnapshotRecord(
            snapshot_id=snapshot_id,
            session_id=session_id,
            lane_id=lane,
            last_seq=last_seq,
            path=path,
            checksum=checksum,
            state_hash=state_hash,
            event_count=projected.event_count,
            extra={"seq": event.seq},
        )

    def maybe_snapshot(self, session_id: str) -> SnapshotRecord | None:
        """Snapshot when enough events have accumulated since the last one."""

        state = self._store.load_state(session_id)
        latest = state.latest_snapshot()
        since = state.last_seq - (latest.last_seq if latest is not None else 0)
        if since < self._snapshot_every:
            return None
        return self.create_snapshot(session_id, state=state)

    def load_snapshot(self, session_id: str, snapshot_id: str) -> SnapshotRecord | None:
        """Read one snapshot file, verifying its checksum and schema.

        Returns ``None`` for a snapshot that cannot be trusted. Recovery then falls
        back to replaying the whole log, which is slower and always correct.
        """

        path = self.snapshot_dir(session_id) / f"{snapshot_id}.json"
        if not path.exists():
            return None
        body = path.read_text(encoding="utf-8")
        try:
            document = json.loads(body)
        except json.JSONDecodeError:
            return None
        if not isinstance(document, dict):
            return None
        if document.get("session_id") != session_id:
            return None
        version = document.get("schema_version")
        if not isinstance(version, int) or version not in SUPPORTED_SCHEMA_VERSIONS:
            return None
        last_seq = document.get("last_seq")
        if not isinstance(last_seq, int) or last_seq < 0:
            return None
        return SnapshotRecord(
            snapshot_id=str(document.get("snapshot_id") or snapshot_id),
            session_id=session_id,
            lane_id=str(document.get("lane") or "main"),
            last_seq=last_seq,
            path=path,
            checksum=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            state_hash=_optional_str(document.get("state_hash")),
            event_count=_optional_int(document.get("event_count")),
        )

    # ----------------------------------------------------------------- recovery

    def plan(self, session_id: str) -> RecoveryPlan:
        """Replay one session's log and decide what to do about what was open.

        Any validation failure refuses automatic recovery and reports the last seq that
        did validate, so an operator can see exactly how far the log is trustworthy.
        """

        validate_session_id(session_id)
        if not self._store.session_exists(session_id):
            raise RecoveryError(
                "cannot recover an unknown session",
                details={"session_id": session_id},
            )
        try:
            events = self._store.read_events(session_id)
        except EventValidationError as exc:
            raise RecoveryError(
                "event log failed validation; automatic recovery refused",
                details={"session_id": session_id, **dict(exc.details)},
            ) from exc
        try:
            state = Reducer(session_id).reduce(events)
        except EventValidationError as exc:
            raise RecoveryError(
                "event log failed validation; automatic recovery refused",
                details={"session_id": session_id, **dict(exc.details)},
            ) from exc

        snapshot = self._resolve_snapshot(session_id, state)
        operations = [
            self._recover_operation(state, operation_id, events)
            for operation_id in state.unfinished_operation_ids
        ]
        return RecoveryPlan(
            session_id=session_id,
            last_valid_seq=state.last_seq,
            resumed_from_seq=snapshot.last_seq if snapshot is not None else 0,
            snapshot_id=snapshot.snapshot_id if snapshot is not None else None,
            operations=operations,
            state=state,
        )

    def _resolve_snapshot(self, session_id: str, state: SessionState) -> SnapshotRecord | None:
        """Pick the newest snapshot whose file still validates."""

        for record in sorted(state.snapshot_records, key=lambda r: r.last_seq, reverse=True):
            loaded = self.load_snapshot(session_id, record.snapshot_id)
            if loaded is None:
                continue
            if loaded.last_seq > state.last_seq:
                # A snapshot claiming events the log does not have means the log was
                # truncated behind it. The log wins; ignore the snapshot.
                continue
            return loaded
        return None

    def _recover_operation(
        self, state: SessionState, operation_id: str, events: Sequence[Event]
    ) -> OperationRecovery:
        operation = state.operations[operation_id]
        # Every call is classified, settled ones included. A finished call becomes a
        # visible ``restore`` row rather than being silently omitted: "this tool will
        # not run again" is the guarantee M4 exists to make, so an operator should be
        # able to read it off the plan instead of inferring it from an absence.
        decisions = [classify_tool_call(call) for call in operation.tool_calls.values()]
        return OperationRecovery(
            operation_id=operation_id,
            lane_id=operation.lane_id,
            status=operation.status,
            decisions=decisions,
            model_request_incomplete=operation.model_requests > operation.model_responses,
        )

    # -------------------------------------------------- suspend / resume / abort

    def suspend(
        self,
        session_id: str,
        operation_id: str,
        *,
        reason: str,
        pending_tool_call_ids: Sequence[str] = (),
        detail: str | None = None,
        lane_id: str | None = None,
    ) -> Event:
        """Record that the operation may not continue without a decision."""

        state = self._store.load_state(session_id)
        operation = state.operations.get(operation_id)
        lane = lane_id or (operation.lane_id if operation is not None else state.current_lane_id)
        return self._store.append_new(
            EventType.OPERATION_SUSPENDED,
            session_id=session_id,
            lane_id=lane,
            operation_id=operation_id,
            payload=OperationSuspended(
                reason=reason,
                pending_tool_call_ids=list(pending_tool_call_ids),
                detail=detail,
            ),
        )

    def suspend_from_plan(self, plan: RecoveryPlan) -> list[Event]:
        """Suspend every operation in the plan that owes a confirmation."""

        written: list[Event] = []
        for operation in plan.operations:
            if not operation.needs_confirmation:
                continue
            if operation.status == SUSPENDED_STATUS:
                continue
            reasons = "; ".join(
                f"{d.call_id}: {d.reason}" for d in operation.decisions if d.action == CONFIRM
            )
            written.append(
                self.suspend(
                    plan.session_id,
                    operation.operation_id,
                    reason="unconfirmed side effect after restart",
                    pending_tool_call_ids=operation.confirm_call_ids,
                    detail=reasons,
                    lane_id=operation.lane_id,
                )
            )
        return written

    def resume(
        self,
        session_id: str,
        *,
        confirm: Sequence[str] = (),
    ) -> RecoveryPlan:
        """Take suspended operations forward, honouring explicit confirmations.

        A partial answer is refused outright rather than half-applied. An operation
        needs every one of its questions answered before it may resume, so confirming
        some of them changes nothing — and a confirmation that changes nothing would be
        silently forgotten, because the authorization only becomes durable when it is
        folded from ``operation_resumed``. Refusing keeps that from looking like it
        worked.
        """

        plan = self.plan(session_id)
        confirmed = set(confirm)
        unknown = confirmed - {
            decision.call_id for operation in plan.operations for decision in operation.decisions
        }
        if unknown:
            raise RecoveryError(
                "confirmation names a tool call that is not awaiting a decision",
                details={"session_id": session_id, "unknown_tool_call_ids": sorted(unknown)},
            )
        # Validated across every operation before anything is written, so a refusal
        # cannot leave an earlier operation already resumed.
        for operation in plan.operations:
            answered = [call_id for call_id in operation.confirm_call_ids if call_id in confirmed]
            missing = [
                call_id for call_id in operation.confirm_call_ids if call_id not in confirmed
            ]
            if answered and missing:
                raise RecoveryError(
                    "operation needs every pending call confirmed before it can resume",
                    details={
                        "session_id": session_id,
                        "operation_id": operation.operation_id,
                        "confirmed_tool_call_ids": answered,
                        "missing_tool_call_ids": missing,
                    },
                )
        for operation in plan.operations:
            outstanding = [
                call_id for call_id in operation.confirm_call_ids if call_id not in confirmed
            ]
            if outstanding:
                if operation.status != SUSPENDED_STATUS:
                    self.suspend(
                        session_id,
                        operation.operation_id,
                        reason="unconfirmed side effect after restart",
                        pending_tool_call_ids=outstanding,
                        lane_id=operation.lane_id,
                    )
                continue
            self._store.append_new(
                EventType.OPERATION_RESUMED,
                session_id=session_id,
                lane_id=operation.lane_id,
                operation_id=operation.operation_id,
                payload=OperationResumed(
                    resumed_from_seq=plan.resumed_from_seq,
                    confirmed_tool_call_ids=[
                        call_id for call_id in operation.confirm_call_ids if call_id in confirmed
                    ],
                    replayed_tool_call_ids=operation.replay_call_ids,
                ),
            )
        return self.plan(session_id)

    def abort(self, session_id: str, *, reason: str = "aborted by operator") -> list[Event]:
        """Close every unfinished operation without running anything else."""

        plan = self.plan(session_id)
        written: list[Event] = []
        for operation in plan.operations:
            written.append(
                self._store.append_new(
                    EventType.OPERATION_ABORTED,
                    session_id=session_id,
                    lane_id=operation.lane_id,
                    operation_id=operation.operation_id,
                    payload={"reason": reason},
                )
            )
        return written


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: Any) -> int | None:
    return None if isinstance(value, bool) or not isinstance(value, int) else value
