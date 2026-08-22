"""The crash matrix: a fault on either side of every event the runtime writes.

Twelve points, six event boundaries. For each one the same four questions are asked,
because passing three of them is still a data-loss bug:

* does the event log still validate, with no gap and no duplicate seq;
* does the projection agree with the log;
* was a tool that already produced a ``tool_result`` executed a second time;
* does recovery name the right next command.

The tool call counter is the important one. ``mutate`` is a non-idempotent write, so
if the count ever reaches two the harness has duplicated a side effect, which is the
exact failure M4 exists to prevent.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict
from tests.conftest import StoreFactory

from atlas_harness.agent.loop import (
    FAULT_AFTER_ASSISTANT_MESSAGE,
    FAULT_AFTER_MODEL_REQUESTED,
    FAULT_AFTER_OPERATION_FINISHED,
    FAULT_BEFORE_ASSISTANT_MESSAGE,
    FAULT_BEFORE_MODEL_REQUESTED,
    FAULT_BEFORE_OPERATION_FINISHED,
    AgentLoop,
)
from atlas_harness.events import EventStore, EventType
from atlas_harness.kernel import FaultInjected, FaultInjector, FrozenClock
from atlas_harness.model.providers.fake import FakeAdapter, text_turn, tool_call_turn
from atlas_harness.policy import FixedApprovalGate, NetworkPolicy, PathPolicy, PolicyEngine
from atlas_harness.session.recovery import (
    CONFIRM,
    FAULT_AFTER_SNAPSHOT_CREATED,
    FAULT_BEFORE_SNAPSHOT_CREATED,
    REPLAY,
    RESTORE,
    RecoveryService,
)
from atlas_harness.tools import RiskLevel, Tool, ToolContext, ToolManifest, ToolRegistry
from atlas_harness.tools.executor import (
    FAULT_AFTER_TOOL_RESULT,
    FAULT_AFTER_TOOL_STARTED,
    FAULT_BEFORE_TOOL_RESULT,
    FAULT_BEFORE_TOOL_STARTED,
    ToolExecutor,
)

SESSION_ID = "ses_crash"
OPERATION_ID = "op_crash"


class MutateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str = "x"


class CountingMutateTool(Tool):
    """A non-idempotent write. Every run is a side effect that must not repeat."""

    manifest = ToolManifest(
        name="mutate",
        version="1.0.0",
        description="record one irreversible change",
        input_schema=MutateInput.model_json_schema(),
        risk=RiskLevel.WRITE,
        idempotent=False,
    )
    input_model = MutateInput

    def __init__(self) -> None:
        self.runs: list[str] = []

    async def run(self, args: MutateInput, context: ToolContext) -> str:
        self.runs.append(args.value)
        return f"mutated {args.value} (run {len(self.runs)})"


class CountingPeekTool(Tool):
    """An idempotent read. Safe to replay under its original key."""

    manifest = ToolManifest(
        name="peek",
        version="1.0.0",
        description="read something without changing it",
        input_schema=MutateInput.model_json_schema(),
        risk=RiskLevel.READ,
        idempotent=True,
    )
    input_model = MutateInput

    def __init__(self) -> None:
        self.runs: list[str] = []

    async def run(self, args: MutateInput, context: ToolContext) -> str:
        self.runs.append(args.value)
        return f"peeked {args.value}"


class Harness:
    """One store, one injector, one loop — so every fault shares a timeline."""

    def __init__(
        self,
        store: EventStore,
        faults: FaultInjector,
        workspace: Path,
        clock: FrozenClock,
    ) -> None:
        self.store = store
        self.faults = faults
        self.mutate = CountingMutateTool()
        self.peek = CountingPeekTool()
        self.registry = ToolRegistry([self.mutate, self.peek])
        self.executor = ToolExecutor(
            registry=self.registry,
            policy=PolicyEngine(
                paths=PathPolicy(workspace, max_read_bytes=64_000),
                network=NetworkPolicy(clock=clock),
            ),
            store=store,
            approvals=FixedApprovalGate(True, reason="test", approver="test"),
            clock=clock,
            faults=faults,
        )
        self.recovery = RecoveryService(store, faults=faults)

    def loop(self, adapter: FakeAdapter) -> AgentLoop:
        return AgentLoop(
            adapter=adapter,
            registry=self.registry,
            executor=self.executor,
            store=self.store,
            model="fake-model",
            provider="fake",
        )

    def open_session(self) -> None:
        self.store.append_new(
            EventType.SESSION_CREATED,
            session_id=SESSION_ID,
            payload={"title": "crash", "workspace_root": "/tmp/ws"},
        )
        self.store.append_new(
            EventType.OPERATION_STARTED,
            session_id=SESSION_ID,
            operation_id=OPERATION_ID,
            payload={"name": "agent_run"},
        )

    async def run(self, *turns: list[Any]) -> None:
        await self.loop(FakeAdapter(list(turns))).run(
            "do the thing",
            session_id=SESSION_ID,
            operation_id=OPERATION_ID,
        )


@pytest.fixture
def harness(
    store_factory: StoreFactory,
    faults: FaultInjector,
    workspace: Path,
    clock: FrozenClock,
) -> Iterator[Harness]:
    built = Harness(store_factory(), faults, workspace, clock)
    built.open_session()
    yield built
    faults.clear()


def assert_log_is_intact(store: EventStore, session_id: str) -> list[int]:
    """The log validates and its seqs are 1..N with no gap and no repeat.

    ``read_events`` raises on a duplicate, a gap or a truncated line, so calling it
    at all is most of the assertion; the returned seqs make the failure readable.
    """

    seqs = [event.seq for event in store.read_events(session_id)]
    assert seqs == list(range(1, len(seqs) + 1)), f"log has a gap or a repeat: {seqs}"
    return seqs


def assert_projection_matches_log(store: EventStore, session_id: str) -> None:
    """The reducer's view and the raw log agree on how much happened."""

    events = store.read_events(session_id)
    state = store.load_state(session_id)
    assert state.event_count == len(events)
    assert state.last_seq == (events[-1].seq if events else 0)


