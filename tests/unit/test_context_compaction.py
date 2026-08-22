"""Structured compaction: what the summary keeps, and what it must never lose.

The plan's completion condition for M5 is that a long task survives a compaction
with its objective, blockers, next actions and evidence intact. These tests pin
each of those, plus the invariant underneath them: compaction replaces the
prompt, never the record. Every assertion that a message was dropped from the
transcript is paired with one that the event is still in the log.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from atlas_harness.context.compaction import (
    MAX_ITEM_CHARS,
    MAX_SUMMARY_ITEMS,
    REASON_MANUAL,
    REASON_OVERFLOW,
    REASON_THRESHOLD,
    CompactionSummary,
    Compactor,
    compaction_reason_for,
    summary_from_event,
)
from atlas_harness.context.tokens import ContextBudget, ContextPressure
from atlas_harness.events import EventStore, EventType
from atlas_harness.kernel.errors import EventValidationError
from atlas_harness.model.protocol import ModelMessage, ModelToolCall, Role

SESSION_ID = "ses_compact"
OPERATION_ID = "op_compact"

StoreFactory = Callable[..., EventStore]


@pytest.fixture
def compactor(store: EventStore) -> Compactor:
    store.append_new(
        EventType.SESSION_CREATED,
        session_id=SESSION_ID,
        payload={"title": "long task", "workspace_root": "/tmp/ws"},
    )
    store.append_new(
        EventType.OPERATION_STARTED,
        session_id=SESSION_ID,
        operation_id=OPERATION_ID,
        payload={"name": "agent_run"},
    )
    return Compactor(store, budget=ContextBudget(limit_tokens=1_000))


def tool_result(
    store: EventStore,
    *,
    call_id: str,
    tool_name: str = "read_file",
    success: bool = True,
    output: object = None,
    error: str | None = None,
    details: dict[str, object] | None = None,
) -> None:
    store.append_new(
        EventType.TOOL_STARTED,
        session_id=SESSION_ID,
        operation_id=OPERATION_ID,
        payload={"tool_name": tool_name, "call_id": call_id, "risk": "read", "idempotent": True},
    )
    payload: dict[str, object] = {
        "tool_name": tool_name,
        "call_id": call_id,
        "success": success,
        "output": output,
        "error": error,
    }
    if details is not None:
        payload["details"] = details
    store.append_new(
        EventType.TOOL_RESULT,
        session_id=SESSION_ID,
        operation_id=OPERATION_ID,
        payload=payload,
    )


def transcript(turns: int) -> list[ModelMessage]:
    messages: list[ModelMessage] = [ModelMessage.system("you are a tool-using agent")]
    for index in range(turns):
        messages.append(ModelMessage.user(f"user turn {index}"))
        messages.append(ModelMessage.assistant(f"assistant turn {index}"))
    return messages


# --------------------------------------------------------------------------- #
# the summary object
# --------------------------------------------------------------------------- #


def test_every_field_the_plan_requires_is_present_even_when_empty() -> None:
    """A consumer must never have to tell "nothing found" from "key dropped"."""

    payload = CompactionSummary().model_dump(mode="json")

    assert set(payload) == {
        "current_objective",
        "task_progress",
        "blockers",
        "next_actions",
        "decisions",
        "tool_lessons",
        "failed_paths",
        "evidence_refs",
        "open_questions",
    }


def test_an_empty_summary_knows_it_is_empty() -> None:
    assert CompactionSummary().is_empty is True
    assert CompactionSummary(blockers=["stuck"]).is_empty is False


def test_the_rendered_text_omits_empty_sections() -> None:
    text = CompactionSummary(current_objective="ship M5", blockers=["tests fail"]).as_text()

    assert "Current objective: ship M5" in text
    assert "Blockers:\n  - tests fail" in text
    assert "Evidence" not in text


def test_the_summary_survives_a_round_trip_through_an_event_payload() -> None:
    """The event carries the summary, so a replay can rebuild it."""

    original = CompactionSummary(
        current_objective="ship M5",
        blockers=["tests fail"],
        evidence_refs=["src/a.py"],
    )

    restored = summary_from_event(
        {**original.model_dump(mode="json"), "reason": "threshold", "used_tokens": 10}
    )

    assert restored == original


# --------------------------------------------------------------------------- #
# folding the log into a summary
# --------------------------------------------------------------------------- #


def test_the_objective_comes_from_the_operation(compactor: Compactor) -> None:
    summary = compactor.summarize(SESSION_ID, operation_id=OPERATION_ID)

    assert summary.current_objective == f"agent_run ({OPERATION_ID})"


def test_an_explicit_objective_wins(compactor: Compactor) -> None:
    summary = compactor.summarize(SESSION_ID, operation_id=OPERATION_ID, objective="ship M5")

    assert summary.current_objective == "ship M5"


def test_a_successful_tool_call_becomes_progress(compactor: Compactor) -> None:
    tool_result(compactor.store, call_id="c1", output={"path": "src/a.py"})

    summary = compactor.summarize(SESSION_ID, operation_id=OPERATION_ID)

    assert "read_file succeeded" in summary.task_progress
    assert "src/a.py" in summary.evidence_refs


def test_a_failed_tool_call_becomes_a_lesson_not_progress(compactor: Compactor) -> None:
    """The model's next move depends on knowing what did not work."""

    tool_result(
        compactor.store,
        call_id="c1",
        success=False,
        error="file not found",
        details={"path": "missing.py"},
    )

    summary = compactor.summarize(SESSION_ID, operation_id=OPERATION_ID)

    assert summary.task_progress == []
    assert "read_file failed: file not found" in summary.tool_lessons
    assert "missing.py" in summary.failed_paths


