"""The model -> tool -> result -> model loop, driven by a scripted provider.

Every test here asserts on the event log as well as on the returned result: the
loop's contract is that the log alone explains what happened, so a green
assertion on ``RunResult`` that leaves no trace behind is not enough.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from tests.conftest import TOOL_SESSION_ID

from atlas_harness.agent import (
    AgentLoop,
    BudgetLimits,
    QueueManager,
    QueueName,
    StopCause,
    tool_declarations,
)
from atlas_harness.events import Event, EventStore, EventType
from atlas_harness.model.protocol import Role, StopReason, TokenUsage
from atlas_harness.model.providers.fake import (
    FakeAdapter,
    error_turn,
    malformed_tool_call_turn,
    text_turn,
    tool_call_turn,
    truncated_turn,
)
from atlas_harness.tools import default_registry
from atlas_harness.tools.executor import ToolExecutor

OPERATION_ID = "op_loop"


def build_loop(
    store: EventStore,
    executor: ToolExecutor,
    adapter: FakeAdapter,
    *,
    limits: BudgetLimits | None = None,
) -> AgentLoop:
    return AgentLoop(
        adapter=adapter,
        registry=default_registry(),
        executor=executor,
        store=store,
        model="fake-model",
        provider="fake",
        limits=limits,
    )


def start_operation(store: EventStore, operation_id: str = OPERATION_ID) -> str:
    store.append_new(
        EventType.OPERATION_STARTED,
        session_id=TOOL_SESSION_ID,
        operation_id=operation_id,
        payload={"name": "agent_run"},
    )
    return operation_id


def types_of(events: list[Event]) -> list[str]:
    return [event.event_type.value for event in events]


def payload_of(events: list[Event], event_type: EventType) -> dict[str, Any]:
    """The first payload of one type, as a plain dict.

    The store parses payloads into typed models on the way out, so tests dump
    them rather than indexing the model directly.
    """

    for event in events:
        if event.event_type is event_type:
            return event.payload.model_dump(mode="json")
    raise AssertionError(f"no {event_type.value} event in {types_of(events)}")


# --------------------------------------------------------------------------- #
# tool declarations
# --------------------------------------------------------------------------- #


def test_declarations_carry_only_what_a_model_needs() -> None:
    declarations = tool_declarations(default_registry())

    assert declarations
    for declaration in declarations:
        assert declaration["type"] == "function"
        assert set(declaration["function"]) == {"name", "description", "parameters"}
        assert declaration["function"]["parameters"]["type"] == "object"


def test_declarations_omit_operator_facing_fields() -> None:
    """Risk, scopes and approval flags are policy concerns, not prompt content.

    The check is structural rather than a substring scan of the rendered JSON,
    because a tool may legitimately take an argument that shares a name with a
    manifest field: ``run_command`` accepts its own ``timeout_ms``.
    """

    operator_only = {"risk", "scopes", "requires_approval", "parallel_safe", "version"}

    for declaration in tool_declarations(default_registry()):
        assert not operator_only & set(declaration)
        assert not operator_only & set(declaration["function"])


def test_declarations_cover_every_registered_tool() -> None:
    registry = default_registry()
    names = {d["function"]["name"] for d in tool_declarations(registry)}

    assert names == {manifest.name for manifest in registry.manifests()}


# --------------------------------------------------------------------------- #
# the closed loop
# --------------------------------------------------------------------------- #


async def test_reads_a_file_then_answers(
    tool_store: EventStore, executor: ToolExecutor, workspace: Path
) -> None:
    """The M3 acceptance path: one tool round trip, then a plain-text answer."""

    (workspace / "notes.txt").write_text("the answer is 42\n", encoding="utf-8")
    adapter = FakeAdapter(
        [
            tool_call_turn("read_file", {"path": "notes.txt"}),
            text_turn("The file says the answer is 42."),
        ]
    )
    operation_id = start_operation(tool_store)

    result = await build_loop(tool_store, executor, adapter).run(
        "What does notes.txt say?",
        session_id=TOOL_SESSION_ID,
        operation_id=operation_id,
    )

    assert result.stop_cause is StopCause.COMPLETED
    assert result.succeeded
    assert result.answer == "The file says the answer is 42."
    assert result.iterations == 2
    assert result.tool_calls == 1


async def test_the_tool_result_reaches_the_second_request(
    tool_store: EventStore, executor: ToolExecutor, workspace: Path
) -> None:
    (workspace / "notes.txt").write_text("the answer is 42\n", encoding="utf-8")
    adapter = FakeAdapter(
        [
            tool_call_turn("read_file", {"path": "notes.txt"}, call_id="call_read"),
            text_turn("42."),
        ]
    )
    operation_id = start_operation(tool_store)

    await build_loop(tool_store, executor, adapter).run(
        "read notes.txt",
        session_id=TOOL_SESSION_ID,
        operation_id=operation_id,
    )

    second = adapter.requests[1]
    roles = [message.role for message in second.messages]
    assert roles == [Role.SYSTEM, Role.USER, Role.ASSISTANT, Role.TOOL]

    reply = second.messages[-1]
    assert reply.tool_call_id == "call_read"
    assert reply.name == "read_file"
    assert "the answer is 42" in reply.content


async def test_every_request_carries_the_tool_declarations(
    tool_store: EventStore, executor: ToolExecutor, workspace: Path
) -> None:
    (workspace / "notes.txt").write_text("hi\n", encoding="utf-8")
    adapter = FakeAdapter([tool_call_turn("read_file", {"path": "notes.txt"}), text_turn("done")])
    operation_id = start_operation(tool_store)

    await build_loop(tool_store, executor, adapter).run(
        "read it", session_id=TOOL_SESSION_ID, operation_id=operation_id
    )

    assert adapter.calls == 2
    for request in adapter.requests:
        assert {d["function"]["name"] for d in request.tools} >= {"read_file"}


async def test_the_whole_round_trip_is_in_the_log(
    tool_store: EventStore, executor: ToolExecutor, workspace: Path
) -> None:
    (workspace / "notes.txt").write_text("the answer is 42\n", encoding="utf-8")
    adapter = FakeAdapter(
        [
            tool_call_turn("read_file", {"path": "notes.txt"}),
            text_turn("The answer is 42."),
        ]
    )
    operation_id = start_operation(tool_store)

    await build_loop(tool_store, executor, adapter).run(
        "read notes.txt", session_id=TOOL_SESSION_ID, operation_id=operation_id
    )
    events = tool_store.read_events(TOOL_SESSION_ID)

    assert types_of(events) == [
        "session_created",
        "operation_started",
        "model_requested",
        "model_stream_completed",
        "tool_started",
        "tool_result",
        "model_requested",
        "model_stream_completed",
        "assistant_message",
        "operation_finished",
    ]


async def test_request_and_response_events_share_a_request_id(
    tool_store: EventStore, executor: ToolExecutor
) -> None:
    """A stream event has to be attributable to the request that produced it."""

    adapter = FakeAdapter([text_turn("hello")])
    operation_id = start_operation(tool_store)

    await build_loop(tool_store, executor, adapter).run(
        "hi", session_id=TOOL_SESSION_ID, operation_id=operation_id
    )
    events = tool_store.read_events(TOOL_SESSION_ID)
    requested = payload_of(events, EventType.MODEL_REQUESTED)
    completed = payload_of(events, EventType.MODEL_STREAM_COMPLETED)

    assert requested["request_id"] == completed["request_id"]
    assert requested["iteration"] == completed["iteration"] == 1


async def test_stream_completion_records_counts_not_text(
    tool_store: EventStore, executor: ToolExecutor
) -> None:
    adapter = FakeAdapter(
        [text_turn("a longer answer", usage=TokenUsage(input_tokens=11, output_tokens=7))]
    )
    operation_id = start_operation(tool_store)

    result = await build_loop(tool_store, executor, adapter).run(
        "hi", session_id=TOOL_SESSION_ID, operation_id=operation_id
    )
    completed = payload_of(
        tool_store.read_events(TOOL_SESSION_ID), EventType.MODEL_STREAM_COMPLETED
    )

    assert completed["text_length"] == len("a longer answer")
    assert completed["tool_call_count"] == 0
    assert completed["stop_reason"] == StopReason.END_TURN.value
    assert completed["input_tokens"] == 11
    assert result.usage.total_tokens == 18


async def test_usage_accumulates_across_iterations(
    tool_store: EventStore, executor: ToolExecutor, workspace: Path
) -> None:
    (workspace / "notes.txt").write_text("hi\n", encoding="utf-8")
    adapter = FakeAdapter(
        [
            tool_call_turn(
                "read_file",
                {"path": "notes.txt"},
                usage=TokenUsage(input_tokens=10, output_tokens=5),
            ),
            text_turn("done", usage=TokenUsage(input_tokens=20, output_tokens=3)),
        ]
    )
    operation_id = start_operation(tool_store)

    result = await build_loop(tool_store, executor, adapter).run(
        "read it", session_id=TOOL_SESSION_ID, operation_id=operation_id
    )

    assert result.usage.input_tokens == 30
    assert result.usage.output_tokens == 8


async def test_assistant_message_is_only_written_when_there_is_text(
    tool_store: EventStore, executor: ToolExecutor, workspace: Path
) -> None:
    """A pure tool-call turn has no prose, so it gets no assistant_message."""

    (workspace / "notes.txt").write_text("hi\n", encoding="utf-8")
    adapter = FakeAdapter([tool_call_turn("read_file", {"path": "notes.txt"}), text_turn("done")])
    operation_id = start_operation(tool_store)

    await build_loop(tool_store, executor, adapter).run(
        "read it", session_id=TOOL_SESSION_ID, operation_id=operation_id
    )
    messages = [
        event
        for event in tool_store.read_events(TOOL_SESSION_ID)
        if event.event_type is EventType.ASSISTANT_MESSAGE
    ]

    assert len(messages) == 1
    assert messages[0].payload.model_dump(mode="json")["content"] == "done"


async def test_two_tools_in_one_turn_are_both_answered(
    tool_store: EventStore, executor: ToolExecutor, workspace: Path
) -> None:
    (workspace / "a.txt").write_text("alpha\n", encoding="utf-8")
    (workspace / "b.txt").write_text("beta\n", encoding="utf-8")
    turn = tool_call_turn("read_file", {"path": "a.txt"}, call_id="call_a", index=0)
    turn = turn[:-1] + tool_call_turn("read_file", {"path": "b.txt"}, call_id="call_b", index=1)
    adapter = FakeAdapter([turn, text_turn("both read")])
    operation_id = start_operation(tool_store)

    result = await build_loop(tool_store, executor, adapter).run(
        "read both", session_id=TOOL_SESSION_ID, operation_id=operation_id
    )

    assert result.tool_calls == 2
    replies = [m for m in adapter.requests[1].messages if m.role is Role.TOOL]
    assert [m.tool_call_id for m in replies] == ["call_a", "call_b"]


# --------------------------------------------------------------------------- #
# failures the model can learn from
# --------------------------------------------------------------------------- #


async def test_a_failing_tool_is_reported_back_to_the_model(
    tool_store: EventStore, executor: ToolExecutor
) -> None:
    """The loop keeps going: a tool error is evidence, not a run failure."""

    adapter = FakeAdapter(
        [
            tool_call_turn("read_file", {"path": "missing.txt"}, call_id="call_missing"),
            text_turn("That file does not exist."),
        ]
    )
    operation_id = start_operation(tool_store)

    result = await build_loop(tool_store, executor, adapter).run(
        "read missing.txt", session_id=TOOL_SESSION_ID, operation_id=operation_id
    )

    assert result.stop_cause is StopCause.COMPLETED
    reply = adapter.requests[1].messages[-1]
    body = json.loads(reply.content)
    assert body["error"]
    assert reply.tool_call_id == "call_missing"


async def test_a_malformed_tool_call_is_answered_not_dropped(
    tool_store: EventStore, executor: ToolExecutor
) -> None:
    adapter = FakeAdapter(
        [
            malformed_tool_call_turn("read_file", "{not json", call_id="call_bad"),
            text_turn("Sorry, retrying properly."),
        ]
    )
    operation_id = start_operation(tool_store)

    result = await build_loop(tool_store, executor, adapter).run(
        "read it", session_id=TOOL_SESSION_ID, operation_id=operation_id
    )

    assert result.stop_cause is StopCause.COMPLETED
    assert result.tool_calls == 0

    reply = adapter.requests[1].messages[-1]
    assert reply.role is Role.TOOL
    assert reply.tool_call_id == "call_bad"
    assert json.loads(reply.content)["error"] == "tool_input_error"


async def test_a_malformed_call_never_reaches_the_executor(
    tool_store: EventStore, executor: ToolExecutor
) -> None:
    adapter = FakeAdapter([malformed_tool_call_turn("read_file", "{}}"), text_turn("ok")])
    operation_id = start_operation(tool_store)

    await build_loop(tool_store, executor, adapter).run(
        "read it", session_id=TOOL_SESSION_ID, operation_id=operation_id
    )
    types = types_of(tool_store.read_events(TOOL_SESSION_ID))

    assert "tool_started" not in types
    invalid = payload_of(tool_store.read_events(TOOL_SESSION_ID), EventType.MODEL_STREAM_COMPLETED)
    assert invalid["invalid_tool_call_count"] == 1


async def test_a_denied_tool_keeps_the_loop_running(
    tool_store: EventStore, executor_factory, workspace: Path
) -> None:
    """A refusal is a tool result: the model is told, and it answers."""

    (workspace / "notes.txt").write_text("hi\n", encoding="utf-8")
    adapter = FakeAdapter(
        [
            tool_call_turn("write_file", {"path": "out.txt", "content": "x"}),
            text_turn("I was not allowed to write that."),
        ]
    )
    operation_id = start_operation(tool_store)

    result = await build_loop(tool_store, executor_factory(approve=False), adapter).run(
        "write out.txt", session_id=TOOL_SESSION_ID, operation_id=operation_id
    )

    assert result.stop_cause is StopCause.COMPLETED
    assert not (workspace / "out.txt").exists()
    assert json.loads(adapter.requests[1].messages[-1].content)["error"]


# --------------------------------------------------------------------------- #
# provider faults
# --------------------------------------------------------------------------- #


async def test_a_provider_error_fails_the_operation(
    tool_store: EventStore, executor: ToolExecutor
) -> None:
    adapter = FakeAdapter([error_turn("upstream is down", status_code=503)])
    operation_id = start_operation(tool_store)

    result = await build_loop(tool_store, executor, adapter).run(
        "hi", session_id=TOOL_SESSION_ID, operation_id=operation_id
    )
    events = tool_store.read_events(TOOL_SESSION_ID)

    assert result.stop_cause is StopCause.PROVIDER_ERROR
    assert not result.succeeded
    assert result.error == "upstream is down"
    assert types_of(events)[-2:] == ["provider_error", "operation_failed"]
    assert payload_of(events, EventType.PROVIDER_ERROR)["status_code"] == 503


async def test_a_truncated_stream_is_a_provider_error(
    tool_store: EventStore, executor: ToolExecutor
) -> None:
    """No ``message_completed`` means the turn is not usable, even with text."""

    adapter = FakeAdapter([truncated_turn("half an ans")])
    operation_id = start_operation(tool_store)

    result = await build_loop(tool_store, executor, adapter).run(
        "hi", session_id=TOOL_SESSION_ID, operation_id=operation_id
    )
    error = payload_of(tool_store.read_events(TOOL_SESSION_ID), EventType.PROVIDER_ERROR)

    assert result.stop_cause is StopCause.PROVIDER_ERROR
    assert result.error_code == "provider_incomplete_stream"
    assert error["retryable"] is False


async def test_a_provider_error_never_writes_the_partial_text(
    tool_store: EventStore, executor: ToolExecutor
) -> None:
    adapter = FakeAdapter([truncated_turn("secretish partial text")])
    operation_id = start_operation(tool_store)

    await build_loop(tool_store, executor, adapter).run(
        "hi", session_id=TOOL_SESSION_ID, operation_id=operation_id
    )
    types = types_of(tool_store.read_events(TOOL_SESSION_ID))

    assert "assistant_message" not in types
    assert "model_stream_completed" not in types


# --------------------------------------------------------------------------- #
# budgets
# --------------------------------------------------------------------------- #


async def test_the_iteration_ceiling_stops_the_run(
    tool_store: EventStore, executor: ToolExecutor, workspace: Path
) -> None:
    (workspace / "notes.txt").write_text("hi\n", encoding="utf-8")
    adapter = FakeAdapter(
        [tool_call_turn("read_file", {"path": "notes.txt"}, call_id=f"c{i}") for i in range(3)]
    )
    operation_id = start_operation(tool_store)

    result = await build_loop(
        tool_store, executor, adapter, limits=BudgetLimits(max_iterations=2)
    ).run("read it", session_id=TOOL_SESSION_ID, operation_id=operation_id)

    assert result.stop_cause is StopCause.MAX_ITERATIONS
    assert result.iterations == 2
    assert result.error_code == "budget_exceeded"
    assert adapter.calls == 2


async def test_the_tool_ceiling_refuses_before_executing(
    tool_store: EventStore, executor: ToolExecutor, workspace: Path
) -> None:
    """A turn that overruns the ceiling runs nothing, rather than half of it."""

    (workspace / "a.txt").write_text("alpha\n", encoding="utf-8")
    (workspace / "b.txt").write_text("beta\n", encoding="utf-8")
    turn = tool_call_turn("read_file", {"path": "a.txt"}, call_id="call_a", index=0)
    turn = turn[:-1] + tool_call_turn("read_file", {"path": "b.txt"}, call_id="call_b", index=1)
    adapter = FakeAdapter([turn])
    operation_id = start_operation(tool_store)

    result = await build_loop(
        tool_store, executor, adapter, limits=BudgetLimits(max_tool_calls=1)
    ).run("read both", session_id=TOOL_SESSION_ID, operation_id=operation_id)

    types = types_of(tool_store.read_events(TOOL_SESSION_ID))

    assert result.stop_cause is StopCause.MAX_TOOL_CALLS
    assert result.tool_calls == 0
    assert "tool_started" not in types
    assert types[-1] == "operation_finished"


async def test_an_exhausted_tool_budget_still_answers_every_call(
    tool_store: EventStore, executor: ToolExecutor, workspace: Path
) -> None:
    """A refusal keeps the call/reply pairing valid for strict providers."""

    (workspace / "notes.txt").write_text("hi\n", encoding="utf-8")
    adapter = FakeAdapter(
        [
            tool_call_turn("read_file", {"path": "notes.txt"}, call_id="c1"),
            tool_call_turn("read_file", {"path": "notes.txt"}, call_id="c2"),
        ]
    )
    operation_id = start_operation(tool_store)

    result = await build_loop(
        tool_store, executor, adapter, limits=BudgetLimits(max_tool_calls=1)
    ).run("read it", session_id=TOOL_SESSION_ID, operation_id=operation_id)

    assert result.stop_cause is StopCause.MAX_TOOL_CALLS
    assert result.tool_calls == 1
    assert types_of(tool_store.read_events(TOOL_SESSION_ID))[-1] == "operation_finished"


async def test_the_token_ceiling_stops_before_the_next_request(
    tool_store: EventStore, executor: ToolExecutor, workspace: Path
) -> None:
    (workspace / "notes.txt").write_text("hi\n", encoding="utf-8")
    adapter = FakeAdapter(
        [
            tool_call_turn(
                "read_file",
                {"path": "notes.txt"},
                usage=TokenUsage(input_tokens=40, output_tokens=20),
            ),
            text_turn("never reached"),
        ]
    )
    operation_id = start_operation(tool_store)

    result = await build_loop(
        tool_store, executor, adapter, limits=BudgetLimits(max_total_tokens=50)
    ).run("read it", session_id=TOOL_SESSION_ID, operation_id=operation_id)

    assert result.stop_cause is StopCause.TOKEN_BUDGET
    assert adapter.calls == 1


async def test_a_budget_stop_is_a_finish_not_a_failure(
    tool_store: EventStore, executor: ToolExecutor, workspace: Path
) -> None:
    """Running out of budget is an early stop; the operation still finished."""

    (workspace / "notes.txt").write_text("hi\n", encoding="utf-8")
    adapter = FakeAdapter(
        [tool_call_turn("read_file", {"path": "notes.txt"}, call_id=f"c{i}") for i in range(2)]
    )
    operation_id = start_operation(tool_store)

    await build_loop(tool_store, executor, adapter, limits=BudgetLimits(max_iterations=1)).run(
        "read it", session_id=TOOL_SESSION_ID, operation_id=operation_id
    )
    state = tool_store.load_state(TOOL_SESSION_ID)

    assert state.operations[operation_id].status == "finished"


# --------------------------------------------------------------------------- #
# cancellation
# --------------------------------------------------------------------------- #


async def test_cancelling_before_the_first_request_aborts_the_operation(
    tool_store: EventStore, executor: ToolExecutor
) -> None:
    adapter = FakeAdapter([text_turn("never asked")])
    cancel = asyncio.Event()
    cancel.set()
    operation_id = start_operation(tool_store)

    result = await build_loop(tool_store, executor, adapter).run(
        "hi", session_id=TOOL_SESSION_ID, operation_id=operation_id, cancel=cancel
    )

    assert result.stop_cause is StopCause.CANCELLED
    assert adapter.calls == 0
    assert types_of(tool_store.read_events(TOOL_SESSION_ID))[-1] == "operation_aborted"


async def test_cancelling_mid_stream_skips_the_tool_phase(
    tool_store: EventStore, executor: ToolExecutor, workspace: Path
) -> None:
    """The flag is checked while streaming, so no tool runs after a cancel."""

    (workspace / "notes.txt").write_text("hi\n", encoding="utf-8")
    cancel = asyncio.Event()

    class CancellingAdapter(FakeAdapter):
        def stream(self, request):  # type: ignore[override]
            cancel.set()
            return super().stream(request)

    adapter = CancellingAdapter([tool_call_turn("read_file", {"path": "notes.txt"})])
    operation_id = start_operation(tool_store)

    result = await build_loop(tool_store, executor, adapter).run(
        "read it", session_id=TOOL_SESSION_ID, operation_id=operation_id, cancel=cancel
    )
    types = types_of(tool_store.read_events(TOOL_SESSION_ID))

    assert result.stop_cause is StopCause.CANCELLED
    assert result.tool_calls == 0
    assert "tool_started" not in types
    assert types[-1] == "operation_aborted"


async def test_a_hard_cancellation_still_closes_the_operation(
    tool_store: EventStore, executor: ToolExecutor
) -> None:
    """``CancelledError`` unwinds past ``_finish``, so the loop closes the log itself.

    A caller cancelling is not a provider fault, so the abort is the only event
    written here; what matters is that replay never sees an operation left open.
    """

    class ExplodingAdapter(FakeAdapter):
        async def _stream(self, events):  # type: ignore[override]
            raise asyncio.CancelledError
            yield  # pragma: no cover - makes this an async generator

    adapter = ExplodingAdapter([text_turn("unreachable")])
    operation_id = start_operation(tool_store)
    loop = build_loop(tool_store, executor, adapter)

    with pytest.raises(asyncio.CancelledError):
        await loop.run("hi", session_id=TOOL_SESSION_ID, operation_id=operation_id)
    events = tool_store.read_events(TOOL_SESSION_ID)

    assert types_of(events)[-2:] == ["model_requested", "operation_aborted"]
    assert "cancel" in payload_of(events, EventType.OPERATION_ABORTED)["reason"]
    assert tool_store.load_state(TOOL_SESSION_ID).operations[operation_id].status == "aborted"


# --------------------------------------------------------------------------- #
# queues inside the loop
# --------------------------------------------------------------------------- #


async def test_a_steer_message_lands_in_the_first_request(
    tool_store: EventStore, executor: ToolExecutor
) -> None:
    adapter = FakeAdapter([text_turn("understood")])
    operation_id = start_operation(tool_store)
    queues = QueueManager(tool_store, session_id=TOOL_SESSION_ID, operation_id=operation_id)
    queues.enqueue(QueueName.STEER, "be brief")

    await build_loop(tool_store, executor, adapter).run(
        "hi", session_id=TOOL_SESSION_ID, operation_id=operation_id, queues=queues
    )

    contents = [m.content for m in adapter.requests[0].messages if m.role is Role.USER]
    assert "hi" in contents
    assert any("be brief" in c and c.startswith("[steer]") for c in contents)
    assert queues.snapshot().total == 0


async def test_a_steer_message_queued_mid_run_lands_on_the_next_iteration(
    tool_store: EventStore, executor: ToolExecutor, workspace: Path
) -> None:
    (workspace / "notes.txt").write_text("hi\n", encoding="utf-8")
    operation_id = start_operation(tool_store)
    queues = QueueManager(tool_store, session_id=TOOL_SESSION_ID, operation_id=operation_id)

    class SteeringAdapter(FakeAdapter):
        def stream(self, request):  # type: ignore[override]
            if not self.requests:
                queues.enqueue(QueueName.STEER, "stop after this")
            return super().stream(request)

    adapter = SteeringAdapter(
        [tool_call_turn("read_file", {"path": "notes.txt"}), text_turn("stopping")]
    )

    await build_loop(tool_store, executor, adapter).run(
        "read it", session_id=TOOL_SESSION_ID, operation_id=operation_id, queues=queues
    )

    second = [m.content for m in adapter.requests[1].messages if m.role is Role.USER]
    assert any("stop after this" in c for c in second)


async def test_next_run_messages_are_left_for_the_next_operation(
    tool_store: EventStore, executor: ToolExecutor
) -> None:
    """``next_run`` is deliberately outside the per-iteration drain."""

    adapter = FakeAdapter([text_turn("done")])
    operation_id = start_operation(tool_store)
    queues = QueueManager(tool_store, session_id=TOOL_SESSION_ID, operation_id=operation_id)
    queues.enqueue(QueueName.NEXT_RUN, "and then deploy")

    await build_loop(tool_store, executor, adapter).run(
        "hi", session_id=TOOL_SESSION_ID, operation_id=operation_id, queues=queues
    )

    assert queues.snapshot().next_run == 1
    rendered = json.dumps([m.content for m in adapter.requests[0].messages])
    assert "and then deploy" not in rendered