# --------------------------------------------------------------------------- #
# the two loop-owned text boundaries
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("point", "expect_event"),
    [
        (FAULT_BEFORE_MODEL_REQUESTED, False),
        (FAULT_AFTER_MODEL_REQUESTED, True),
    ],
)
async def test_crash_around_model_requested(
    harness: Harness, point: str, expect_event: bool
) -> None:
    harness.faults.arm(point)

    with pytest.raises(FaultInjected):
        await harness.run(text_turn("hello"))

    assert_log_is_intact(harness.store, SESSION_ID)
    assert_projection_matches_log(harness.store, SESSION_ID)
    state = harness.store.load_state(SESSION_ID)
    operation = state.operations[OPERATION_ID]

    assert (operation.model_requests == 1) is expect_event
    assert operation.status == "started", "a crashed operation stays open until recovery"
    assert harness.mutate.runs == []

    plan = harness.recovery.plan(SESSION_ID)
    assert plan.unfinished_operation_ids == [OPERATION_ID]
    # No tool was in flight, so nothing is owed a confirmation.
    assert plan.needs_confirmation is False
    assert plan.command() == f"atlas resume {SESSION_ID}"
    recovered = plan.operations[0]
    assert recovered.model_request_incomplete is expect_event


@pytest.mark.parametrize(
    ("point", "expect_event"),
    [
        (FAULT_BEFORE_ASSISTANT_MESSAGE, False),
        (FAULT_AFTER_ASSISTANT_MESSAGE, True),
    ],
)
async def test_crash_around_assistant_message(
    harness: Harness, point: str, expect_event: bool
) -> None:
    harness.faults.arm(point)

    with pytest.raises(FaultInjected):
        await harness.run(text_turn("the answer"))

    assert_log_is_intact(harness.store, SESSION_ID)
    assert_projection_matches_log(harness.store, SESSION_ID)
    state = harness.store.load_state(SESSION_ID)

    assert (state.messages == ["the answer"]) is expect_event
    assert state.operations[OPERATION_ID].status == "started"

    plan = harness.recovery.plan(SESSION_ID)
    assert plan.needs_confirmation is False
    assert plan.command() == f"atlas resume {SESSION_ID}"