def test_search_matches_contribute_their_paths_as_evidence(compactor: Compactor) -> None:
    tool_result(
        compactor.store,
        call_id="c1",
        tool_name="search",
        output={"matches": [{"path": "a.py"}, {"path": "b.py"}]},
    )

    summary = compactor.summarize(SESSION_ID, operation_id=OPERATION_ID)

    assert "a.py" in summary.evidence_refs
    assert "b.py" in summary.evidence_refs


def test_a_provider_error_becomes_a_blocker(compactor: Compactor) -> None:
    compactor.store.append_new(
        EventType.PROVIDER_ERROR,
        session_id=SESSION_ID,
        operation_id=OPERATION_ID,
        payload={"error": "rate limited", "error_code": "provider_rate_limited"},
    )

    summary = compactor.summarize(SESSION_ID, operation_id=OPERATION_ID)

    assert any("rate limited" in blocker for blocker in summary.blockers)


def test_a_suspension_becomes_a_blocker_and_an_open_question(compactor: Compactor) -> None:
    """M4's suspended state has to survive into M5's summary.

    A compaction that dropped the pending confirmation would leave the model with
    no idea why the run stopped.
    """

    compactor.store.append_new(
        EventType.OPERATION_SUSPENDED,
        session_id=SESSION_ID,
        operation_id=OPERATION_ID,
        payload={"reason": "unconfirmed side effect", "pending_tool_call_ids": ["c9"]},
    )

    summary = compactor.summarize(SESSION_ID, operation_id=OPERATION_ID)

    assert any("unconfirmed side effect" in blocker for blocker in summary.blockers)
    assert "confirm tool call c9?" in summary.open_questions


def test_a_pending_approval_becomes_an_open_question(compactor: Compactor) -> None:
    compactor.store.append_new(
        EventType.APPROVAL_REQUESTED,
        session_id=SESSION_ID,
        operation_id=OPERATION_ID,
        payload={"approval_id": "ap_1", "reason": "write to config.yml"},
    )

    summary = compactor.summarize(SESSION_ID, operation_id=OPERATION_ID)

    assert "approval pending: write to config.yml" in summary.open_questions


def test_an_unfinished_tool_call_becomes_a_next_action(compactor: Compactor) -> None:
    compactor.store.append_new(
        EventType.TOOL_STARTED,
        session_id=SESSION_ID,
        operation_id=OPERATION_ID,
        payload={"tool_name": "write_file", "call_id": "c_open", "risk": "write"},
    )

    summary = compactor.summarize(SESSION_ID, operation_id=OPERATION_ID)

    assert "finish write_file (c_open)" in summary.next_actions


def test_an_unconsumed_queue_message_becomes_a_next_action(compactor: Compactor) -> None:
    """A steer message the loop never drained is still owed."""

    compactor.store.append_new(
        EventType.QUEUE_MESSAGE_ENQUEUED,
        session_id=SESSION_ID,
        operation_id=OPERATION_ID,
        payload={"queue": "steer", "message_id": "m1", "content": "stop editing config.yml"},
    )

    summary = compactor.summarize(SESSION_ID, operation_id=OPERATION_ID)

    assert "[steer] stop editing config.yml" in summary.next_actions


def test_a_stored_artifact_is_referenced_not_inlined(compactor: Compactor) -> None:
    compactor.store.append_new(
        EventType.ARTIFACT_STORED,
        session_id=SESSION_ID,
        operation_id=OPERATION_ID,
        payload={"artifact_id": "art_1", "kind": "tool_output", "size": 2_000_000},
    )

    summary = compactor.summarize(SESSION_ID, operation_id=OPERATION_ID)

    assert any("art_1" in reference for reference in summary.evidence_refs)


