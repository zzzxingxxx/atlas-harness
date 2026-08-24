"""Dispatch a sub-agent task and account for every way it can end.

The contract worth pinning is not the happy path. It is that a task which times
out, exhausts its budget or raises still lands exactly one
``subagent_task_finished`` event in the parent, still closes the child's
operation, and still hands back a result the parent can read. A child that
disappears mid-flight would leave ``open_subagent_task_ids`` claiming it is
running forever.

Isolation is asserted structurally: the child's registry is built from the
task's ``allowed_tools`` and nothing else, so a tool the parent holds and the
task did not name is simply absent rather than merely unused.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from tests.conftest import DEMO_SESSION_ID, seed_session

from atlas_harness.config import Settings
from atlas_harness.events import Event, EventStore, EventType
from atlas_harness.model.protocol import ModelEvent, ModelRequest, TokenUsage
from atlas_harness.model.providers.fake import FakeAdapter, text_turn, tool_call_turn
from atlas_harness.subagent.runner import (
    CHILD_OPERATION_NAME,
    CHILD_SYSTEM_PROMPT,
    SubagentRunner,
    child_system_prompt,
    task_summary,
)
from atlas_harness.subagent.task import SubagentResult, SubagentTask, SubagentTaskError
from atlas_harness.tools.registry import ToolRegistry, default_registry


def settings_for(store: EventStore) -> Settings:
    return Settings(data_dir=store.data_dir, workspace_root=store.data_dir / "ws")


def runner_for(
    store: EventStore,
    adapter: FakeAdapter,
    *,
    registry: ToolRegistry | None = None,
) -> SubagentRunner:
    return SubagentRunner(
        settings=settings_for(store),
        store=store,
        adapter=adapter,
        registry=registry if registry is not None else default_registry(),
    )


def types_of(events: list[Event]) -> list[str]:
    return [event.event_type.value for event in events]


def payload_of(events: list[Event], event_type: EventType) -> dict[str, Any]:
    for event in events:
        if event.event_type is event_type:
            return event.payload.model_dump(mode="json")
    raise AssertionError(f"no {event_type.value} event in {types_of(events)}")


def finished_payloads(store: EventStore, session_id: str = DEMO_SESSION_ID) -> list[dict[str, Any]]:
    return [
        event.payload.model_dump(mode="json")
        for event in store.read_events(session_id)
        if event.event_type is EventType.SUBAGENT_TASK_FINISHED
    ]


# --------------------------------------------------------------------------- #
# the task contract
# --------------------------------------------------------------------------- #


def test_a_task_grants_no_tools_by_default() -> None:
    """Inheriting the parent's registry would make delegation an escalation."""

    task = SubagentTask(objective="summarize the log")

    assert task.allowed_tools == ()
    assert task.return_format == "text"


def test_child_ceilings_are_smaller_than_the_parents() -> None:
    task = SubagentTask(objective="x")
    parent = Settings()

    assert task.max_iterations < parent.max_iterations
    assert task.max_tool_calls < parent.max_tool_calls


def test_an_empty_objective_is_refused() -> None:
    with pytest.raises(ValueError, match="objective"):
        SubagentTask(objective="")


def test_an_unknown_return_format_is_refused() -> None:
    with pytest.raises(ValueError, match="return_format"):
        SubagentTask(objective="x", return_format="yaml")


def test_task_summary_reports_the_length_not_the_result() -> None:
    """A summary is printed and logged; the result text can be arbitrarily large."""

    task = SubagentTask(objective="x")
    result = SubagentResult(
        task_id=task.task_id,
        child_session_id="ses_child",
        outcome="completed",
        result="a" * 500,
    )

    summary = result.summary()

    assert summary["result_length"] == 500
    assert "a" * 500 not in str(summary)
    paired = task_summary(task, result)
    assert paired["task"]["task_id"] == task.task_id
    assert paired["result"]["outcome"] == "completed"


def test_the_child_prompt_narrows_the_task_by_default() -> None:
    prompt = child_system_prompt(SubagentTask(objective="x"))

    assert prompt == CHILD_SYSTEM_PROMPT
    assert "one narrow task" in prompt


def test_a_custom_system_prompt_replaces_the_default() -> None:
    task = SubagentTask(objective="x", system_prompt="answer in one word")

    assert child_system_prompt(task) == "answer in one word"


def test_subagent_task_error_is_a_tool_class_failure() -> None:
    assert SubagentTaskError("x").exit_code == 12


