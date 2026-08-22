"""Render an event log as a human-readable timeline.

M3's completion condition is that every step of a run can be replayed. The
projection in :mod:`atlas_harness.events.reducer` answers *what the state is*;
this module answers *what happened, in order*, which is what an operator
actually reads when a run went sideways.

Nothing here re-derives facts. Each line is one persisted event, so a trace and
a replay can never disagree. The renderer is deliberately read-only and has no
knowledge of the loop that produced the events.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from atlas_harness.events.models import Event, EventType

MAX_LINE_CHARS = 160
"""Traces are scanned, not read. Long payload text is cut, never wrapped."""


class TraceLine(BaseModel):
    """One event, flattened for display."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    seq: int
    timestamp_ms: int
    event_type: str
    lane_id: str
    operation_id: str | None = None
    detail: str = ""

    def render(self, *, base_ms: int | None = None) -> str:
        """Format as one line, with time relative to the first event."""

        offset = "" if base_ms is None else f"+{self.timestamp_ms - base_ms:>6}ms "
        detail = f"  {self.detail}" if self.detail else ""
        return f"{self.seq:>4}  {offset}{self.event_type}{detail}"


class Trace(BaseModel):
    """An ordered, renderable view of one session's log."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    lines: tuple[TraceLine, ...] = ()

    @property
    def base_ms(self) -> int | None:
        return self.lines[0].timestamp_ms if self.lines else None

    def render(self) -> list[str]:
        base = self.base_ms
        return [line.render(base_ms=base) for line in self.lines]

    def counts(self) -> dict[str, int]:
        """Events per type, useful as a compact assertion target in tests."""

        counts: dict[str, int] = {}
        for line in self.lines:
            counts[line.event_type] = counts.get(line.event_type, 0) + 1
        return counts

    def of_type(self, event_type: EventType) -> tuple[TraceLine, ...]:
        return tuple(line for line in self.lines if line.event_type == event_type.value)

    def summary(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "events": len(self.lines),
            "counts": self.counts(),
        }


def _clip(text: str) -> str:
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= MAX_LINE_CHARS:
        return collapsed
    return f"{collapsed[: MAX_LINE_CHARS - 1]}…"


def _detail(event: Event) -> str:
    """One line describing this event, drawn only from its own payload.

    Payloads already passed through redaction on the way in, so the values here
    are safe to print. Model and tool text is reported by length or preview
    rather than in full, because a trace is a timeline and not a transcript.
    """

    payload = event.payload.model_dump(mode="python")
    match event.event_type:
        case EventType.SESSION_CREATED:
            return _clip(str(payload.get("title") or ""))
        case EventType.OPERATION_STARTED:
            return _clip(str(payload.get("name") or ""))
        case EventType.MODEL_REQUESTED:
            return _clip(
                f"{payload.get('provider')}/{payload.get('model')} "
                f"iteration={payload.get('iteration')} "
                f"messages={payload.get('message_count')} tools={payload.get('tool_count')}"
            )
        case EventType.MODEL_STREAM_COMPLETED:
            return _clip(
                f"stop={payload.get('stop_reason')} "
                f"text={payload.get('text_length')}c "
                f"tool_calls={payload.get('tool_call_count')} "
                f"tokens={payload.get('input_tokens')}/{payload.get('output_tokens')}"
            )
        case EventType.ASSISTANT_MESSAGE:
            return _clip(str(payload.get("content") or ""))
        case EventType.PROVIDER_ERROR:
            return _clip(
                f"{payload.get('error_code')}: {payload.get('error')} "
                f"retryable={payload.get('retryable')}"
            )
        case EventType.APPROVAL_REQUESTED:
            return _clip(f"{payload.get('tool_name')} {payload.get('reason') or ''}")
        case EventType.APPROVAL_RESOLVED:
            verdict = "approved" if payload.get("approved") else "denied"
            return _clip(f"{verdict} {payload.get('reason') or ''}")
        case EventType.TOOL_STARTED:
            return _clip(f"{payload.get('tool_name')} {payload.get('arguments') or {}}")
        case EventType.TOOL_RESULT:
            verdict = "ok" if payload.get("success", True) else f"fail {payload.get('error_code')}"
            return _clip(f"{payload.get('tool_name')} {verdict} {payload.get('duration_ms', 0)}ms")
        case EventType.QUEUE_MESSAGE_ENQUEUED:
            return _clip(f"{payload.get('queue')} <- {payload.get('content') or ''}")
        case EventType.QUEUE_MESSAGE_CONSUMED:
            return _clip(f"{payload.get('queue')} -> iteration={payload.get('iteration')}")
        case EventType.OPERATION_FINISHED:
            result = payload.get("result")
            if isinstance(result, dict):
                return _clip(
                    f"stop={result.get('stop_cause')} "
                    f"iterations={result.get('iterations')} "
                    f"tool_calls={result.get('tool_calls')}"
                )
            return _clip(str(result or ""))
        case EventType.OPERATION_FAILED:
            return _clip(f"{payload.get('error_code')}: {payload.get('error')}")
        case EventType.OPERATION_ABORTED:
            return _clip(str(payload.get("reason") or ""))
        case EventType.SNAPSHOT_CREATED:
            return _clip(str(payload.get("snapshot_id") or ""))
    return ""


def trace_line(event: Event) -> TraceLine:
    return TraceLine(
        seq=event.seq,
        timestamp_ms=event.timestamp_ms,
        event_type=event.event_type.value,
        lane_id=event.lane_id,
        operation_id=event.operation_id,
        detail=_detail(event),
    )


def build_trace(events: list[Event], *, session_id: str | None = None) -> Trace:
    """Flatten an ordered event sequence into a renderable trace."""

    resolved = session_id or (events[0].session_id if events else "")
    return Trace(
        session_id=resolved,
        lines=tuple(trace_line(event) for event in events),
    )