def test_a_secret_in_a_queue_message_is_redacted(compactor: Compactor) -> None:
    """The summary re-enters the prompt, so it is redacted like any other content."""

    compactor.store.append_new(
        EventType.QUEUE_MESSAGE_ENQUEUED,
        session_id=SESSION_ID,
        operation_id=OPERATION_ID,
        payload={
            "queue": "steer",
            "message_id": "m1",
            "content": "use api_key=sk-abcdefghij0123456789",
        },
    )

    summary = compactor.summarize(SESSION_ID, operation_id=OPERATION_ID)

    rendered = summary.as_text()
    assert "sk-abcdefghij0123456789" not in rendered
    assert "[redacted]" in rendered


def test_summaries_are_scoped_to_one_operation(compactor: Compactor) -> None:
    """A compaction in one lane must not pull in another operation's blockers."""

    compactor.store.append_new(
        EventType.OPERATION_STARTED,
        session_id=SESSION_ID,
        operation_id="op_other",
        payload={"name": "other"},
    )
    compactor.store.append_new(
        EventType.PROVIDER_ERROR,
        session_id=SESSION_ID,
        operation_id="op_other",
        payload={"error": "someone else's problem", "error_code": "provider_error"},
    )

    summary = compactor.summarize(SESSION_ID, operation_id=OPERATION_ID)

    assert summary.blockers == []


def test_each_list_is_capped(compactor: Compactor) -> None:
    """A summary that grows without bound defeats its own purpose."""

    for index in range(MAX_SUMMARY_ITEMS + 8):
        tool_result(
            compactor.store,
            call_id=f"c{index}",
            tool_name=f"tool_{index}",
            success=False,
            error=f"error {index}",
        )

    summary = compactor.summarize(SESSION_ID, operation_id=OPERATION_ID)

    assert len(summary.tool_lessons) == MAX_SUMMARY_ITEMS
    # The most recent survive: an old failure matters less than the last one.
    assert any(f"error {MAX_SUMMARY_ITEMS + 7}" in lesson for lesson in summary.tool_lessons)


def test_a_long_entry_is_clipped(compactor: Compactor) -> None:
    tool_result(
        compactor.store,
        call_id="c1",
        success=False,
        error="x" * (MAX_ITEM_CHARS * 2),
    )

    summary = compactor.summarize(SESSION_ID, operation_id=OPERATION_ID)

    assert len(summary.tool_lessons[0]) <= MAX_ITEM_CHARS


def test_duplicate_entries_are_collapsed(compactor: Compactor) -> None:
    for index in range(4):
        tool_result(compactor.store, call_id=f"c{index}", output={"path": "same.py"})

    summary = compactor.summarize(SESSION_ID, operation_id=OPERATION_ID)

    assert summary.evidence_refs.count("same.py") == 1


# --------------------------------------------------------------------------- #
# compacting: the prompt changes, the record does not
# --------------------------------------------------------------------------- #


def test_compaction_replaces_the_middle_and_keeps_the_tail(compactor: Compactor) -> None:
    messages = transcript(6)

    result = compactor.compact(
        SESSION_ID,
        operation_id=OPERATION_ID,
        messages=messages,
        used_tokens=900,
        keep_recent=4,
    )

    assert result.replaced_messages > 0
    assert len(result.messages) < len(messages)
    # System messages survive: the fixed slot is never displaced.
    assert result.messages[0].role is Role.SYSTEM
    # The summary stands in for what was dropped.
    assert "[compacted context]" in result.messages[1].content
    # The most recent turn is still verbatim.
    assert result.messages[-1].content == messages[-1].content


def test_the_original_events_are_untouched_by_a_compaction(compactor: Compactor) -> None:
    """The core invariant: compaction is a prompt rewrite, not a deletion."""

    tool_result(compactor.store, call_id="c1", output={"path": "src/a.py"})
    before = compactor.store.read_events(SESSION_ID)

    compactor.compact(
        SESSION_ID,
        operation_id=OPERATION_ID,
        messages=transcript(6),
        used_tokens=900,
    )

    after = compactor.store.read_events(SESSION_ID)
    assert [event.event_id for event in before] == [
        event.event_id for event in after[: len(before)]
    ]
    # Exactly one event was added, and it is the compaction itself.
    assert len(after) == len(before) + 1
    assert after[-1].event_type is EventType.CONTEXT_COMPACTED


def test_the_compaction_event_carries_the_whole_summary(compactor: Compactor) -> None:
    tool_result(
        compactor.store,
        call_id="c1",
        success=False,
        error="boom",
        details={"path": "bad.py"},
    )

    compactor.compact(
        SESSION_ID,
        operation_id=OPERATION_ID,
        messages=transcript(6),
        used_tokens=900,
        reason=REASON_OVERFLOW,
    )

    event = compactor.store.read_events(SESSION_ID)[-1]
    payload = event.payload.model_dump(mode="json")
    assert payload["reason"] == REASON_OVERFLOW
    assert payload["used_tokens"] == 900
    assert "read_file failed: boom" in payload["tool_lessons"]
    assert "bad.py" in payload["failed_paths"]
    assert payload["replaced_messages"] > 0