# --------------------------------------------------------------------------- #
# registry isolation
# --------------------------------------------------------------------------- #


def test_the_child_registry_holds_only_what_the_task_named(store: EventStore) -> None:
    runner = runner_for(store, FakeAdapter())
    task = SubagentTask(objective="read one file", allowed_tools=("read_file",))

    narrowed = runner.child_registry(task)

    assert [manifest.name for manifest in narrowed.manifests()] == ["read_file"]
    assert "write_file" not in narrowed
    assert "run_command" not in narrowed


def test_a_task_naming_nothing_gets_an_empty_registry(store: EventStore) -> None:
    runner = runner_for(store, FakeAdapter())

    assert runner.child_registry(SubagentTask(objective="think")).manifests() == []


def test_an_unknown_tool_name_is_dropped_rather_than_raised_on(store: EventStore) -> None:
    """A server that failed to connect takes its tools with it; the task still runs."""

    runner = runner_for(store, FakeAdapter())
    task = SubagentTask(objective="x", allowed_tools=("read_file", "mcp_absent_thing"))

    narrowed = runner.child_registry(task)

    assert [manifest.name for manifest in narrowed.manifests()] == ["read_file"]


def test_the_child_registry_is_not_the_parents(store: EventStore) -> None:
    parent = default_registry()
    runner = runner_for(store, FakeAdapter(), registry=parent)

    narrowed = runner.child_registry(SubagentTask(objective="x", allowed_tools=("read_file",)))
    narrowed.unregister("read_file")

    assert "read_file" in parent


# --------------------------------------------------------------------------- #
# dispatch: the four outcomes
# --------------------------------------------------------------------------- #


async def test_a_completed_task_writes_started_and_finished_around_the_child(
    store: EventStore,
) -> None:
    seed_session(store)
    adapter = FakeAdapter([text_turn("the log has three operations")])
    runner = runner_for(store, adapter)
    task = SubagentTask(objective="summarize the log")

    result = await runner.dispatch(task, parent_session_id=DEMO_SESSION_ID)

    assert result.succeeded
    assert result.outcome == "completed"
    assert result.result == "the log has three operations"
    parent = store.read_events(DEMO_SESSION_ID)
    assert types_of(parent).count("subagent_task_started") == 1
    assert types_of(parent).count("subagent_task_finished") == 1
    started = payload_of(parent, EventType.SUBAGENT_TASK_STARTED)
    assert started["child_session_id"] == result.child_session_id
    assert started["objective"] == "summarize the log"


async def test_the_childs_work_lands_in_its_own_session(store: EventStore) -> None:
    """The parent's log carries references, not the child's transcript."""

    seed_session(store)
    adapter = FakeAdapter([text_turn("done")])
    runner = runner_for(store, adapter)

    result = await runner.dispatch(SubagentTask(objective="x"), parent_session_id=DEMO_SESSION_ID)

    child = store.read_events(result.child_session_id)
    assert types_of(child)[:2] == ["session_created", "operation_started"]
    assert payload_of(child, EventType.OPERATION_STARTED)["name"] == CHILD_OPERATION_NAME
    assert types_of(child)[-1] == "operation_finished"
    parent = types_of(store.read_events(DEMO_SESSION_ID))
    assert parent[-2:] == ["subagent_task_started", "subagent_task_finished"]
    assert len(parent) == 9  # the seven seeded events plus the two the task added
    assert result.evidence_refs
    assert all(ref.startswith(f"{result.child_session_id}#") for ref in result.evidence_refs)


async def test_a_task_past_its_deadline_reports_a_timeout_and_closes_the_child(
    store: EventStore,
) -> None:
    seed_session(store)

    class SlowAdapter(FakeAdapter):
        """Answers eventually, which is to say long past any deadline."""

        def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
            self.requests.append(request)
            return self._slowly()

        async def _slowly(self) -> AsyncIterator[ModelEvent]:
            await asyncio.sleep(5)
            for event in text_turn("never arrives"):  # pragma: no cover - deadline wins
                yield event

    runner = runner_for(store, SlowAdapter())

    result = await runner.dispatch(
        SubagentTask(objective="x", deadline_ms=20), parent_session_id=DEMO_SESSION_ID
    )

    assert result.outcome == "timeout"
    assert result.error_code == "subagent_deadline_exceeded"
    assert len(finished_payloads(store)) == 1
    # The child's operation is closed, so the session is not owed a recovery.
    child_state = store.load_state(result.child_session_id)
    assert child_state.open_operation_ids == []
    assert (
        payload_of(store.read_events(result.child_session_id), EventType.OPERATION_FAILED)[
            "error_code"
        ]
        == "subagent_deadline_exceeded"
    )


