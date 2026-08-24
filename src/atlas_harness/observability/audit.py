"""The audit view: which events answer which accountability question.

A trace is a timeline; an audit is an answer. The plan names seven questions an
audit log has to settle -- which model ran, which skill was injected, which tool
was called, what was approved, what was truncated, what was compacted and what
was recovered -- and this module exists so each of those has exactly one place to
look rather than a grep pattern per question.

Nothing is derived. Every record is one persisted event, so an audit and a replay
of the same log cannot disagree. Events that answer no audit question are left
out on purpose: an audit log that contains everything is a second copy of the
event log, and a reviewer scrolling past ``queue_message_consumed`` lines is a
reviewer who stops reading.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from atlas_harness.events.models import Event, EventType

CATEGORY_MODEL = "model"
CATEGORY_SKILL = "skill"
CATEGORY_TOOL = "tool"
CATEGORY_APPROVAL = "approval"
CATEGORY_TRUNCATION = "truncation"
CATEGORY_COMPACTION = "compaction"
CATEGORY_RECOVERY = "recovery"
CATEGORY_MCP = "mcp"
CATEGORY_SUBAGENT = "subagent"

AUDIT_CATEGORIES: tuple[str, ...] = (
    CATEGORY_MODEL,
    CATEGORY_SKILL,
    CATEGORY_TOOL,
    CATEGORY_APPROVAL,
    CATEGORY_TRUNCATION,
    CATEGORY_COMPACTION,
    CATEGORY_RECOVERY,
    CATEGORY_MCP,
    CATEGORY_SUBAGENT,
)
"""The plan's seven questions, plus the two M8 surfaces that need the same
treatment: an external tool server and a delegated task are both things a
reviewer has to be able to attribute after the fact."""

_CATEGORY_BY_EVENT: dict[EventType, str] = {
    EventType.MODEL_REQUESTED: CATEGORY_MODEL,
    EventType.MODEL_STREAM_COMPLETED: CATEGORY_MODEL,
    EventType.PROVIDER_ERROR: CATEGORY_MODEL,
    EventType.SKILL_REGISTERED: CATEGORY_SKILL,
    EventType.SKILL_STATUS_CHANGED: CATEGORY_SKILL,
    EventType.CAPABILITY_INJECTED: CATEGORY_SKILL,
    EventType.TOOL_STARTED: CATEGORY_TOOL,
    EventType.TOOL_RESULT: CATEGORY_TOOL,
    EventType.APPROVAL_REQUESTED: CATEGORY_APPROVAL,
    EventType.APPROVAL_RESOLVED: CATEGORY_APPROVAL,
    EventType.ARTIFACT_STORED: CATEGORY_TRUNCATION,
    EventType.CONTEXT_COMPACT_PENDING: CATEGORY_COMPACTION,
    EventType.CONTEXT_COMPACTED: CATEGORY_COMPACTION,
    EventType.OPERATION_SUSPENDED: CATEGORY_RECOVERY,
    EventType.OPERATION_RESUMED: CATEGORY_RECOVERY,
    EventType.SNAPSHOT_CREATED: CATEGORY_RECOVERY,
    EventType.MCP_SERVER_CONNECTED: CATEGORY_MCP,
    EventType.MCP_SERVER_DISCONNECTED: CATEGORY_MCP,
    EventType.MCP_TOOLS_REGISTERED: CATEGORY_MCP,
    EventType.SUBAGENT_TASK_STARTED: CATEGORY_SUBAGENT,
    EventType.SUBAGENT_TASK_FINISHED: CATEGORY_SUBAGENT,
}
"""``artifact_stored`` is the truncation record because that is where a clipped
tool output actually went. A ``tool_result`` carrying ``truncated`` says
something was cut; the artifact says where to read the rest, which is the half a
reviewer needs."""


class AuditRecord(BaseModel):
    """One accountable fact, with the fields that question needs and no others.

    ``subject`` is what the record is *about* -- a model name, a tool name, a
    skill id, a server. It is the column a reviewer filters on, and giving every
    category the same one is what makes a single log answer nine questions.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    seq: int
    timestamp_ms: int
    session_id: str
    event_type: str
    category: str
    subject: str = ""
    operation_id: str | None = None
    lane_id: str = "main"
    outcome: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def render(self) -> str:
        outcome = f" {self.outcome}" if self.outcome else ""
        subject = f" {self.subject}" if self.subject else ""
        return f"{self.seq:>4}  {self.category:<11}{subject}{outcome}"