@pytest.mark.parametrize(
    ("point", "expect_finished"),
    [
        (FAULT_BEFORE_OPERATION_FINISHED, False),
        (FAULT_AFTER_OPERATION_FINISHED, True),
    ],
)
async def test_crash_around_operation_finished(
    harness: Harness, point: str, expect_finished: bool
) -> None:
    harness.faults.arm(point)

    with pytest.raises(FaultInjected):
        await harness.run(text_turn("done"))

    assert_log_is_intact(harness.store, SESSION_ID)
    assert_projection_matches_log(harness.store, SESSION_ID)
    state = harness.store.load_state(SESSION_ID)
    operation = state.operations[OPERATION_ID]
    plan = harness.recovery.plan(SESSION_ID)

    if expect_finished:
        # The terminal event landed before the crash, so there is nothing to resume.
        assert operation.status == "finished"
        assert plan.unfinished_operation_ids == []
        assert plan.command() == f"atlas inspect {SESSION_ID}"
    else:
        assert operation.status == "started"
        assert plan.unfinished_operation_ids == [OPERATION_ID]
        assert plan.command() == f"atlas resume {SESSION_ID}"


# --------------------------------------------------------------------------- #
# the tool boundaries — where a duplicated side effect would show up
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("point", "expect_started", "expect_runs"),
    [
        (FAULT_BEFORE_TOOL_STARTED, False, 0),
        (FAULT_AFTER_TOOL_STARTED, True, 0),
    ],
)
async def test_crash_around_tool_started(
    harness: Harness, point: str, expect_started: bool, expect_runs: int
) -> None:
    """A crash before the tool ran leaves either no record or a bare ``tool_started``.

    The bare case is the dangerous one: the harness cannot tell whether the write
    landed, so recovery must ask rather than retry.
    """

    harness.faults.arm(point)

    with pytest.raises(FaultInjected):
        await harness.run(tool_call_turn("mutate", {"value": "a"}), text_turn("done"))

    assert_log_is_intact(harness.store, SESSION_ID)
    assert_projection_matches_log(harness.store, SESSION_ID)
    state = harness.store.load_state(SESSION_ID)
    operation = state.operations[OPERATION_ID]

    assert len(harness.mutate.runs) == expect_runs
    assert (len(operation.tool_calls) == 1) is expect_started

    plan = harness.recovery.plan(SESSION_ID)
    recovered = plan.operations[0]

    if not expect_started:
        assert recovered.decisions == []
        assert plan.command() == f"atlas resume {SESSION_ID}"
        return

    call_id = next(iter(operation.tool_calls))
    assert recovered.confirm_call_ids == [call_id]
    assert plan.needs_confirmation is True
    assert plan.command() == f"atlas resume {SESSION_ID} --confirm {call_id}"
    decision = recovered.decisions[0]
    assert decision.action == CONFIRM
    assert "non-idempotent" in decision.reason


@pytest.mark.parametrize(
    ("point", "expect_result"),
    [
        (FAULT_BEFORE_TOOL_RESULT, False),
        (FAULT_AFTER_TOOL_RESULT, True),
    ],
)
async def test_crash_around_tool_result(harness: Harness, point: str, expect_result: bool) -> None:
    """The completion condition: a finished tool is never executed again.

    Both sides ran the tool exactly once. What differs is whether the result was
    recorded, and that is precisely what decides replay versus confirm.
    """

    harness.faults.arm(point)

    with pytest.raises(FaultInjected):
        await harness.run(tool_call_turn("mutate", {"value": "a"}), text_turn("done"))

    assert_log_is_intact(harness.store, SESSION_ID)
    assert_projection_matches_log(harness.store, SESSION_ID)
    assert harness.mutate.runs == ["a"], "the tool ran once before the crash"

    state = harness.store.load_state(SESSION_ID)
    operation = state.operations[OPERATION_ID]
    call_id = next(iter(operation.tool_calls))
    plan = harness.recovery.plan(SESSION_ID)
    decision = plan.operations[0].decisions[0]

    if expect_result:
        # The result is on disk, so the call is settled: restore it, never re-run it.
        assert decision.action == RESTORE
        assert plan.needs_confirmation is False
        assert plan.command() == f"atlas resume {SESSION_ID}"
        resumed = harness.recovery.resume(SESSION_ID)
        assert resumed.operations[0].replay_call_ids == []
    else:
        # The write happened but was never recorded. Replaying would duplicate it.
        assert decision.action == CONFIRM
        assert plan.command() == f"atlas resume {SESSION_ID} --confirm {call_id}"

    # Recovery is a projection, not an execution: the counter never moves.
    assert harness.mutate.runs == ["a"]


