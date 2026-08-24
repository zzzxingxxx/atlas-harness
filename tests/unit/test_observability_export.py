"""One log in, four artefacts out, and nothing else consulted.

The property under test is that the export is a pure function of the event list.
Every assertion here is made against a log built in the test, not against a
runtime that was driven -- because that is exactly the guarantee an operator
relies on when they replay a log from last month and expect the same metrics
file the runtime wrote at the time.

The negative cases carry as much weight as the positive ones. An empty log, a
log with a hole in the middle, an operation that never finished and a sub-agent
task that never reported are all things a real log does, and a report that
called any of them clean would be worse than no report.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.conftest import DEMO_SESSION_ID, seed_session

from atlas_harness.events import Event, EventStore, EventType
from atlas_harness.observability.audit import AUDIT_CATEGORIES, build_audit
from atlas_harness.observability.export import (
    AUDIT_FILENAME,
    EXPORT_FILENAMES,
    METRICS_FILENAME,
    REPLAY_REPORT_FILENAME,
    TRACE_FILENAME,
    _sequence_gaps,
    build_bundle,
    build_metrics,
    build_replay_report,
)


def append(
    store: EventStore,
    event_type: EventType,
    payload: dict[str, object],
    *,
    session_id: str = DEMO_SESSION_ID,
) -> Event:
    return store.append_new(event_type, session_id=session_id, payload=payload)


def rich_log(store: EventStore) -> list[Event]:
    """A session that touched every audit category the plan asks about.

    The plan's M8 test is that the audit log can answer questions about the
    model, skills, tools, approvals, truncation, compaction and recovery. This
    builds one log that exercises all of them plus MCP and sub-agents, so the
    category coverage assertion below is about the mapping rather than about
    seven separate fixtures.
    """

    seed_session(store)
    append(
        store,
        EventType.APPROVAL_REQUESTED,
        {"approval_id": "apr_1", "tool_name": "write_file", "call_id": "c2"},
    )
    append(
        store,
        EventType.APPROVAL_RESOLVED,
        {"approval_id": "apr_1", "approved": False, "reason": "denied"},
    )
    append(
        store,
        EventType.TOOL_RESULT,
        {"tool_name": "write_file", "call_id": "c2", "success": False, "error_code": "denied"},
    )
    append(
        store,
        EventType.MODEL_STREAM_COMPLETED,
        {
            "provider": "stub",
            "model": "stub-1",
            "input_tokens": 120,
            "output_tokens": 30,
            "stop_reason": "end_turn",
        },
    )
    append(
        store,
        EventType.PROVIDER_ERROR,
        {"provider": "stub", "model": "stub-1", "error": "rate limited"},
    )
    append(store, EventType.ARTIFACT_STORED, {"artifact_id": "art_1", "size": 900})
    append(store, EventType.CONTEXT_COMPACTED, {"reason": "manual", "removed_messages": 4})
    append(store, EventType.CAPABILITY_INJECTED, {"query": "review", "tokens_used": 40})
    append(
        store,
        EventType.SNAPSHOT_CREATED,
        {"snapshot_id": "snap_1", "state_hash": "abc", "last_seq": 5},
    )
    append(
        store,
        EventType.MCP_SERVER_CONNECTED,
        {
            "server": "files",
            "transport": "stdio",
            "tool_count": 1,
            "protocol_version": "2024-11-05",
        },
    )
    append(
        store,
        EventType.MCP_TOOLS_REGISTERED,
        {
            "server": "files",
            "tools": ["mcp_files_read_note"],
            "rejected": [{"tool": "write_note", "reason": "scope_not_granted"}],
            "granted_scopes": ["fs:read"],
        },
    )
    append(
        store,
        EventType.SUBAGENT_TASK_STARTED,
        {"task_id": "task_1", "child_session_id": "ses_child", "objective": "summarize"},
    )
    append(
        store,
        EventType.SUBAGENT_TASK_FINISHED,
        {
            "task_id": "task_1",
            "child_session_id": "ses_child",
            "outcome": "timeout",
            "error_code": "subagent_deadline_exceeded",
            "tool_calls": 2,
            "total_tokens": 400,
        },
    )
    return store.read_events(DEMO_SESSION_ID)


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #


def test_metrics_count_persisted_events_not_intentions(store: EventStore) -> None:
    events = rich_log(store)

    metrics = build_metrics(events)

    assert metrics.session_id == DEMO_SESSION_ID
    assert metrics.events == len(events)
    assert metrics.last_seq == events[-1].seq
    assert metrics.model_requests == 1
    assert metrics.input_tokens == 120
    assert metrics.output_tokens == 30
    assert metrics.total_tokens == 150
    assert metrics.provider_errors == 1
    assert metrics.tool_calls == 1
    assert metrics.tool_failures == 1
    assert metrics.approvals_requested == 1
    assert metrics.approvals_denied == 1
    assert metrics.artifacts == 1
    assert metrics.compactions == 1
    assert metrics.capability_injections == 1


def test_metrics_count_the_m8_surfaces(store: EventStore) -> None:
    metrics = build_metrics(rich_log(store))

    assert metrics.mcp_servers_connected == 1
    assert metrics.mcp_tools_registered == 1
    assert metrics.subagent_tasks == 1
    assert metrics.subagent_failures == 1


def test_a_completed_subagent_task_is_not_a_failure(store: EventStore) -> None:
    seed_session(store)
    append(
        store,
        EventType.SUBAGENT_TASK_STARTED,
        {"task_id": "t", "child_session_id": "ses_child"},
    )
    append(
        store,
        EventType.SUBAGENT_TASK_FINISHED,
        {"task_id": "t", "child_session_id": "ses_child", "outcome": "completed"},
    )

    metrics = build_metrics(store.read_events(DEMO_SESSION_ID))

    assert metrics.subagent_tasks == 1
    assert metrics.subagent_failures == 0


def test_total_tokens_is_exported_not_only_computed(store: EventStore) -> None:
    """A consumer reading the file must not have to re-add the two halves."""

    payload = build_metrics(rich_log(store)).as_json()

    assert payload["total_tokens"] == payload["input_tokens"] + payload["output_tokens"]


def test_metrics_of_an_empty_log_are_zeros_not_an_error() -> None:
    metrics = build_metrics([], session_id="ses_empty")

    assert metrics.session_id == "ses_empty"
    assert metrics.events == 0
    assert metrics.duration_ms == 0


# --------------------------------------------------------------------------- #
# audit coverage
# --------------------------------------------------------------------------- #


def test_the_audit_answers_every_question_the_plan_names(store: EventStore) -> None:
    """model, skill, tool, approval, truncation, compaction, recovery, mcp, subagent."""

    audit = build_audit(rich_log(store))

    counts = audit.counts()
    assert set(counts) == set(AUDIT_CATEGORIES)
    assert all(count > 0 for count in counts.values())


def test_the_audit_names_the_subjects_behind_each_category(store: EventStore) -> None:
    audit = build_audit(rich_log(store))

    summary = audit.summary()
    assert summary["models"] == ["stub/stub-1"]
    assert "write_file" in summary["tools"]
    assert summary["mcp_servers"] == ["files"]
    assert summary["subagent_tasks"] == ["task_1"]


def test_a_denied_approval_is_answerable_from_the_audit_alone(store: EventStore) -> None:
    audit = build_audit(rich_log(store))

    approvals = audit.of_category("approval")

    assert [record.outcome for record in approvals] == ["requested", "denied"]


# --------------------------------------------------------------------------- #
# replay report
# --------------------------------------------------------------------------- #


def test_a_finished_session_replays_clean(store: EventStore) -> None:
    seed_session(store)
    events = store.read_events(DEMO_SESSION_ID)

    report = build_replay_report(events)

    assert report.clean
    assert report.problems == ()
    assert report.replayed_events == 7
    assert report.state_hash == store.load_state(DEMO_SESSION_ID).state_hash()


def test_a_matching_expected_hash_is_deterministic(store: EventStore) -> None:
    seed_session(store)
    events = store.read_events(DEMO_SESSION_ID)
    recorded = store.load_state(DEMO_SESSION_ID).state_hash()

    report = build_replay_report(events, expected_state_hash=recorded)

    assert report.deterministic
    assert report.clean


def test_a_mismatched_expected_hash_is_a_problem_not_an_exception(store: EventStore) -> None:
    """A release check needs the verdict in a file, not a traceback."""

    seed_session(store)
    events = store.read_events(DEMO_SESSION_ID)

    report = build_replay_report(events, expected_state_hash="deadbeef")

    assert report.deterministic is False
    assert report.clean is False
    assert any("deadbeef" in problem for problem in report.problems)


def test_an_unfinished_operation_is_not_clean(store: EventStore) -> None:
    append(store, EventType.SESSION_CREATED, {"title": "x", "workspace_root": "/tmp/ws"})
    append(store, EventType.OPERATION_STARTED, {"name": "agent_run"})
    events = store.read_events(DEMO_SESSION_ID)

    report = build_replay_report(events)

    assert report.clean is False
    assert len(report.unfinished_operation_ids) == 1
    assert any("is still started" in problem for problem in report.problems)


def test_a_dispatched_subagent_task_with_no_outcome_is_not_clean(store: EventStore) -> None:
    """A child that vanished leaves the parent claiming it is still running."""

    seed_session(store)
    append(
        store,
        EventType.SUBAGENT_TASK_STARTED,
        {"task_id": "task_lost", "child_session_id": "ses_child"},
    )

    report = build_replay_report(store.read_events(DEMO_SESSION_ID))

    assert report.open_subagent_task_ids == ("task_lost",)
    assert report.clean is False
    assert any("task_lost" in problem for problem in report.problems)


def test_a_hole_in_the_middle_of_the_log_is_reported(store: EventStore) -> None:
    events = [event for event in rich_log(store) if event.seq != 4]

    report = build_replay_report(events, session_id=DEMO_SESSION_ID)

    assert report.gaps == (4,)
    assert report.clean is False
    assert any("missing seq" in problem for problem in report.problems)


def test_truncation_at_the_tail_is_not_a_gap(store: EventStore) -> None:
    """A log cut short by a crash is recovery's business, not a corruption claim."""

    events = rich_log(store)

    assert _sequence_gaps(events[:-3]) == ()
    assert _sequence_gaps([]) == ()


