"""Versioned event envelope and typed payloads."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, SerializeAsAny, model_validator

from atlas_harness.kernel.errors import EventValidationError
from atlas_harness.kernel.ids import IdFactory

DEFAULT_LANE = "main"

CURRENT_SCHEMA_VERSION = 4
"""Version written by this build. M5 added the compaction and artifact events."""

SUPPORTED_SCHEMA_VERSIONS = frozenset({1, 2, 3, 4})
"""Versions this build can read. v1-v3 logs from M1-M4 stay replayable."""


class EventType(StrEnum):
    SESSION_CREATED = "session_created"
    OPERATION_STARTED = "operation_started"
    MODEL_REQUESTED = "model_requested"
    ASSISTANT_MESSAGE = "assistant_message"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"
    TOOL_STARTED = "tool_started"
    TOOL_RESULT = "tool_result"
    OPERATION_FINISHED = "operation_finished"
    OPERATION_FAILED = "operation_failed"
    OPERATION_ABORTED = "operation_aborted"
    OPERATION_SUSPENDED = "operation_suspended"
    OPERATION_RESUMED = "operation_resumed"
    SNAPSHOT_CREATED = "snapshot_created"
    LANE_CREATED = "lane_created"
    BRANCH_CREATED = "branch_created"
    BRANCH_SWITCHED = "branch_switched"
    MODEL_STREAM_COMPLETED = "model_stream_completed"
    PROVIDER_ERROR = "provider_error"
    QUEUE_MESSAGE_ENQUEUED = "queue_message_enqueued"
    QUEUE_MESSAGE_CONSUMED = "queue_message_consumed"
    CONTEXT_COMPACT_PENDING = "context_compact_pending"
    CONTEXT_COMPACTED = "context_compacted"
    ARTIFACT_STORED = "artifact_stored"


TERMINAL_OPERATION_EVENTS = frozenset(
    {
        EventType.OPERATION_FINISHED,
        EventType.OPERATION_FAILED,
        EventType.OPERATION_ABORTED,
    }
)

SUSPENDED_STATUS = "suspended"
"""Status an operation, lane and session take while a decision is owed."""


class Payload(BaseModel):
    """Base payload. Unknown keys are kept so old logs stay readable."""

    model_config = ConfigDict(extra="allow")


class SessionCreated(Payload):
    title: str | None = None
    workspace_root: str | None = None


class OperationStarted(Payload):
    name: str | None = None
    deadline_ms: int | None = None


class ModelRequested(Payload):
    provider: str | None = None
    model: str | None = None
    prompt: str | None = None


class AssistantMessage(Payload):
    content: str = ""
    role: str = "assistant"


class ApprovalRequested(Payload):
    approval_id: str
    reason: str | None = None


class ApprovalResolved(Payload):
    approval_id: str
    approved: bool
    reason: str | None = None


class ToolStarted(Payload):
    tool_name: str
    call_id: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None
    risk: str | None = None
    """Declared risk level, copied from the manifest so recovery can triage
    an unfinished call without re-consulting the registry."""
    idempotent: bool = False
    """Whether the manifest declares the call safe to run twice. Recovery also
    needs the risk level: an idempotent write still owes a confirmation."""


class ToolResult(Payload):
    tool_name: str
    call_id: str | None = None
    success: bool = True
    output: Any = None
    error: str | None = None


class OperationFinished(Payload):
    result: Any = None


class OperationFailed(Payload):
    error: str
    error_code: str | None = None


class OperationAborted(Payload):
    reason: str | None = None


class OperationSuspended(Payload):
    """A decision is owed before the operation may continue."""

    reason: str
    pending_tool_call_ids: list[str] = Field(default_factory=list)
    detail: str | None = None


class OperationResumed(Payload):
    """Recovery took the operation out of suspended."""

    resumed_from_seq: int | None = None
    confirmed_tool_call_ids: list[str] = Field(default_factory=list)
    replayed_tool_call_ids: list[str] = Field(default_factory=list)


class SnapshotCreated(Payload):
    snapshot_id: str | None = None
    state_hash: str | None = None
    last_seq: int | None = None
    """Last valid seq folded into this snapshot. Recovery replays from here."""
    path: str | None = None
    checksum: str | None = None
    event_count: int | None = None


class LaneCreated(Payload):
    lane: str
    parent_lane: str | None = None
    reason: str | None = None


class BranchCreated(Payload):
    """A new lane forked off an existing one at a known seq."""

    lane: str
    parent_lane: str | None = None
    from_seq: int | None = None
    label: str | None = None


class BranchSwitched(Payload):
    """Navigation only. History is never rewritten or deleted."""

    lane: str
    from_lane: str | None = None
    at_seq: int | None = None


class ModelStreamCompleted(Payload):
    """Summary of one model response. The text itself lives in assistant_message."""

    request_id: str | None = None
    provider: str | None = None
    model: str | None = None
    stop_reason: str | None = None
    iteration: int | None = None
    text_length: int = 0
    tool_call_count: int = 0
    invalid_tool_call_count: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None


class ProviderError(Payload):
    """A provider call failed or its stream broke mid-message."""

    provider: str | None = None
    model: str | None = None
    request_id: str | None = None
    error: str
    error_code: str | None = None
    status_code: int | None = None
    retryable: bool = False
    attempt: int = 1


class QueueMessageEnqueued(Payload):
    queue: str
    message_id: str
    content: str = ""
    source: str = "user"


class QueueMessageConsumed(Payload):
    queue: str
    message_id: str
    iteration: int | None = None


COMPACTION_REASONS = frozenset({"manual", "threshold", "overflow"})
"""Why a compaction ran. Recorded so an operator can tell a deliberate
``/compact`` apart from one the token budget forced."""


class ContextCompactPending(Payload):
    """The soft threshold was crossed. Nothing has been compacted yet.

    Written once per operation when usage first passes the preparation mark, so
    the decision to compact is visible in the log before it happens rather than
    only afterwards.
    """

    used_tokens: int = 0
    limit_tokens: int = 0
    ratio: float = 0.0
    iteration: int | None = None


class ContextCompacted(Payload):
    """One structured compaction. Only the model's context is replaced.

    The original events, diffs and artifacts stay in the log untouched: this
    payload records the summary that took their place in the *prompt*, not a
    deletion. Every field the plan requires is present even when empty, so a
    consumer never has to guess whether a key was dropped or simply had nothing
    in it.
    """

    reason: str = "threshold"
    used_tokens: int = 0
    limit_tokens: int = 0
    ratio: float = 0.0
    freed_tokens: int = 0
    replaced_messages: int = 0
    iteration: int | None = None
    current_objective: str = ""
    task_progress: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    tool_lessons: list[str] = Field(default_factory=list)
    failed_paths: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class ArtifactStored(Payload):
    """A large tool output moved to a file; the context keeps only this reference.

    ``size`` is the artifact's own byte count, not the truncated preview's, so a
    reader can tell how much was set aside.
    """

    artifact_id: str
    kind: str = "tool_output"
    path: str | None = None
    checksum: str | None = None
    size: int = 0
    tool_name: str | None = None
    call_id: str | None = None
    preview: str | None = None


PAYLOAD_TYPES: dict[EventType, type[Payload]] = {
    EventType.SESSION_CREATED: SessionCreated,
    EventType.OPERATION_STARTED: OperationStarted,
    EventType.MODEL_REQUESTED: ModelRequested,
    EventType.ASSISTANT_MESSAGE: AssistantMessage,
    EventType.APPROVAL_REQUESTED: ApprovalRequested,
    EventType.APPROVAL_RESOLVED: ApprovalResolved,
    EventType.TOOL_STARTED: ToolStarted,
    EventType.TOOL_RESULT: ToolResult,
    EventType.OPERATION_FINISHED: OperationFinished,
    EventType.OPERATION_FAILED: OperationFailed,
    EventType.OPERATION_ABORTED: OperationAborted,
    EventType.OPERATION_SUSPENDED: OperationSuspended,
    EventType.OPERATION_RESUMED: OperationResumed,
    EventType.SNAPSHOT_CREATED: SnapshotCreated,
    EventType.LANE_CREATED: LaneCreated,
    EventType.BRANCH_CREATED: BranchCreated,
    EventType.BRANCH_SWITCHED: BranchSwitched,
    EventType.MODEL_STREAM_COMPLETED: ModelStreamCompleted,
    EventType.PROVIDER_ERROR: ProviderError,
    EventType.QUEUE_MESSAGE_ENQUEUED: QueueMessageEnqueued,
    EventType.QUEUE_MESSAGE_CONSUMED: QueueMessageConsumed,
    EventType.CONTEXT_COMPACT_PENDING: ContextCompactPending,
    EventType.CONTEXT_COMPACTED: ContextCompacted,
    EventType.ARTIFACT_STORED: ArtifactStored,
}


class Event(BaseModel):
    """The immutable envelope persisted to JSONL and indexed in SQLite."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    CURRENT_SCHEMA_VERSION: ClassVar[int] = CURRENT_SCHEMA_VERSION

    schema_version: int = CURRENT_SCHEMA_VERSION
    event_id: str = Field(min_length=1)
    event_type: EventType
    session_id: str = Field(min_length=1)
    seq: int = Field(gt=0)
    timestamp_ms: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1)
    lane_id: str = Field(default=DEFAULT_LANE, min_length=1)
    operation_id: str | None = None
    payload: SerializeAsAny[Payload]

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values
        version = values.get("schema_version", CURRENT_SCHEMA_VERSION)
        if version not in SUPPORTED_SCHEMA_VERSIONS:
            raise EventValidationError(
                "unsupported event schema version",
                details={
                    "schema_version": version,
                    "supported": sorted(SUPPORTED_SCHEMA_VERSIONS),
                },
            )
        raw_type = values.get("event_type", values.get("type"))
        if "type" in values:
            values = {key: value for key, value in values.items() if key != "type"}
            if raw_type is not None:
                values["event_type"] = raw_type
        if not isinstance(raw_type, str):
            return values
        try:
            event_type = EventType(raw_type)
        except ValueError:
            return values
        payload_type = PAYLOAD_TYPES[event_type]
        payload = values.get("payload") or {}
        if not isinstance(payload, payload_type):
            values = {**values, "payload": payload_for_event(event_type, payload)}
        return values

    @property
    def type(self) -> EventType:
        """Compatibility alias useful when reading event streams."""

        return self.event_type

    @classmethod
    def create(
        cls,
        event_type: EventType,
        *,
        session_id: str,
        seq: int,
        payload: Payload | dict[str, Any] | None = None,
        factory: IdFactory | None = None,
        lane_id: str = DEFAULT_LANE,
        operation_id: str | None = None,
        idempotency_key_value: str | None = None,
    ) -> Event:
        factory = factory or IdFactory()
        payload_type = PAYLOAD_TYPES[event_type]
        typed_payload = (
            payload
            if isinstance(payload, payload_type)
            else payload_for_event(event_type, payload or {})
        )
        key = idempotency_key_value or factory.idempotency_key(
            session_id,
            lane_id,
            seq,
            event_type.value,
        )
        return cls(
            event_id=factory.event_id(),
            event_type=event_type,
            session_id=session_id,
            seq=seq,
            timestamp_ms=factory.timestamp_ms(),
            idempotency_key=key,
            lane_id=lane_id,
            operation_id=operation_id,
            payload=typed_payload,
        )

    def to_json_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def payload_for_event(event_type: EventType, payload: Payload | dict[str, Any]) -> Payload:
    """Validate a payload and expose a domain error instead of Pydantic internals."""

    try:
        return PAYLOAD_TYPES[event_type].model_validate(payload)
    except Exception as exc:
        raise EventValidationError(
            f"invalid payload for {event_type.value}",
            details={"error": str(exc)},
        ) from exc