def test_a_tool_message_is_never_orphaned_from_its_call(compactor: Compactor) -> None:
    """A tool result without its request is invalid for several providers.

    The split point moves back rather than producing a transcript that cannot be
    sent.
    """

    messages = [
        ModelMessage.system("system"),
        ModelMessage.user("do it"),
        ModelMessage.assistant(
            tool_calls=(ModelToolCall(call_id="c1", name="read_file", arguments={}),)
        ),
        ModelMessage.tool(tool_call_id="c1", content="{}", name="read_file"),
    ]

    result = compactor.compact(
        SESSION_ID,
        operation_id=OPERATION_ID,
        messages=messages,
        used_tokens=900,
        keep_recent=1,
    )

    # The tail must not begin with a tool message.
    tail = [m for m in result.messages if m.role is not Role.SYSTEM][1:]
    assert not tail or tail[0].role is not Role.TOOL


def test_a_transcript_at_its_floor_records_the_attempt_without_replacing(
    compactor: Compactor,
) -> None:
    """Nothing to compact is not an error, but it is still part of the history."""

    messages = [ModelMessage.system("system"), ModelMessage.user("only turn")]

    result = compactor.compact(
        SESSION_ID,
        operation_id=OPERATION_ID,
        messages=messages,
        used_tokens=900,
        keep_recent=4,
    )

    assert result.replaced_messages == 0
    assert list(result.messages) == messages
    assert compactor.store.read_events(SESSION_ID)[-1].event_type is EventType.CONTEXT_COMPACTED


def test_an_unknown_reason_is_refused(compactor: Compactor) -> None:
    """The three reasons are a closed set, so an audit can group by them."""

    with pytest.raises(EventValidationError) as excinfo:
        compactor.compact(
            SESSION_ID,
            operation_id=OPERATION_ID,
            messages=transcript(4),
            used_tokens=900,
            reason="because",
        )

    assert excinfo.value.details["supported"] == ["manual", "overflow", "threshold"]


def test_a_second_compaction_carries_the_first_one_forward(compactor: Compactor) -> None:
    """Otherwise a long task loses what the first compaction established."""

    compactor.compact(
        SESSION_ID,
        operation_id=OPERATION_ID,
        messages=transcript(6),
        used_tokens=900,
        reason=REASON_MANUAL,
    )

    summary = compactor.summarize(SESSION_ID, operation_id=OPERATION_ID)

    assert any("compacted earlier" in decision for decision in summary.decisions)


# --------------------------------------------------------------------------- #
# the pending mark
# --------------------------------------------------------------------------- #


def test_marking_pending_writes_an_event_without_compacting(compactor: Compactor) -> None:
    """Crossing 70% is information, not yet a reason to discard anything."""

    before = len(compactor.store.read_events(SESSION_ID))

    event = compactor.mark_pending(
        SESSION_ID, operation_id=OPERATION_ID, used_tokens=700, iteration=3
    )

    assert event.event_type is EventType.CONTEXT_COMPACT_PENDING
    payload = event.payload.model_dump(mode="json")
    assert payload["used_tokens"] == 700
    assert payload["ratio"] == 0.7
    events = compactor.store.read_events(SESSION_ID)
    assert len(events) == before + 1
    assert not any(e.event_type is EventType.CONTEXT_COMPACTED for e in events)


def test_the_projection_tracks_the_pending_flag(compactor: Compactor) -> None:
    compactor.mark_pending(SESSION_ID, operation_id=OPERATION_ID, used_tokens=700)

    state = compactor.store.load_state(SESSION_ID)
    assert state.operations[OPERATION_ID].compact_pending is True

    compactor.compact(
        SESSION_ID,
        operation_id=OPERATION_ID,
        messages=transcript(6),
        used_tokens=900,
    )

    state = compactor.store.load_state(SESSION_ID)
    operation = state.operations[OPERATION_ID]
    assert operation.compact_pending is False
    assert operation.compaction_count == 1
    assert operation.last_compaction_reason == REASON_THRESHOLD
    assert state.compactions == 1


# --------------------------------------------------------------------------- #
# reason mapping
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("pressure", "expected"),
    [
        (ContextPressure.COMPACT, REASON_THRESHOLD),
        (ContextPressure.FORCE, REASON_OVERFLOW),
    ],
)
def test_pressure_maps_to_the_recorded_reason(pressure: ContextPressure, expected: str) -> None:
    assert compaction_reason_for(pressure) == expected