def test_an_empty_log_replays_to_an_empty_state() -> None:
    report = build_replay_report([], session_id="ses_empty")

    assert report.replayed_events == 0
    assert report.session_id == "ses_empty"
    assert report.clean


def test_the_report_renders_one_line_per_problem(store: EventStore) -> None:
    seed_session(store)
    append(
        store,
        EventType.SUBAGENT_TASK_STARTED,
        {"task_id": "task_lost", "child_session_id": "ses_child"},
    )

    lines = build_replay_report(store.read_events(DEMO_SESSION_ID)).render()

    assert lines[0] == f"session: {DEMO_SESSION_ID}"
    assert "verdict: problems" in lines
    assert len(lines) == 6


# --------------------------------------------------------------------------- #
# the bundle and its four files
# --------------------------------------------------------------------------- #


def test_the_bundle_produces_exactly_the_four_files_the_plan_names(store: EventStore) -> None:
    bundle = build_bundle(rich_log(store))

    assert tuple(bundle.files()) == EXPORT_FILENAMES


def test_the_jsonl_files_are_one_parseable_object_per_event(store: EventStore) -> None:
    events = rich_log(store)
    files = build_bundle(events).files()

    trace_rows = [json.loads(line) for line in files[TRACE_FILENAME].splitlines()]
    audit_rows = [json.loads(line) for line in files[AUDIT_FILENAME].splitlines()]

    assert len(trace_rows) == len(events)
    assert trace_rows[0]["seq"] == events[0].seq
    assert {row["category"] for row in audit_rows} <= set(AUDIT_CATEGORIES)