async def test_an_exhausted_iteration_budget_reports_budget_exceeded(store: EventStore) -> None:
    seed_session(store)
    adapter = FakeAdapter(
        [
            tool_call_turn("read_file", {"path": "a.txt"}, call_id="call_1"),
            tool_call_turn("read_file", {"path": "b.txt"}, call_id="call_2"),
            tool_call_turn("read_file", {"path": "c.txt"}, call_id="call_3"),
        ]
    )
    runner = runner_for(store, adapter)
    task = SubagentTask(
        objective="read every file",
        allowed_tools=("read_file",),
        max_iterations=1,
        max_tool_calls=1,
    )

    result = await runner.dispatch(task, parent_session_id=DEMO_SESSION_ID)

    assert result.outcome == "budget_exceeded"
    assert len(finished_payloads(store)) == 1
    assert store.load_state(result.child_session_id).open_operation_ids == []


async def test_an_exhausted_token_budget_reports_budget_exceeded(store: EventStore) -> None:
    seed_session(store)
    adapter = FakeAdapter(
        [
            tool_call_turn(
                "read_file",
                {"path": "a.txt"},
                call_id="call_1",
                usage=TokenUsage(input_tokens=400, output_tokens=400),
            ),
            text_turn("still going", usage=TokenUsage(input_tokens=400, output_tokens=400)),
        ]
    )
    runner = runner_for(store, adapter)
    task = SubagentTask(objective="x", allowed_tools=("read_file",), max_tokens=100)

    result = await runner.dispatch(task, parent_session_id=DEMO_SESSION_ID)

    assert result.outcome == "budget_exceeded"
    assert result.total_tokens > 0


async def test_an_unexpected_exception_is_recorded_rather_than_propagated(
    store: EventStore,
) -> None:
    """A child must never take the parent down, and must never vanish silently."""

    seed_session(store)
    runner = runner_for(store, FakeAdapter([]))  # a script of length zero raises on first call

    result = await runner.dispatch(SubagentTask(objective="x"), parent_session_id=DEMO_SESSION_ID)

    assert result.outcome == "failed"
    assert result.error_code == "subagent_task_error"
    assert result.error is not None
    finished = finished_payloads(store)
    assert len(finished) == 1
    assert finished[0]["outcome"] == "failed"


async def test_a_denied_tool_ends_the_task_without_running_it(store: EventStore) -> None:
    """A child has no console, so an approval-requiring tool is denied, not hung."""

    seed_session(store)
    adapter = FakeAdapter(
        [
            tool_call_turn("write_file", {"path": "a.txt", "content": "x"}, call_id="call_1"),
            text_turn("i could not write"),
        ]
    )
    runner = runner_for(store, adapter)
    task = SubagentTask(objective="write a file", allowed_tools=("write_file",))

    result = await runner.dispatch(task, parent_session_id=DEMO_SESSION_ID)

    child = store.read_events(result.child_session_id)
    denied = payload_of(child, EventType.TOOL_RESULT)
    assert denied["success"] is False
    assert denied["error_code"] == "approval_denied"
    assert len(finished_payloads(store)) == 1


# --------------------------------------------------------------------------- #
# projection
# --------------------------------------------------------------------------- #


async def test_subagent_events_project_onto_the_parent_session(store: EventStore) -> None:
    seed_session(store)
    runner = runner_for(store, FakeAdapter([text_turn("done")]))
    task = SubagentTask(objective="x")

    result = await runner.dispatch(task, parent_session_id=DEMO_SESSION_ID)

    state = store.load_state(DEMO_SESSION_ID)
    assert state.subagent_task_ids == [task.task_id]
    assert state.subagent_sessions[task.task_id] == result.child_session_id
    assert state.subagent_outcomes[task.task_id] == "completed"
    assert state.open_subagent_task_ids == []


async def test_a_json_task_gets_its_answer_uncoerced(store: EventStore) -> None:
    """A child that ignored the format must be visibly wrong, not wrapped into shape."""

    seed_session(store)
    runner = runner_for(store, FakeAdapter([text_turn("plain text answer")]))

    result = await runner.dispatch(
        SubagentTask(objective="x", return_format="json"),
        parent_session_id=DEMO_SESSION_ID,
    )

    assert result.result == "plain text answer"
