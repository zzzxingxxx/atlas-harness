"""Pure event-to-state projection used for live state and replay."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from typing import Any

from pydantic import BaseModel, Field

from atlas_harness.events.models import (
    CURRENT_SCHEMA_VERSION,
    DEFAULT_LANE,
    SUSPENDED_STATUS,
    TERMINAL_OPERATION_EVENTS,
    Event,
    EventType,
)
from atlas_harness.kernel.errors import EventValidationError


class ToolCallState(BaseModel):
    call_id: str
    tool_name: str
    status: str = "started"
    arguments: dict[str, Any] = Field(default_factory=dict)
    output: Any = None
    error: str | None = None
    started_at_ms: int | None = None
    finished_at_ms: int | None = None
    idempotency_key: str | None = None
    risk: str | None = None
    idempotent: bool = False
    """Declared by the manifest at tool_started time.

    Recorded on the projection so recovery can triage an interrupted call without
    consulting the registry, which may have changed shape since the log was written.
    """

    confirmed: bool = False
    """A human authorized this specific call to run again after a crash.

    Folded from ``operation_resumed``, so the authorization lives in the log and
    survives a second crash instead of being re-asked forever.
    """

    @property
    def replayable(self) -> bool:
        """True only for a read that the tool itself declares idempotent.

        A write can be idempotent and still be unsafe to replay unattended, so the
        risk level and the flag both have to agree. Everything else is suspended for
        a human decision.
        """

        return self.risk == "read" and self.idempotent


class TokenUsageState(BaseModel):
    """Running token total for one operation, summed over model responses."""

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class ProviderErrorState(BaseModel):
    error: str
    error_code: str | None = None
    status_code: int | None = None
    retryable: bool = False
    attempt: int = 1
    at_ms: int | None = None


class QueueMessageState(BaseModel):
    message_id: str
    queue: str
    content: str = ""
    source: str = "user"
    consumed: bool = False
    enqueued_at_ms: int | None = None
    consumed_at_ms: int | None = None


class OperationState(BaseModel):
    operation_id: str
    lane_id: str
    status: str = "started"
    name: str | None = None
    provider: str | None = None
    model: str | None = None
    model_requests: int = 0
    model_responses: int = 0
    stop_reason: str | None = None
    token_usage: TokenUsageState = Field(default_factory=TokenUsageState)
    messages: list[str] = Field(default_factory=list)
    tool_calls: dict[str, ToolCallState] = Field(default_factory=dict)
    approvals: dict[str, bool | None] = Field(default_factory=dict)
    provider_errors: list[ProviderErrorState] = Field(default_factory=list)
    queue_messages: dict[str, QueueMessageState] = Field(default_factory=dict)
    result: Any = None
    error: str | None = None
    started_at_ms: int | None = None
    finished_at_ms: int | None = None
    suspend_reason: str | None = None
    pending_tool_call_ids: list[str] = Field(default_factory=list)
    """Calls a human still owes a decision on. Empty unless status is suspended."""

    resume_count: int = 0
    compact_pending: bool = False
    """The soft threshold was crossed and no compaction has run since."""

    compaction_count: int = 0
    last_compaction_reason: str | None = None
    artifact_ids: list[str] = Field(default_factory=list)
    """Large outputs moved out of the context. The bytes are still on disk, so a
    compaction never costs evidence."""

    capability_injections: int = 0
    injected_memory_ids: list[str] = Field(default_factory=list)
    injected_skill_versions: list[str] = Field(default_factory=list)
    """What this operation's prompts actually carried, de-duplicated across
    iterations. Retrieval runs per iteration and usually returns the same records,
    so a raw append would report one skill twenty times."""

    capability_skips: dict[str, int] = Field(default_factory=dict)
    """Skip reason -> how often it fired. This is the half that answers "why is the
    skill I expected missing": a count against ``not_permitted`` is a scope problem,
    against ``budget`` a sizing one, and against ``no_match`` a retrieval one."""

    @property
    def open_tool_call_ids(self) -> list[str]:
        return [call_id for call_id, call in self.tool_calls.items() if call.status == "started"]

    def pending_queue_messages(self, queue: str) -> list[QueueMessageState]:
        """Messages enqueued for one queue that no iteration has consumed yet."""

        return [
            message
            for message in self.queue_messages.values()
            if message.queue == queue and not message.consumed
        ]


class LaneState(BaseModel):
    lane_id: str
    status: str = "idle"
    operation_ids: list[str] = Field(default_factory=list)
    current_operation_id: str | None = None
    parent_lane: str | None = None
    forked_from_seq: int | None = None
    label: str | None = None


class SnapshotState(BaseModel):
    """One recorded snapshot. `last_seq` is where recovery resumes replaying."""

    snapshot_id: str
    lane_id: str
    last_seq: int = 0
    state_hash: str | None = None
    path: str | None = None
    checksum: str | None = None
    event_count: int | None = None
    created_at_ms: int | None = None


class SessionState(BaseModel):
    session_id: str
    status: str = "created"
    title: str | None = None
    workspace_root: str | None = None
    schema_version: int = CURRENT_SCHEMA_VERSION
    created_at_ms: int | None = None
    updated_at_ms: int | None = None
    last_seq: int = 0
    event_count: int = 0
    messages: list[str] = Field(default_factory=list)
    lanes: dict[str, LaneState] = Field(default_factory=dict)
    operations: dict[str, OperationState] = Field(default_factory=dict)
    approvals: dict[str, bool | None] = Field(default_factory=dict)
    snapshots: list[str] = Field(default_factory=list)
    snapshot_records: list[SnapshotState] = Field(default_factory=list)
    current_lane_id: str = DEFAULT_LANE
    artifacts: list[str] = Field(default_factory=list)
    """Every artifact stored in this session, in order. Ids only: the bytes live
    on disk and are never folded into the projection."""

    compactions: int = 0
    """How many times the context was compacted. The messages above are unaffected
    -- compaction replaces the prompt, not the record."""

    memory_ids: list[str] = Field(default_factory=list)
    """Memories this session stored, in order. Ids only; the content is in the log
    and in the searchable index, neither of which the projection duplicates."""

    expired_memory_ids: list[str] = Field(default_factory=list)
    """Memories that left the retrievable set. Kept as a separate list rather than
    removed from ``memory_ids``, because expiry is not deletion: a replay still has
    to be able to say the memory existed and when it stopped being used."""

    skill_versions: list[str] = Field(default_factory=list)
    """Every ``skill_id@version`` registered here, so a version that was later
    retired is still visible as something the session once knew about."""

    skill_statuses: dict[str, str] = Field(default_factory=dict)
    """Current status per ``skill_id@version``. Folded from the transitions rather
    than read from the table, so a replay of the log alone can say which version
    was injectable at the end -- the projection never has to trust the index."""

    capability_injections: int = 0
    """Requests that carried a capability slot. Zero for a run with injection off."""

    @property
    def live_memory_ids(self) -> list[str]:
        """Stored and not since expired."""

        expired = set(self.expired_memory_ids)
        return [memory_id for memory_id in self.memory_ids if memory_id not in expired]

    @property
    def pending_approval_ids(self) -> list[str]:
        return [key for key, resolved in self.approvals.items() if resolved is None]

    @property
    def open_operation_ids(self) -> list[str]:
        return [
            operation.operation_id
            for operation in self.operations.values()
            if operation.status == "started"
        ]

    @property
    def suspended_operation_ids(self) -> list[str]:
        return [
            operation.operation_id
            for operation in self.operations.values()
            if operation.status == SUSPENDED_STATUS
        ]

    @property
    def unfinished_operation_ids(self) -> list[str]:
        """Operations that never reached a terminal event, suspended included."""

        return [
            operation.operation_id
            for operation in self.operations.values()
            if operation.status in {"started", SUSPENDED_STATUS}
        ]

    def latest_snapshot(self, lane_id: str | None = None) -> SnapshotState | None:
        candidates = [
            record
            for record in self.snapshot_records
            if lane_id is None or record.lane_id == lane_id
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda record: record.last_seq)

    def state_hash(self) -> str:
        """Stable fingerprint so two replays of one log can be compared."""

        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Reducer:
    """Apply events in order. No I/O is performed by this class."""

    def __init__(self, session_id: str, *, strict_seq: bool = True) -> None:
        self.state = SessionState(session_id=session_id)
        self._strict_seq = strict_seq

    def apply(self, event: Event) -> SessionState:
        state = self.state
        if event.session_id != state.session_id:
            raise EventValidationError(
                "event belongs to a different session",
                details={"expected": state.session_id, "actual": event.session_id},
            )
        self._check_seq(event)
        payload = event.payload.model_dump(mode="python")
        lane = state.lanes.setdefault(event.lane_id, LaneState(lane_id=event.lane_id))

        if event.event_type is EventType.SESSION_CREATED:
            state.status = "active"
            state.title = payload.get("title") or state.title
            state.workspace_root = payload.get("workspace_root") or state.workspace_root
        elif event.event_type is EventType.OPERATION_STARTED:
            operation_id = event.operation_id or event.event_id
            state.operations[operation_id] = OperationState(
                operation_id=operation_id,
                lane_id=event.lane_id,
                name=payload.get("name"),
                started_at_ms=event.timestamp_ms,
            )
            lane.status = "running"
            lane.current_operation_id = operation_id
            if operation_id not in lane.operation_ids:
                lane.operation_ids.append(operation_id)
        elif event.event_type is EventType.SNAPSHOT_CREATED:
            snapshot_id = str(payload.get("snapshot_id") or event.event_id)
            state.snapshots.append(snapshot_id)
            last_seq = payload.get("last_seq")
            state.snapshot_records.append(
                SnapshotState(
                    snapshot_id=snapshot_id,
                    lane_id=event.lane_id,
                    last_seq=int(last_seq) if last_seq is not None else max(event.seq - 1, 0),
                    state_hash=payload.get("state_hash"),
                    path=payload.get("path"),
                    checksum=payload.get("checksum"),
                    event_count=payload.get("event_count"),
                    created_at_ms=event.timestamp_ms,
                )
            )
        elif event.event_type is EventType.LANE_CREATED:
            self._declare_lane(str(payload["lane"]), parent=payload.get("parent_lane"))
        elif event.event_type is EventType.BRANCH_CREATED:
            branch = self._declare_lane(str(payload["lane"]), parent=payload.get("parent_lane"))
            from_seq = payload.get("from_seq")
            branch.forked_from_seq = int(from_seq) if from_seq is not None else event.seq
            branch.label = payload.get("label") or branch.label
        elif event.event_type is EventType.BRANCH_SWITCHED:
            target = self._declare_lane(str(payload["lane"]))
            state.current_lane_id = target.lane_id

        operation = self._resolve_operation(event)
        if operation is not None:
            self._apply_operation_event(operation, event, payload)
            if (
                event.event_type in TERMINAL_OPERATION_EVENTS
                and lane.current_operation_id == operation.operation_id
            ):
                lane.status = "idle"
                lane.current_operation_id = None
            elif event.event_type is EventType.OPERATION_SUSPENDED:
                lane.status = SUSPENDED_STATUS
                lane.current_operation_id = operation.operation_id
            elif event.event_type is EventType.OPERATION_RESUMED:
                lane.status = "running"
                lane.current_operation_id = operation.operation_id

        self._apply_session_event(event, payload)
        if state.status != "created" and state.suspended_operation_ids:
            state.status = SUSPENDED_STATUS
        elif state.status == SUSPENDED_STATUS:
            state.status = "active"
        if state.created_at_ms is None:
            state.created_at_ms = event.timestamp_ms
        state.updated_at_ms = event.timestamp_ms
        state.last_seq = event.seq
        state.event_count += 1
        return state

    def _check_seq(self, event: Event) -> None:
        expected = self.state.last_seq + 1
        if event.seq == expected:
            return
        if self._strict_seq or event.seq <= self.state.last_seq:
            raise EventValidationError(
                "event seq is duplicated, missing or out of order",
                details={
                    "session_id": event.session_id,
                    "expected_seq": expected,
                    "actual_seq": event.seq,
                    "last_valid_seq": self.state.last_seq,
                },
            )

    def _declare_lane(self, lane_id: str, *, parent: str | None = None) -> LaneState:
        lane = self.state.lanes.setdefault(lane_id, LaneState(lane_id=lane_id))
        if parent is not None:
            lane.parent_lane = parent
        return lane

    def _resolve_operation(self, event: Event) -> OperationState | None:
        if event.operation_id is None:
            return None
        operation = self.state.operations.get(event.operation_id)
        if operation is None:
            raise EventValidationError(
                "event references an operation that was never started",
                details={"operation_id": event.operation_id, "seq": event.seq},
            )
        return operation

    def _apply_session_event(self, event: Event, payload: dict[str, Any]) -> None:
        state = self.state
        if event.event_type is EventType.ASSISTANT_MESSAGE:
            state.messages.append(str(payload.get("content", "")))
        elif event.event_type is EventType.APPROVAL_REQUESTED:
            state.approvals.setdefault(str(payload["approval_id"]), None)
        elif event.event_type is EventType.APPROVAL_RESOLVED:
            state.approvals[str(payload["approval_id"])] = bool(payload["approved"])
        elif event.event_type is EventType.ARTIFACT_STORED:
            # Tracked at session level too: an artifact can be stored outside any
            # operation, and either way the id has to stay findable after the
            # prompt that referenced it has been compacted away.
            state.artifacts.append(str(payload["artifact_id"]))
        elif event.event_type is EventType.CONTEXT_COMPACTED:
            state.compactions += 1
        elif event.event_type is EventType.MEMORY_STORED:
            memory_id = str(payload["memory_id"])
            if memory_id not in state.memory_ids:
                state.memory_ids.append(memory_id)
        elif event.event_type is EventType.MEMORY_EXPIRED:
            expired = str(payload["memory_id"])
            if expired not in state.expired_memory_ids:
                state.expired_memory_ids.append(expired)
        elif event.event_type is EventType.SKILL_REGISTERED:
            label = f"{payload['skill_id']}@{payload.get('version') or '0.1.0'}"
            if label not in state.skill_versions:
                state.skill_versions.append(label)
            state.skill_statuses[label] = str(payload.get("status") or "draft")
        elif event.event_type is EventType.SKILL_STATUS_CHANGED:
            label = f"{payload['skill_id']}@{payload.get('version') or '0.1.0'}"
            state.skill_statuses[label] = str(payload.get("to_status") or "candidate")
        elif event.event_type is EventType.CAPABILITY_INJECTED:
            state.capability_injections += 1

    def _apply_operation_event(
        self, operation: OperationState, event: Event, payload: dict[str, Any]
    ) -> None:
        event_type = event.event_type
        if event_type is EventType.MODEL_REQUESTED:
            operation.provider = payload.get("provider") or operation.provider
            operation.model = payload.get("model") or operation.model
            operation.model_requests += 1
        elif event_type is EventType.ASSISTANT_MESSAGE:
            operation.messages.append(str(payload.get("content", "")))
        elif event_type is EventType.TOOL_STARTED:
            call_id = str(payload.get("call_id") or event.event_id)
            operation.tool_calls[call_id] = ToolCallState(
                call_id=call_id,
                tool_name=str(payload["tool_name"]),
                arguments=dict(payload.get("arguments") or {}),
                started_at_ms=event.timestamp_ms,
                idempotency_key=payload.get("idempotency_key"),
                risk=payload.get("risk"),
                idempotent=bool(payload.get("idempotent", False)),
            )
        elif event_type is EventType.TOOL_RESULT:
            call_id = str(payload.get("call_id") or event.event_id)
            call = operation.tool_calls.get(call_id) or ToolCallState(
                call_id=call_id, tool_name=str(payload["tool_name"])
            )
            call.status = "succeeded" if payload.get("success", True) else "failed"
            call.output = payload.get("output")
            call.error = payload.get("error")
            call.finished_at_ms = event.timestamp_ms
            operation.tool_calls[call_id] = call
        elif event_type is EventType.APPROVAL_REQUESTED:
            operation.approvals.setdefault(str(payload["approval_id"]), None)
        elif event_type is EventType.APPROVAL_RESOLVED:
            operation.approvals[str(payload["approval_id"])] = bool(payload["approved"])
        elif event_type is EventType.OPERATION_FINISHED:
            operation.status = "finished"
            operation.result = payload.get("result")
            operation.finished_at_ms = event.timestamp_ms
        elif event_type is EventType.OPERATION_FAILED:
            operation.status = "failed"
            operation.error = str(payload.get("error", "operation failed"))
            operation.finished_at_ms = event.timestamp_ms
        elif event_type is EventType.OPERATION_ABORTED:
            operation.status = "aborted"
            operation.error = payload.get("reason")
            operation.finished_at_ms = event.timestamp_ms
        elif event_type is EventType.OPERATION_SUSPENDED:
            operation.status = SUSPENDED_STATUS
            operation.suspend_reason = str(payload.get("reason", "decision owed"))
            operation.pending_tool_call_ids = [
                str(call_id) for call_id in payload.get("pending_tool_call_ids") or []
            ]
        elif event_type is EventType.OPERATION_RESUMED:
            operation.status = "started"
            operation.suspend_reason = None
            operation.resume_count += 1
            confirmed = {str(call_id) for call_id in payload.get("confirmed_tool_call_ids") or []}
            operation.pending_tool_call_ids = [
                call_id for call_id in operation.pending_tool_call_ids if call_id not in confirmed
            ]
            # The authorization is folded onto the call itself, so a second crash
            # after the confirmation does not ask the same question again.
            for call_id in confirmed:
                authorized = operation.tool_calls.get(call_id)
                if authorized is not None:
                    authorized.confirmed = True
        elif event_type is EventType.MODEL_STREAM_COMPLETED:
            operation.model_responses += 1
            operation.stop_reason = payload.get("stop_reason") or operation.stop_reason
            operation.token_usage.input_tokens += int(payload.get("input_tokens") or 0)
            operation.token_usage.output_tokens += int(payload.get("output_tokens") or 0)
        elif event_type is EventType.PROVIDER_ERROR:
            operation.provider_errors.append(
                ProviderErrorState(
                    error=str(payload.get("error", "provider error")),
                    error_code=payload.get("error_code"),
                    status_code=payload.get("status_code"),
                    retryable=bool(payload.get("retryable", False)),
                    attempt=int(payload.get("attempt") or 1),
                    at_ms=event.timestamp_ms,
                )
            )
        elif event_type is EventType.QUEUE_MESSAGE_ENQUEUED:
            message_id = str(payload["message_id"])
            operation.queue_messages[message_id] = QueueMessageState(
                message_id=message_id,
                queue=str(payload["queue"]),
                content=str(payload.get("content", "")),
                source=str(payload.get("source", "user")),
                enqueued_at_ms=event.timestamp_ms,
            )
        elif event_type is EventType.QUEUE_MESSAGE_CONSUMED:
            message_id = str(payload["message_id"])
            queued = operation.queue_messages.get(message_id) or QueueMessageState(
                message_id=message_id, queue=str(payload["queue"])
            )
            queued.consumed = True
            queued.consumed_at_ms = event.timestamp_ms
            operation.queue_messages[message_id] = queued
        elif event_type is EventType.CONTEXT_COMPACT_PENDING:
            operation.compact_pending = True
        elif event_type is EventType.CONTEXT_COMPACTED:
            # Compaction changes the prompt, not the record: the messages folded
            # above stay exactly where they are. Only the counter moves, so a
            # replay can tell how often this operation was compacted without the
            # projection pretending anything was removed.
            operation.compact_pending = False
            operation.compaction_count += 1
            operation.last_compaction_reason = str(payload.get("reason") or "threshold")
        elif event_type is EventType.ARTIFACT_STORED:
            operation.artifact_ids.append(str(payload["artifact_id"]))
        elif event_type is EventType.CAPABILITY_INJECTED:
            operation.capability_injections += 1
            for selection in payload.get("selected") or []:
                if not isinstance(selection, dict):
                    continue
                ref_id = str(selection.get("ref_id") or "")
                if not ref_id:
                    continue
                if selection.get("kind") == "skill":
                    label = f"{ref_id}@{selection.get('version') or '0.1.0'}"
                    if label not in operation.injected_skill_versions:
                        operation.injected_skill_versions.append(label)
                elif ref_id not in operation.injected_memory_ids:
                    operation.injected_memory_ids.append(ref_id)
            for skip in payload.get("skipped") or []:
                if not isinstance(skip, dict):
                    continue
                reason = str(skip.get("reason") or "no_match")
                operation.capability_skips[reason] = operation.capability_skips.get(reason, 0) + 1

    def reduce(self, events: Iterable[Event]) -> SessionState:
        for event in events:
            self.apply(event)
        return self.state


def replay(events: Sequence[Event], *, session_id: str | None = None) -> SessionState:
    """Rebuild a session projection from an ordered event sequence."""

    if session_id is None:
        if not events:
            raise EventValidationError("cannot replay an empty stream without a session id")
        session_id = events[0].session_id
    return Reducer(session_id).reduce(events)
