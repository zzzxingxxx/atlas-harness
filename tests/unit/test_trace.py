"""The trace renderer: one line per persisted event, nothing re-derived.

The property that matters here is that a trace cannot disagree with a replay.
These tests check that by rendering real logs rather than hand-built payloads,
so a field renamed in an event payload shows up as a blank detail here.
"""

from __future__ import annotations

from tests.conftest import DEMO_SESSION_ID

from atlas_harness.events import Event, EventStore, EventType
from atlas_harness.observability.trace import (
    MAX_LINE_CHARS,
    Trace,
    build_trace,
    trace_line,
)


def details(trace: Trace, event_type: EventType) -> list[str]:
    return [line.detail for line in trace.of_type(event_type)]


def test_a_trace_has_one_line_per_event_in_log_order(
    seeded: tuple[EventStore, str],
) -> None:
    store, session_id = seeded
    events = store.read_events(session_id)

    trace = build_trace(events)

    assert trace.session_id == session_id
    assert len(trace.lines) == len(events)
    assert [line.seq for line in trace.lines] == [event.seq for event in events]
    assert [line.event_type for line in trace.lines] == [e.event_type.value for e in events]


def test_counts_match_the_log(seeded: tuple[EventStore, str]) -> None:
    store, session_id = seeded
    events = store.read_events(session_id)

    counts = build_trace(events).counts()

    assert sum(counts.values()) == len(events)
    assert counts["session_created"] == 1
    assert counts["operation_finished"] == 1


def test_the_session_id_can_be_supplied_for_an_empty_log() -> None:
    trace = build_trace([], session_id=DEMO_SESSION_ID)

    assert trace.session_id == DEMO_SESSION_ID
    assert trace.lines == ()
    assert trace.render() == []
    assert trace.base_ms is None
    assert trace.summary() == {"session_id": DEMO_SESSION_ID, "events": 0, "counts": {}}


def test_offsets_are_relative_to_the_first_event(store: EventStore, seed: object) -> None:
    """A trace reads as elapsed time, so the first line is always +0ms."""

    events = seed(store)  # type: ignore[operator]
    trace = build_trace(events)

    rendered = trace.render()

    assert trace.base_ms == events[0].timestamp_ms
    assert "+     0ms" in rendered[0]
    for line, event in zip(trace.lines, events, strict=True):
        assert f"+{event.timestamp_ms - events[0].timestamp_ms:>6}ms" in line.render(
            base_ms=trace.base_ms
        )


def test_a_line_without_a_base_omits_the_offset(seeded: tuple[EventStore, str]) -> None:
    store, session_id = seeded
    event = store.read_events(session_id)[0]

    line = trace_line(event)

    assert line.render() == f"{event.seq:>4}  session_created  demo"
    assert "ms" not in line.render()


def test_long_details_are_clipped_not_wrapped(store: EventStore) -> None:
    store.append_new(
        EventType.SESSION_CREATED,
        session_id="ses_long",
        payload={"title": "t" * (MAX_LINE_CHARS * 2)},
    )

    trace = build_trace(store.read_events("ses_long"))

    detail = trace.lines[0].detail
    assert len(detail) == MAX_LINE_CHARS
    assert detail.endswith("…")
    assert "\n" not in trace.render()[0]


def test_whitespace_in_a_detail_is_collapsed(store: EventStore) -> None:
    store.append_new(
        EventType.ASSISTANT_MESSAGE,
        session_id="ses_ws",
        payload={"content": "first line\n\n  second   line\t"},
    )

    trace = build_trace(store.read_events("ses_ws"))

    assert trace.lines[0].detail == "first line second line"


def test_every_event_type_renders_a_detail(store: EventStore) -> None:
    """A missing ``match`` arm shows up as a blank line, so cover them all."""

    session_id = "ses_all"
    payloads: dict[EventType, dict[str, object]] = {
        EventType.SESSION_CREATED: {"title": "everything"},
        EventType.OPERATION_STARTED: {"name": "agent_run"},
        EventType.MODEL_REQUESTED: {
            "provider": "fake",
            "model": "fake-model",
            "iteration": 1,
            "message_count": 2,
            "tool_count": 4,
        },
        EventType.MODEL_STREAM_COMPLETED: {
            "stop_reason": "tool_use",
            "text_length": 12,
            "tool_call_count": 1,
            "input_tokens": 30,
            "output_tokens": 7,
        },
        EventType.ASSISTANT_MESSAGE: {"content": "here is the answer"},
        EventType.PROVIDER_ERROR: {
            "error": "gateway timed out",
            "error_code": "provider_timeout",
            "retryable": True,
        },
        EventType.APPROVAL_REQUESTED: {
            "approval_id": "apr_1",
            "tool_name": "write_file",
            "reason": "writes are gated",
        },
        EventType.APPROVAL_RESOLVED: {
            "approval_id": "apr_1",
            "approved": True,
            "reason": "operator said yes",
        },
        EventType.TOOL_STARTED: {"tool_name": "read_file", "arguments": {"path": "a.txt"}},
        EventType.TOOL_RESULT: {"tool_name": "read_file", "success": True, "duration_ms": 3},
        EventType.QUEUE_MESSAGE_ENQUEUED: {
            "queue": "steer",
            "message_id": "msg_1",
            "content": "be brief",
        },
        EventType.QUEUE_MESSAGE_CONSUMED: {
            "queue": "steer",
            "message_id": "msg_1",
            "iteration": 2,
        },
        EventType.SNAPSHOT_CREATED: {"snapshot_id": "snap_1"},
        EventType.OPERATION_FINISHED: {
            "result": {"stop_cause": "completed", "iterations": 2, "tool_calls": 1}
        },
    }
    for event_type, payload in payloads.items():
        store.append_new(event_type, session_id=session_id, payload=payload)

    trace = build_trace(store.read_events(session_id))

    assert set(trace.counts()) == {event_type.value for event_type in payloads}
    for line in trace.lines:
        assert line.detail, f"{line.event_type} rendered a blank detail"