def test_the_json_files_parse_and_carry_their_derived_verdicts(store: EventStore) -> None:
    files = build_bundle(rich_log(store)).files()

    metrics = json.loads(files[METRICS_FILENAME])
    replay_report = json.loads(files[REPLAY_REPORT_FILENAME])

    assert metrics["total_tokens"] == 150
    assert replay_report["clean"] is True
    assert replay_report["state_hash"] == store.load_state(DEMO_SESSION_ID).state_hash()


def test_the_bundle_is_a_pure_function_of_the_log(store: EventStore) -> None:
    """Two builds from the same events must be byte-identical, or the file is not evidence."""

    events = rich_log(store)

    first = build_bundle(events).files()
    second = build_bundle([Event.model_validate(event.to_json_dict()) for event in events]).files()

    assert first == second


def test_write_lands_all_four_files_on_disk(store: EventStore, tmp_path: Path) -> None:
    target = tmp_path / "export" / DEMO_SESSION_ID

    written = build_bundle(rich_log(store)).write(target)

    assert sorted(path.name for path in written.values()) == sorted(EXPORT_FILENAMES)
    assert all(path.read_text(encoding="utf-8") for path in written.values())


def test_the_summary_carries_all_four_sections(store: EventStore) -> None:
    summary = build_bundle(rich_log(store)).summary()

    assert set(summary) == {"session_id", "trace", "audit", "metrics", "replay"}
    assert summary["trace"]["events"] == summary["metrics"]["events"]


def test_an_export_of_an_untouched_session_is_still_answerable() -> None:
    bundle = build_bundle([], session_id="ses_empty")

    files = bundle.files()

    assert files[TRACE_FILENAME] == ""
    assert files[AUDIT_FILENAME] == ""
    assert json.loads(files[METRICS_FILENAME])["events"] == 0