async def test_finished_tool_is_not_rerun_when_only_the_result_write_failed(
    harness: Harness,
) -> None:
    """The plan's exact wording: a completed tool must not re-run because the
    result write failed. The tool ran, the log lost the result, and recovery still
    refuses to execute it a second time — it asks instead."""

    harness.faults.arm(FAULT_BEFORE_TOOL_RESULT)

    with pytest.raises(FaultInjected):
        await harness.run(tool_call_turn("mutate", {"value": "once"}), text_turn("done"))

    assert harness.mutate.runs == ["once"]
    plan = harness.recovery.plan(SESSION_ID)
    call_id = plan.operations[0].confirm_call_ids[0]

    # Resuming without the confirmation leaves it suspended and runs nothing.
    harness.recovery.suspend_from_plan(plan)
    still_pending = harness.recovery.resume(SESSION_ID)
    assert still_pending.needs_confirmation is True
    assert harness.mutate.runs == ["once"]

    # Even with the confirmation, recovery only records the decision. Re-execution is
    # the caller's next run, never a side effect of resuming.
    resumed = harness.recovery.resume(SESSION_ID, confirm=[call_id])
    assert resumed.needs_confirmation is False
    assert harness.mutate.runs == ["once"]
    assert_log_is_intact(harness.store, SESSION_ID)


async def test_idempotent_read_is_replayed_not_suspended(harness: Harness) -> None:
    harness.faults.arm(FAULT_AFTER_TOOL_STARTED)

    with pytest.raises(FaultInjected):
        await harness.run(tool_call_turn("peek", {"value": "a"}), text_turn("done"))

    plan = harness.recovery.plan(SESSION_ID)
    decision = plan.operations[0].decisions[0]

    assert decision.action == REPLAY
    assert plan.needs_confirmation is False
    assert plan.command() == f"atlas resume {SESSION_ID}"


# --------------------------------------------------------------------------- #
# the snapshot boundary
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("point", "expect_event"),
    [
        (FAULT_BEFORE_SNAPSHOT_CREATED, False),
        (FAULT_AFTER_SNAPSHOT_CREATED, True),
    ],
)
async def test_crash_around_snapshot_created(
    harness: Harness, point: str, expect_event: bool
) -> None:
    """A crash either side of a snapshot never costs correctness.

    Before the file is written there is nothing at all. After the event there is a
    usable snapshot. In between there is an orphan file that nothing points at, which
    is why the file is written first.
    """

    await harness.run(text_turn("first answer"))
    harness.faults.arm(point)

    with pytest.raises(FaultInjected):
        harness.recovery.create_snapshot(SESSION_ID)

    assert_log_is_intact(harness.store, SESSION_ID)
    assert_projection_matches_log(harness.store, SESSION_ID)
    state = harness.store.load_state(SESSION_ID)

    assert (len(state.snapshot_records) == 1) is expect_event
    files = sorted(harness.recovery.snapshot_dir(SESSION_ID).glob("*.json"))
    assert len(files) == (1 if expect_event else 0)

    # Either way the session replays to the same state, with or without a snapshot.
    plan = harness.recovery.plan(SESSION_ID)
    assert plan.last_valid_seq == state.last_seq
    assert (plan.snapshot_id is not None) is expect_event


async def test_orphan_snapshot_file_is_ignored_by_recovery(harness: Harness) -> None:
    """A file with no announcing event is invisible: recovery replays the log."""

    await harness.run(text_turn("answer"))
    harness.faults.arm(FAULT_AFTER_SNAPSHOT_CREATED)
    with pytest.raises(FaultInjected):
        harness.recovery.create_snapshot(SESSION_ID)

    # Corrupt the file the event points at. Recovery must fall back, not fail.
    for path in harness.recovery.snapshot_dir(SESSION_ID).glob("*.json"):
        path.write_text("{not json", encoding="utf-8")

    plan = harness.recovery.plan(SESSION_ID)

    assert plan.snapshot_id is None
    assert plan.resumed_from_seq == 0
    assert plan.last_valid_seq == harness.store.load_state(SESSION_ID).last_seq


async def test_snapshot_lets_recovery_resume_from_a_later_seq(harness: Harness) -> None:
    await harness.run(text_turn("answer"))
    record = harness.recovery.create_snapshot(SESSION_ID)

    plan = harness.recovery.plan(SESSION_ID)

    assert plan.snapshot_id == record.snapshot_id
    assert plan.resumed_from_seq == record.last_seq
    assert plan.last_valid_seq >= record.last_seq
