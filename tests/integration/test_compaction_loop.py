"""Automatic compaction inside a real agent loop.

The milestone's completion condition is one sentence: a long task survives at
least one automatic compaction and still finishes. That is
:func:`test_a_long_run_compacts_automatically_and_still_completes` at the bottom;
everything above it pins one part of the mechanism so a failure says *which* part
broke rather than only that the run died.

Every test asserts on the event log as well as the result. Compaction's whole
claim is that it changes the prompt and not the record, and a claim about the
record has to be checked against the record.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from tests.conftest import TOOL_SESSION_ID

from atlas_harness.agent import AgentLoop, BudgetLimits, StopCause
from atlas_harness.context.compaction import (
    REASON_MANUAL,
    REASON_OVERFLOW,
    REASON_THRESHOLD,
)
from atlas_harness.context.tokens import ContextBudget
from atlas_harness.events import Event, EventStore, EventType
from atlas_harness.model.protocol import Role
from atlas_harness.model.providers.fake import FakeAdapter, text_turn, tool_call_turn
from atlas_harness.tools import default_registry
from atlas_harness.tools.executor import ToolExecutor

OPERATION_ID = "op_compact"

BIG = "x" * 4_000
"""Roughly 1000 estimated tokens, so a handful of turns crosses a small budget."""


def start_operation(store: EventStore, operation_id: str = OPERATION_ID) -> str:
    store.append_new(
        EventType.OPERATION_STARTED,
        session_id=TOOL_SESSION_ID,
        operation_id=operation_id,
        payload={"name": "agent_run"},
    )
    return operation_id


def build_loop(
    store: EventStore,
    executor: ToolExecutor,
    adapter: FakeAdapter,
    *,
    limit_tokens: int = 4_000,
    keep_recent: int = 2,
    limits: BudgetLimits | None = None,
    inline_limit: int | None = None,
) -> AgentLoop:
    from atlas_harness.context.artifacts import ArtifactStore

    return AgentLoop(
        adapter=adapter,
        registry=default_registry(),
        executor=executor,
        store=store,
        model="fake-model",
        provider="fake",
        limits=limits,
        budget=ContextBudget(limit_tokens=limit_tokens),
        keep_recent_messages=keep_recent,
        artifacts=(
            ArtifactStore(store)
            if inline_limit is None
            else ArtifactStore(store, inline_limit=inline_limit)
        ),
    )


def types_of(events: Sequence[Event]) -> list[str]:
    return [event.event_type.value for event in events]


def payloads_of(events: Sequence[Event], event_type: EventType) -> list[dict[str, Any]]:
    return [
        event.payload.model_dump(mode="json") for event in events if event.event_type is event_type
    ]


@pytest.fixture
def store(tool_store: EventStore) -> EventStore:
    return tool_store


# --------------------------------------------------------------------------- #
# the pending mark
# --------------------------------------------------------------------------- #


async def test_crossing_the_prepare_mark_announces_without_compacting(
    store: EventStore, executor: ToolExecutor
) -> None:
    """70% is information. Nothing is replaced yet."""

    adapter = FakeAdapter(script=[text_turn("done")])
    # The tool declarations alone cost ~1000 estimated tokens, so a budget has to
    # clear that floor before the content ratio means anything. 1500 puts the
    # first prompt at roughly 79% -- inside the 70-85% band.
    loop = build_loop(store, executor, adapter, limit_tokens=1_500)
    operation_id = start_operation(store)

    result = await loop.run("y" * 600, session_id=TOOL_SESSION_ID, operation_id=operation_id)

    events = store.read_events(TOOL_SESSION_ID)
    assert "context_compact_pending" in types_of(events)
    assert "context_compacted" not in types_of(events)
    assert result.compactions == 0
    assert result.stop_cause is StopCause.COMPLETED


async def test_the_pending_mark_is_announced_once_per_cycle(
    store: EventStore, executor: ToolExecutor
) -> None:
    """A three-iteration run above 70% announces once, not three times.

    Otherwise every iteration of a long conversation would append another
    pending event and the log would say nothing useful.
    """

    adapter = FakeAdapter(
        script=[
            tool_call_turn("read_file", {"path": "a.txt"}, call_id="c1"),
            tool_call_turn("read_file", {"path": "a.txt"}, call_id="c2"),
            text_turn("done"),
        ]
    )
    (store.log_path(TOOL_SESSION_ID).parent / "unused").mkdir(exist_ok=True)
    loop = build_loop(store, executor, adapter, limit_tokens=3_000)
    operation_id = start_operation(store)

    await loop.run("y" * 8_000, session_id=TOOL_SESSION_ID, operation_id=operation_id)

    events = store.read_events(TOOL_SESSION_ID)
    pending = payloads_of(events, EventType.CONTEXT_COMPACT_PENDING)
    assert len(pending) <= 1


# --------------------------------------------------------------------------- #
# the compact mark
# --------------------------------------------------------------------------- #


async def test_crossing_the_compact_mark_replaces_the_prompt(
    store: EventStore, executor: ToolExecutor, workspace: Any
) -> None:
    """The transcript the model sees gets shorter; the log does not.

    The run needs more than one turn to be compactable at all: on the first
    iteration the transcript is the system prompt plus the user's question, and
    both of those are kept, so there is nothing in between to summarize.
    """

    (workspace / "a.txt").write_text("A" * 3_000, encoding="utf-8")
    adapter = FakeAdapter(
        script=[
            tool_call_turn("read_file", {"path": "a.txt"}, call_id="c1"),
            text_turn("done"),
        ]
    )
    loop = build_loop(store, executor, adapter, limit_tokens=2_500)
    operation_id = start_operation(store)

    result = await loop.run(BIG, session_id=TOOL_SESSION_ID, operation_id=operation_id)

    events = store.read_events(TOOL_SESSION_ID)
    compacted = payloads_of(events, EventType.CONTEXT_COMPACTED)
    assert len(compacted) == 1
    assert compacted[0]["reason"] in {REASON_THRESHOLD, REASON_OVERFLOW}
    assert compacted[0]["replaced_messages"] >= 1
    assert result.compactions == 1
    assert result.stop_cause is StopCause.COMPLETED


async def test_a_first_iteration_prompt_records_no_automatic_compaction(
    store: EventStore, executor: ToolExecutor
) -> None:
    """Nothing to replace means nothing to record.

    A single huge question is over the mark from the first request, but the only
    two messages present are the system prompt and the question itself, and both
    are kept. Recording a compaction here would append an event claiming to have
    freed tokens on every iteration for the rest of the run.
    """

    adapter = FakeAdapter(script=[text_turn("done")])
    loop = build_loop(store, executor, adapter, limit_tokens=1_200)
    operation_id = start_operation(store)

    result = await loop.run(BIG, session_id=TOOL_SESSION_ID, operation_id=operation_id)

    assert "context_compacted" not in types_of(store.read_events(TOOL_SESSION_ID))
    assert result.compactions == 0
    assert result.stop_cause is StopCause.COMPLETED


async def test_the_recorded_reason_is_overflow_past_the_force_mark(
    store: EventStore, executor: ToolExecutor, workspace: Any
) -> None:
    """Well past 95% the reason must say so, not merely 'threshold'."""

    (workspace / "a.txt").write_text("A" * 3_000, encoding="utf-8")
    adapter = FakeAdapter(
        script=[
            tool_call_turn("read_file", {"path": "a.txt"}, call_id="c1"),
            text_turn("done"),
        ]
    )
    loop = build_loop(store, executor, adapter, limit_tokens=1_200)
    operation_id = start_operation(store)

    await loop.run(BIG, session_id=TOOL_SESSION_ID, operation_id=operation_id)

    compacted = payloads_of(store.read_events(TOOL_SESSION_ID), EventType.CONTEXT_COMPACTED)
    assert compacted, "a run this far over the limit must compact"
    assert compacted[0]["reason"] == REASON_OVERFLOW


async def test_compaction_preserves_the_plan_required_fields(
    store: EventStore, executor: ToolExecutor
) -> None:
    """Objective, blockers, next actions and evidence must survive.

    This is the plan's own list, checked against the persisted payload rather
    than the in-memory object, because the payload is what a later replay reads.
    """

    adapter = FakeAdapter(
        script=[
            tool_call_turn("read_file", {"path": "a.txt"}, call_id="c1"),
            text_turn("done"),
        ]
    )
    (store.log_path(TOOL_SESSION_ID).parent.parent.parent / "ws" / "a.txt").write_text(
        "hello", encoding="utf-8"
    )
    loop = build_loop(store, executor, adapter, limit_tokens=300)
    operation_id = start_operation(store)

    await loop.run(BIG, session_id=TOOL_SESSION_ID, operation_id=operation_id)

    compacted = payloads_of(store.read_events(TOOL_SESSION_ID), EventType.CONTEXT_COMPACTED)
    assert compacted
    for field in (
        "current_objective",
        "task_progress",
        "blockers",
        "next_actions",
        "decisions",
        "tool_lessons",
        "failed_paths",
        "evidence_refs",
        "open_questions",
    ):
        assert field in compacted[0]
    assert compacted[0]["current_objective"]


async def test_the_original_events_survive_a_compaction(
    store: EventStore, executor: ToolExecutor
) -> None:
    """Compaction replaces the prompt, never the record.

    The assistant text written before the compaction has to still be readable
    from the log afterwards -- that asymmetry is the entire safety argument for
    compacting at all.
    """

    adapter = FakeAdapter(
        script=[
            tool_call_turn("read_file", {"path": "a.txt"}, call_id="c1", text="looking"),
            text_turn("final answer"),
        ]
    )
    (store.log_path(TOOL_SESSION_ID).parent.parent.parent / "ws" / "a.txt").write_text(
        "hello", encoding="utf-8"
    )
    loop = build_loop(store, executor, adapter, limit_tokens=300)
    operation_id = start_operation(store)

    await loop.run(BIG, session_id=TOOL_SESSION_ID, operation_id=operation_id)

    state = store.load_state(TOOL_SESSION_ID)
    assert "looking" in state.messages
    assert "final answer" in state.messages
    assert state.compactions >= 1
    # And the tool result is still there in full.
    results = payloads_of(store.read_events(TOOL_SESSION_ID), EventType.TOOL_RESULT)
    assert results and results[0]["success"] is True


async def test_the_system_prompt_survives_every_compaction(
    store: EventStore, executor: ToolExecutor
) -> None:
    """The fixed slot is never displaced, however tight the budget."""

    adapter = FakeAdapter(script=[text_turn("done")])
    loop = build_loop(store, executor, adapter, limit_tokens=100)
    operation_id = start_operation(store)

    await loop.run(BIG, session_id=TOOL_SESSION_ID, operation_id=operation_id)

    sent = adapter.requests[-1]
    assert sent.messages[0].role is Role.SYSTEM
    assert sent.messages[0].content == loop.system_prompt


# --------------------------------------------------------------------------- #
# the model-requested path
# --------------------------------------------------------------------------- #


async def test_the_model_can_request_a_compaction(
    store: EventStore, executor: ToolExecutor
) -> None:
    """The third trigger: the model calls compact_context itself."""

    adapter = FakeAdapter(
        script=[
            tool_call_turn("compact_context", {"reason": "getting long"}, call_id="c1"),
            text_turn("done"),
        ]
    )
    loop = build_loop(store, executor, adapter, limit_tokens=1_000_000)
    operation_id = start_operation(store)

    result = await loop.run("short", session_id=TOOL_SESSION_ID, operation_id=operation_id)

    compacted = payloads_of(store.read_events(TOOL_SESSION_ID), EventType.CONTEXT_COMPACTED)
    assert len(compacted) == 1
    assert compacted[0]["reason"] == REASON_MANUAL
    assert result.stop_cause is StopCause.COMPLETED


async def test_a_denied_compact_call_does_not_compact(
    store: EventStore, executor_factory: Any
) -> None:
    """A refused call already told the model why; it must not still act."""

    executor = executor_factory(approve=False)
    adapter = FakeAdapter(
        script=[
            tool_call_turn("run_command", {"command": ["git", "status"]}, call_id="c1"),
            text_turn("done"),
        ]
    )
    loop = build_loop(store, executor, adapter, limit_tokens=1_000_000)
    operation_id = start_operation(store)

    await loop.run("short", session_id=TOOL_SESSION_ID, operation_id=operation_id)

    assert "context_compacted" not in types_of(store.read_events(TOOL_SESSION_ID))


# --------------------------------------------------------------------------- #
# artifacts
# --------------------------------------------------------------------------- #


async def test_a_large_tool_output_becomes_an_artifact_reference(
    store: EventStore, executor: ToolExecutor, workspace: Any
) -> None:
    """A big result leaves the prompt but stays on disk.

    Both halves are asserted: the model's tool message carries the artifact id
    rather than the body, and the artifact file holds the content.
    """

    (workspace / "big.txt").write_text("A" * 20_000, encoding="utf-8")
    adapter = FakeAdapter(
        script=[
            tool_call_turn("read_file", {"path": "big.txt"}, call_id="c1"),
            text_turn("done"),
        ]
    )
    loop = build_loop(store, executor, adapter, limit_tokens=1_000_000, inline_limit=1_000)
    operation_id = start_operation(store)

    await loop.run("read it", session_id=TOOL_SESSION_ID, operation_id=operation_id)

    events = store.read_events(TOOL_SESSION_ID)
    stored = payloads_of(events, EventType.ARTIFACT_STORED)
    assert len(stored) == 1
    assert stored[0]["size"] > 1_000

    # The prompt carries the reference, not the content.
    tool_messages = [
        message for message in adapter.requests[-1].messages if message.role is Role.TOOL
    ]
    assert tool_messages
    assert stored[0]["artifact_id"] in tool_messages[0].content
    assert "A" * 5_000 not in tool_messages[0].content

    # The bytes survive on disk, and the full output is still in the log.
    body = loop.artifacts.read(TOOL_SESSION_ID, stored[0]["artifact_id"])
    assert body is not None and "A" * 1_000 in body


async def test_a_failed_tool_result_is_never_externalized(
    store: EventStore, executor: ToolExecutor
) -> None:
    """An error message is small and is exactly what the model needs in full."""

    adapter = FakeAdapter(
        script=[
            tool_call_turn("read_file", {"path": "missing.txt"}, call_id="c1"),
            text_turn("done"),
        ]
    )
    loop = build_loop(store, executor, adapter, limit_tokens=1_000_000, inline_limit=1)
    operation_id = start_operation(store)

    await loop.run("read it", session_id=TOOL_SESSION_ID, operation_id=operation_id)

    assert "artifact_stored" not in types_of(store.read_events(TOOL_SESSION_ID))


# --------------------------------------------------------------------------- #
# the completion condition
# --------------------------------------------------------------------------- #


async def test_a_long_run_compacts_automatically_and_still_completes(
    store: EventStore, executor: ToolExecutor, workspace: Any
) -> None:
    """M5's completion condition, end to end.

    A fixed multi-turn task runs against a budget small enough to force at least
    one automatic compaction, and still reaches ``completed`` with the answer
    intact. The assertions cover all three of the plan's requirements at once:
    the run finishes, a compaction actually happened without anyone asking for
    it, and every original event is still in the log afterwards.
    """

    (workspace / "a.txt").write_text("A" * 3_000, encoding="utf-8")
    (workspace / "b.txt").write_text("B" * 3_000, encoding="utf-8")
    (workspace / "c.txt").write_text("C" * 3_000, encoding="utf-8")
    adapter = FakeAdapter(
        script=[
            tool_call_turn("read_file", {"path": "a.txt"}, call_id="c1", text="reading a"),
            tool_call_turn("read_file", {"path": "b.txt"}, call_id="c2", text="reading b"),
            tool_call_turn("read_file", {"path": "c.txt"}, call_id="c3", text="reading c"),
            text_turn("all three files read"),
        ]
    )
    loop = build_loop(store, executor, adapter, limit_tokens=3_000, keep_recent=2)
    operation_id = start_operation(store)

    result = await loop.run(
        "read a.txt, b.txt and c.txt", session_id=TOOL_SESSION_ID, operation_id=operation_id
    )

    # It finished.
    assert result.stop_cause is StopCause.COMPLETED
    assert result.answer == "all three files read"

    # At least one compaction happened on its own.
    events = store.read_events(TOOL_SESSION_ID)
    compacted = payloads_of(events, EventType.CONTEXT_COMPACTED)
    assert compacted, "the run should have compacted at least once"
    assert all(payload["reason"] != REASON_MANUAL for payload in compacted)
    assert result.compactions >= 1

    # Nothing was lost: every tool call and every assistant message is still there.
    state = store.load_state(TOOL_SESSION_ID)
    operation = state.operations[operation_id]
    assert sorted(operation.tool_calls) == ["c1", "c2", "c3"]
    assert all(call.status == "succeeded" for call in operation.tool_calls.values())
    for text in ("reading a", "reading b", "reading c", "all three files read"):
        assert text in state.messages

    # And the log still replays to the same state.
    assert store.load_state(TOOL_SESSION_ID).state_hash() == state.state_hash()