def test_model_requested_reports_the_counts_the_loop_writes(store: EventStore) -> None:
    """Guards against a payload field rename silently blanking the detail."""

    store.append_new(
        EventType.MODEL_REQUESTED,
        session_id="ses_req",
        payload={
            "provider": "fake",
            "model": "fake-model",
            "iteration": 3,
            "message_count": 5,
            "tool_count": 4,
        },
    )

    detail = build_trace(store.read_events("ses_req")).lines[0].detail

    assert detail == "fake/fake-model iteration=3 messages=5 tools=4"


def test_a_failed_operation_reports_its_code(store: EventStore) -> None:
    store.append_new(
        EventType.OPERATION_FAILED,
        session_id="ses_fail",
        payload={"error": "stream broke", "error_code": "provider_incomplete_stream"},
    )
    store.append_new(
        EventType.OPERATION_ABORTED,
        session_id="ses_fail",
        payload={"reason": "run was cancelled"},
    )

    trace = build_trace(store.read_events("ses_fail"))

    assert details(trace, EventType.OPERATION_FAILED) == [
        "provider_incomplete_stream: stream broke"
    ]
    assert details(trace, EventType.OPERATION_ABORTED) == ["run was cancelled"]


def test_a_failed_tool_result_says_so(store: EventStore) -> None:
    store.append_new(
        EventType.TOOL_RESULT,
        session_id="ses_tr",
        payload={
            "tool_name": "read_file",
            "success": False,
            "error": "no such file",
            "error_code": "tool_error",
            "duration_ms": 2,
        },
    )

    detail = build_trace(store.read_events("ses_tr")).lines[0].detail

    assert detail == "read_file fail tool_error 2ms"


def test_a_denied_approval_says_denied(store: EventStore) -> None:
    store.append_new(
        EventType.APPROVAL_RESOLVED,
        session_id="ses_ap",
        payload={"approval_id": "apr_1", "approved": False, "reason": "policy"},
    )

    assert build_trace(store.read_events("ses_ap")).lines[0].detail == "denied policy"


def test_a_string_operation_result_still_renders(seeded: tuple[EventStore, str]) -> None:
    """M1 and M2 logs store a plain string result; those must not blank out."""

    store, session_id = seeded

    trace = build_trace(store.read_events(session_id))

    assert details(trace, EventType.OPERATION_FINISHED) == ["done"]


def test_the_operation_id_is_carried_through(seeded: tuple[EventStore, str]) -> None:
    store, session_id = seeded
    trace = build_trace(store.read_events(session_id))

    session_line, *rest = trace.lines
    assert session_line.operation_id is None
    assert all(line.operation_id == "op_demo" for line in rest)


def test_a_trace_line_is_frozen(seeded: tuple[EventStore, str]) -> None:
    store, session_id = seeded
    line = trace_line(store.read_events(session_id)[0])

    assert line.model_config["frozen"] is True


def test_the_trace_never_prints_a_secret_the_log_redacted(store: EventStore) -> None:
    """Redaction happens on the way in; the trace must not undo it."""

    store.append_new(
        EventType.QUEUE_MESSAGE_ENQUEUED,
        session_id="ses_secret",
        payload={"queue": "steer", "message_id": "msg_1", "content": "[redacted]"},
    )

    assert "[redacted]" in build_trace(store.read_events("ses_secret")).lines[0].detail


def test_lane_ids_reach_the_line(store: EventStore) -> None:
    store.append_new(
        EventType.SESSION_CREATED,
        session_id="ses_lane",
        payload={"title": "lanes"},
        lane_id="lane_b",
    )

    assert build_trace(store.read_events("ses_lane")).lines[0].lane_id == "lane_b"


def test_of_type_filters_without_reordering(seeded: tuple[EventStore, str]) -> None:
    store, session_id = seeded
    events: list[Event] = store.read_events(session_id)
    trace = build_trace(events)

    lines = trace.of_type(EventType.MODEL_REQUESTED)

    assert len(lines) == 1
    assert lines[0].seq == next(e.seq for e in events if e.event_type is EventType.MODEL_REQUESTED)
