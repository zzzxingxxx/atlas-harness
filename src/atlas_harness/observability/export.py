"""Write the four files the plan names: trace, audit, metrics and replay report.

The plan's M8 deliverable is ``trace.jsonl``, ``audit.jsonl``, ``metrics.json``
and ``replay-report.json``. All four are derived from one read of the event log
and nothing else, which is the property that makes them usable as evidence: a
metrics file computed from a live runtime and one computed from the log weeks
later have to agree, and they can only be guaranteed to agree if the log is the
only input.

The two JSONL files are per-event and the two JSON files are per-session. That
split is not cosmetic. A trace and an audit are appended to and read a line at a
time by tools that do not want to parse a whole file; a metrics summary and a
replay verdict are single answers, and streaming them a line at a time would
invite a consumer to read half of one.

Nothing here re-runs anything. ``replay-report.json`` reports whether the log
*replays* -- whether folding it produces the same state hash and whether every
operation reached a terminal event -- not whether the model would answer the same
way today. Those are different claims, and only the first one is checkable from a
log alone.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from atlas_harness.events.models import Event, EventType
from atlas_harness.events.reducer import Reducer, SessionState
from atlas_harness.observability.audit import AuditLog, build_audit
from atlas_harness.observability.trace import Trace, build_trace

TRACE_FILENAME = "trace.jsonl"
AUDIT_FILENAME = "audit.jsonl"
METRICS_FILENAME = "metrics.json"
REPLAY_REPORT_FILENAME = "replay-report.json"

EXPORT_FILENAMES: tuple[str, ...] = (
    TRACE_FILENAME,
    AUDIT_FILENAME,
    METRICS_FILENAME,
    REPLAY_REPORT_FILENAME,
)


class RunMetrics(BaseModel):
    """Counters an operator reads before deciding whether to open the trace.

    These are counts of persisted events, not of things the runtime believes it
    did. A model request that was never logged is not counted here, which is the
    point: an under-count is a logging bug worth seeing, and a runtime-reported
    number would paper over it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    status: str = "created"
    events: int = 0
    last_seq: int = 0
    operations: int = 0
    open_operations: int = 0
    model_requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    provider_errors: int = 0
    tool_calls: int = 0
    tool_failures: int = 0
    approvals_requested: int = 0
    approvals_denied: int = 0
    artifacts: int = 0
    compactions: int = 0
    capability_injections: int = 0
    mcp_servers_connected: int = 0
    mcp_tools_registered: int = 0
    subagent_tasks: int = 0
    subagent_failures: int = 0
    duration_ms: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def as_json(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        payload["total_tokens"] = self.total_tokens
        return payload


class ReplayReport(BaseModel):
    """Whether this log still folds to the state it folded to before.

    ``state_hash`` is the whole verdict. The reducer is pure, so folding the same
    events twice must produce the same hash; a mismatch against a recorded one
    means either the log changed underneath us or the reducer's behaviour did,
    and both are things a release has to catch before a user does.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    replayed_events: int = 0
    last_seq: int = 0
    schema_version: int = 0
    state_hash: str = ""
    expected_state_hash: str | None = None
    deterministic: bool = True
    operations: int = 0
    unfinished_operation_ids: tuple[str, ...] = ()
    suspended_operation_ids: tuple[str, ...] = ()
    open_subagent_task_ids: tuple[str, ...] = ()
    gaps: tuple[int, ...] = ()
    """Sequence numbers missing from the log. A gap means the log is not the
    whole story, and every projection built on it is suspect."""

    problems: tuple[str, ...] = Field(default_factory=tuple)

    @property
    def clean(self) -> bool:
        """The log replays and left nothing dangling.

        An unfinished operation counts as a problem rather than a warning. It is
        the exact signature of a crashed run, and a report that called that clean
        would make the file useless for the thing it exists to check.
        """

        return not self.problems

    def as_json(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        payload["clean"] = self.clean
        return payload

    def render(self) -> list[str]:
        lines = [
            f"session: {self.session_id}",
            f"replayed: {self.replayed_events} events up to seq {self.last_seq}",
            f"schema_version: {self.schema_version}",
            f"state_hash: {self.state_hash}",
            f"verdict: {'clean' if self.clean else 'problems'}",
        ]
        lines.extend(f"  - {problem}" for problem in self.problems)
        return lines


def _sequence_gaps(events: list[Event]) -> tuple[int, ...]:
    """Sequence numbers absent between the first and last event.

    Truncation at the tail is not a gap: a log cut short by a crash is expected
    and recovery handles it. A hole in the middle is not recoverable, because the
    events after it were folded on top of a state that never existed.
    """

    if not events:
        return ()
    seen = {event.seq for event in events}
    lowest = min(seen)
    highest = max(seen)
    return tuple(seq for seq in range(lowest, highest + 1) if seq not in seen)


def build_metrics(
    events: list[Event],
    *,
    state: SessionState | None = None,
    session_id: str | None = None,
) -> RunMetrics:
    """Count one session's log into the metrics file's shape."""

    projected = state if state is not None else _projected(events, session_id)
    counts: dict[EventType, int] = {}
    input_tokens = 0
    output_tokens = 0
    tool_failures = 0
    approvals_denied = 0
    subagent_failures = 0

    for event in events:
        counts[event.event_type] = counts.get(event.event_type, 0) + 1
        payload = event.payload.model_dump(mode="python")
        if event.event_type is EventType.MODEL_STREAM_COMPLETED:
            input_tokens += int(payload.get("input_tokens") or 0)
            output_tokens += int(payload.get("output_tokens") or 0)
        elif event.event_type is EventType.TOOL_RESULT and not payload.get("success", True):
            tool_failures += 1
        elif event.event_type is EventType.APPROVAL_RESOLVED and not payload.get("approved"):
            approvals_denied += 1
        elif event.event_type is EventType.SUBAGENT_TASK_FINISHED:
            if str(payload.get("outcome") or "") != "completed":
                subagent_failures += 1

    span = 0
    if events:
        span = max(0, events[-1].timestamp_ms - events[0].timestamp_ms)

    return RunMetrics(
        session_id=projected.session_id,
        status=projected.status,
        events=len(events),
        last_seq=projected.last_seq,
        operations=len(projected.operations),
        open_operations=len(projected.unfinished_operation_ids),
        model_requests=counts.get(EventType.MODEL_REQUESTED, 0),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        provider_errors=counts.get(EventType.PROVIDER_ERROR, 0),
        tool_calls=counts.get(EventType.TOOL_STARTED, 0),
        tool_failures=tool_failures,
        approvals_requested=counts.get(EventType.APPROVAL_REQUESTED, 0),
        approvals_denied=approvals_denied,
        artifacts=counts.get(EventType.ARTIFACT_STORED, 0),
        compactions=counts.get(EventType.CONTEXT_COMPACTED, 0),
        capability_injections=counts.get(EventType.CAPABILITY_INJECTED, 0),
        mcp_servers_connected=counts.get(EventType.MCP_SERVER_CONNECTED, 0),
        mcp_tools_registered=counts.get(EventType.MCP_TOOLS_REGISTERED, 0),
        subagent_tasks=counts.get(EventType.SUBAGENT_TASK_STARTED, 0),
        subagent_failures=subagent_failures,
        duration_ms=span,
    )


def _projected(events: list[Event], session_id: str | None = None) -> SessionState:
    """Replay the log, tolerating an empty one and a hole in the middle.

    ``replay`` refuses an empty stream with no session id, and rightly so -- it has
    nothing to name the projection after. An export of a session that has not been
    written to yet is still a legitimate request, so the empty case is answered here
    rather than pushed onto every caller.

    The fold is non-strict for the same reason: this is the report an operator reads
    *because* a log looks damaged, so a missing seq has to come back as a reported
    gap rather than as the exception that stopped the report being written. An intact
    log folds identically either way, since every seq is then the expected one.
    """

    if not events:
        return SessionState(session_id=session_id or "")
    target = session_id if session_id is not None else events[0].session_id
    return Reducer(target, strict_seq=False).reduce(events)


def build_replay_report(
    events: list[Event],
    *,
    session_id: str | None = None,
    expected_state_hash: str | None = None,
) -> ReplayReport:
    """Fold the log and report whether it replayed cleanly."""

    state = _projected(events, session_id)
    state_hash = state.state_hash()
    gaps = _sequence_gaps(events)

    unfinished = tuple(state.unfinished_operation_ids)
    suspended = tuple(state.suspended_operation_ids)
    open_tasks = tuple(state.open_subagent_task_ids)

    deterministic = expected_state_hash is None or expected_state_hash == state_hash
    problems: list[str] = []
    if not deterministic:
        problems.append(f"state hash {state_hash} does not match recorded {expected_state_hash}")
    if gaps:
        problems.append(f"log is missing seq {list(gaps)}")
    for operation_id in unfinished:
        status = state.operations[operation_id].status
        problems.append(f"operation {operation_id} is still {status}")
    for task_id in open_tasks:
        problems.append(f"sub-agent task {task_id} was dispatched and never reported an outcome")

    return ReplayReport(
        session_id=state.session_id,
        replayed_events=len(events),
        last_seq=state.last_seq,
        schema_version=state.schema_version,
        state_hash=state_hash,
        expected_state_hash=expected_state_hash,
        deterministic=deterministic,
        operations=len(state.operations),
        unfinished_operation_ids=unfinished,
        suspended_operation_ids=suspended,
        open_subagent_task_ids=open_tasks,
        gaps=gaps,
        problems=tuple(problems),
    )


class ExportBundle(BaseModel):
    """All four artefacts, built from one read of the log."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    session_id: str
    trace: Trace
    audit: AuditLog
    metrics: RunMetrics
    replay: ReplayReport

    def files(self) -> dict[str, str]:
        """Filename to file body, so a caller can write or serve either one.

        Returning text rather than writing is what lets the HTTP layer hand these
        back over a response without a temp directory, and the CLI write them to
        disk, from the same construction.
        """

        return {
            TRACE_FILENAME: _jsonl(line.model_dump(mode="json") for line in self.trace.lines),
            AUDIT_FILENAME: _jsonl(record.as_json() for record in self.audit.records),
            METRICS_FILENAME: _json(self.metrics.as_json()),
            REPLAY_REPORT_FILENAME: _json(self.replay.as_json()),
        }

    def summary(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "trace": self.trace.summary(),
            "audit": self.audit.summary(),
            "metrics": self.metrics.as_json(),
            "replay": self.replay.as_json(),
        }

    def write(self, directory: Path) -> dict[str, Path]:
        """Write all four files and return where each one landed."""

        directory.mkdir(parents=True, exist_ok=True)
        written: dict[str, Path] = {}
        for filename, body in self.files().items():
            path = directory / filename
            path.write_text(body, encoding="utf-8")
            written[filename] = path
        return written


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _jsonl(rows: Any) -> str:
    lines = [json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows]
    return "".join(f"{line}\n" for line in lines)


def build_bundle(
    events: list[Event],
    *,
    session_id: str | None = None,
    expected_state_hash: str | None = None,
) -> ExportBundle:
    """Build all four artefacts from one event list.

    Taking the events once rather than a store is what keeps this usable against
    a historical log, a snapshot-restored one and a live session without three
    code paths that could disagree.
    """

    resolved = session_id or (events[0].session_id if events else "")
    state = _projected(events, resolved)
    return ExportBundle(
        session_id=resolved,
        trace=build_trace(events, session_id=resolved),
        audit=build_audit(events, session_id=resolved),
        metrics=build_metrics(events, state=state),
        replay=build_replay_report(
            events,
            session_id=resolved,
            expected_state_hash=expected_state_hash,
        ),
    )
