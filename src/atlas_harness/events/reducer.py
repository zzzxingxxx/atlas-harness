"""Pure event-to-state projection used for live state and replay."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from typing import Any

from pydantic import BaseModel, Field

from atlas_harness.events.models import (
    CURRENT_SCHEMA_VERSION,
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
            state.snapshots.append(str(payload.get("snapshot_id") or event.event_id))

        operation = self._resolve_operation(event)
        if operation is not None:
            self._apply_operation_event(operation, event, payload)
            if (
                event.event_type in TERMINAL_OPERATION_EVENTS
                and lane.current_operation_id == operation.operation_id
            ):
                lane.status = "idle"
                lane.current_operation_id = None

        self._apply_session_event(event, payload)
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