class AuditLog(BaseModel):
    """One session's accountable events, in log order."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    records: tuple[AuditRecord, ...] = ()

    def of_category(self, category: str) -> tuple[AuditRecord, ...]:
        return tuple(record for record in self.records if record.category == category)

    def counts(self) -> dict[str, int]:
        counts = {category: 0 for category in AUDIT_CATEGORIES}
        for record in self.records:
            counts[record.category] = counts.get(record.category, 0) + 1
        return counts

    def subjects(self, category: str) -> tuple[str, ...]:
        """Distinct subjects in one category, in first-seen order.

        This is the direct answer to "which models ran" or "which tools were
        called" -- the question is about the set, not the sequence.
        """

        seen: list[str] = []
        for record in self.of_category(category):
            if record.subject and record.subject not in seen:
                seen.append(record.subject)
        return tuple(seen)

    def unanswered(self) -> tuple[str, ...]:
        """Categories with no record at all.

        A reviewer needs to tell "nothing was approved" from "approvals are not
        being logged", and an empty category is the only signal that separates
        them at this level.
        """

        counts = self.counts()
        return tuple(category for category in AUDIT_CATEGORIES if counts.get(category, 0) == 0)

    def summary(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "records": len(self.records),
            "counts": self.counts(),
            "models": list(self.subjects(CATEGORY_MODEL)),
            "tools": list(self.subjects(CATEGORY_TOOL)),
            "skills": list(self.subjects(CATEGORY_SKILL)),
            "mcp_servers": list(self.subjects(CATEGORY_MCP)),
            "subagent_tasks": list(self.subjects(CATEGORY_SUBAGENT)),
            "unanswered": list(self.unanswered()),
        }

    def render(self) -> list[str]:
        return [record.render() for record in self.records]


def _subject_and_outcome(event: Event, payload: dict[str, Any]) -> tuple[str, str]:
    """Pull the filter column and the verdict out of one payload."""

    match event.event_type:
        case EventType.MODEL_REQUESTED:
            return f"{payload.get('provider')}/{payload.get('model')}", "requested"
        case EventType.MODEL_STREAM_COMPLETED:
            return (
                f"{payload.get('provider')}/{payload.get('model')}",
                str(payload.get("stop_reason") or "completed"),
            )
        case EventType.PROVIDER_ERROR:
            return (
                f"{payload.get('provider')}/{payload.get('model')}",
                str(payload.get("error_code") or "error"),
            )
        case EventType.SKILL_REGISTERED:
            return (
                f"{payload.get('skill_id')}@{payload.get('version')}",
                str(payload.get("status") or "registered"),
            )
        case EventType.SKILL_STATUS_CHANGED:
            return (
                f"{payload.get('skill_id')}@{payload.get('version')}",
                f"{payload.get('from_status')}->{payload.get('to_status')}",
            )
        case EventType.CAPABILITY_INJECTED:
            return str(payload.get("query") or "injection"), "injected"
        case EventType.TOOL_STARTED:
            return str(payload.get("tool_name") or ""), "started"
        case EventType.TOOL_RESULT:
            outcome = "ok" if payload.get("success", True) else "failed"
            return str(payload.get("tool_name") or ""), outcome
        case EventType.APPROVAL_REQUESTED:
            return str(payload.get("tool_name") or payload.get("approval_id") or ""), "requested"
        case EventType.APPROVAL_RESOLVED:
            outcome = "approved" if payload.get("approved") else "denied"
            return str(payload.get("approval_id") or ""), outcome
        case EventType.ARTIFACT_STORED:
            return str(payload.get("artifact_id") or payload.get("kind") or ""), "stored"
        case EventType.CONTEXT_COMPACT_PENDING:
            return "context", "pending"
        case EventType.CONTEXT_COMPACTED:
            return "context", str(payload.get("reason") or "compacted")
        case EventType.OPERATION_SUSPENDED:
            return event.operation_id or "", str(payload.get("reason") or "suspended")
        case EventType.OPERATION_RESUMED:
            return event.operation_id or "", "resumed"
        case EventType.SNAPSHOT_CREATED:
            return str(payload.get("snapshot_id") or ""), "created"
        case EventType.MCP_SERVER_CONNECTED:
            return str(payload.get("server") or ""), "connected"
        case EventType.MCP_SERVER_DISCONNECTED:
            return str(payload.get("server") or ""), str(payload.get("reason") or "disconnected")
        case EventType.MCP_TOOLS_REGISTERED:
            return str(payload.get("server") or ""), "registered"
        case EventType.SUBAGENT_TASK_STARTED:
            return str(payload.get("task_id") or ""), "started"
        case EventType.SUBAGENT_TASK_FINISHED:
            return str(payload.get("task_id") or ""), str(payload.get("outcome") or "finished")
    return "", ""


def _detail(event: Event, payload: dict[str, Any]) -> dict[str, Any]:
    """The few payload fields this question actually needs.

    Copying the whole payload would put tool arguments and assistant text into
    the audit file, which is the one file most likely to be shipped somewhere
    else. Payloads are already redacted, but a narrower record is still the
    right default for something meant to be handed to a reviewer.
    """

    keys: tuple[str, ...]
    match event.event_type:
        case EventType.MODEL_REQUESTED:
            keys = ("iteration", "message_count", "tool_count", "request_id")
        case EventType.MODEL_STREAM_COMPLETED:
            keys = ("iteration", "input_tokens", "output_tokens", "tool_call_count")
        case EventType.PROVIDER_ERROR:
            keys = ("error", "retryable", "attempt", "status_code")
        case EventType.SKILL_REGISTERED:
            keys = ("required_scopes", "source_path", "checksum")
        case EventType.SKILL_STATUS_CHANGED:
            keys = ("evaluation_ref", "reason")
        case EventType.CAPABILITY_INJECTED:
            keys = ("selected", "skipped", "tokens_used", "token_budget")
        case EventType.TOOL_STARTED:
            keys = ("call_id", "risk", "idempotent", "idempotency_key")
        case EventType.TOOL_RESULT:
            keys = ("call_id", "error_code", "error", "truncated", "duration_ms")
        case EventType.APPROVAL_REQUESTED:
            keys = ("approval_id", "reason", "risk")
        case EventType.APPROVAL_RESOLVED:
            keys = ("reason", "approver")
        case EventType.ARTIFACT_STORED:
            keys = ("kind", "size", "checksum", "path", "tool_name", "call_id")
        case EventType.CONTEXT_COMPACT_PENDING:
            keys = ("used_tokens", "limit_tokens", "ratio", "iteration")
        case EventType.CONTEXT_COMPACTED:
            keys = ("used_tokens", "limit_tokens", "freed_tokens", "replaced_messages")
        case EventType.OPERATION_SUSPENDED:
            keys = ("pending_tool_call_ids", "detail")
        case EventType.OPERATION_RESUMED:
            keys = ("resumed_from_seq", "confirmed_tool_call_ids", "replayed_tool_call_ids")
        case EventType.SNAPSHOT_CREATED:
            keys = ("last_seq", "state_hash", "event_count")
        case EventType.MCP_SERVER_CONNECTED:
            keys = ("transport", "address", "protocol_version", "tool_count")
        case EventType.MCP_SERVER_DISCONNECTED:
            keys = ("detail", "duration_ms")
        case EventType.MCP_TOOLS_REGISTERED:
            keys = ("tools", "rejected", "granted_scopes")
        case EventType.SUBAGENT_TASK_STARTED:
            keys = ("child_session_id", "allowed_tools", "max_tokens", "deadline_ms")
        case EventType.SUBAGENT_TASK_FINISHED:
            keys = ("child_session_id", "error_code", "tool_calls", "total_tokens", "duration_ms")
        case _:
            keys = ()
    return {key: payload[key] for key in keys if key in payload and payload[key] is not None}


def audit_record(event: Event) -> AuditRecord | None:
    """Flatten one event, or ``None`` if it answers no audit question."""

    category = _CATEGORY_BY_EVENT.get(event.event_type)
    if category is None:
        return None
    payload = event.payload.model_dump(mode="json")
    subject, outcome = _subject_and_outcome(event, payload)
    return AuditRecord(
        seq=event.seq,
        timestamp_ms=event.timestamp_ms,
        session_id=event.session_id,
        event_type=event.event_type.value,
        category=category,
        subject=subject,
        operation_id=event.operation_id,
        lane_id=event.lane_id,
        outcome=outcome,
        detail=_detail(event, payload),
    )


def build_audit(events: Iterable[Event], *, session_id: str | None = None) -> AuditLog:
    """Select and flatten the accountable events from one session's log."""

    ordered = list(events)
    records = tuple(
        record for record in (audit_record(event) for event in ordered) if record is not None
    )
    resolved = session_id or (ordered[0].session_id if ordered else "")
    return AuditLog(session_id=resolved, records=records)
