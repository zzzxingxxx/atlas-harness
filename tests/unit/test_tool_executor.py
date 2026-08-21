import asyncio
import json
from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict
from tests.conftest import TOOL_SESSION_ID, ExecutorFactory

from atlas_harness.events import EventStore, EventType
from atlas_harness.kernel.errors import ToolError
from atlas_harness.policy import ApprovalDecision, CallbackApprovalGate
from atlas_harness.tools import RiskLevel, Tool, ToolContext, ToolManifest, ToolRegistry
from atlas_harness.tools.executor import ToolCall, ToolExecutor


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LargeOutputTool(Tool):
    manifest = ToolManifest(
        name="large_output",
        version="1.0.0",
        description="return a large result",
        input_schema=EmptyInput.model_json_schema(),
        risk=RiskLevel.READ,
        max_output_bytes=80,
    )
    input_model = EmptyInput

    async def run(self, args: EmptyInput, context: ToolContext) -> dict[str, str]:
        return {
            "secret": "api_key=sk-abcdefghijklmnop123456",
            "first": "x" * 100,
            "second": "y" * 100,
        }


class SlowTool(Tool):
    input_model = EmptyInput

    def __init__(
        self,
        name: str,
        risk: RiskLevel,
        tracker: "ConcurrencyTracker",
    ) -> None:
        self.manifest = ToolManifest(
            name=name,
            version="1.0.0",
            description="track concurrency",
            input_schema=EmptyInput.model_json_schema(),
            risk=risk,
            requires_approval=False,
        )
        self.tracker = tracker

    async def run(self, args: EmptyInput, context: ToolContext) -> str:
        await self.tracker.enter(self.manifest.risk)
        try:
            await asyncio.sleep(0.05)
            return self.manifest.name
        finally:
            await self.tracker.leave(self.manifest.risk)


class FailingTool(Tool):
    manifest = ToolManifest(
        name="failing",
        version="1.0.0",
        description="return a secret error",
        input_schema=EmptyInput.model_json_schema(),
        risk=RiskLevel.READ,
        max_output_bytes=80,
    )
    input_model = EmptyInput

    async def run(self, args: EmptyInput, context: ToolContext) -> None:
        raise ToolError(
            "api_key=sk-abcdefghijklmnop123456",
            details={"secret": "token=abcdefghijklmnop", "large": "x" * 200},
        )


@dataclass
class ConcurrencyTracker:
    read_active: int = 0
    write_active: int = 0
    max_read_active: int = 0
    max_write_active: int = 0

    async def enter(self, risk: RiskLevel) -> None:
        if risk is RiskLevel.READ:
            self.read_active += 1
            self.max_read_active = max(self.max_read_active, self.read_active)
        else:
            self.write_active += 1
            self.max_write_active = max(self.max_write_active, self.write_active)

    async def leave(self, risk: RiskLevel) -> None:
        if risk is RiskLevel.READ:
            self.read_active -= 1
        else:
            self.write_active -= 1


@pytest.mark.asyncio
async def test_executor_applies_one_global_output_budget_and_redacts(
    executor_factory: ExecutorFactory,
    tool_store: EventStore,
) -> None:
    registry = ToolRegistry([LargeOutputTool()])
    executor = executor_factory(registry=registry, max_output_bytes=80)

    outcome = await executor.execute(ToolCall(tool_name="large_output"), session_id=TOOL_SESSION_ID)

    assert outcome.success
    assert outcome.truncated
    assert isinstance(outcome.output, str)
    assert len(outcome.output.encode("utf-8")) <= 80
    assert "[redacted]" in outcome.output
    serialized_log = json.dumps(
        [event.to_json_dict() for event in tool_store.read_events(TOOL_SESSION_ID)]
    )
    assert "sk-abcdefghijklmnop123456" not in serialized_log


@pytest.mark.asyncio
async def test_executor_redacts_and_limits_failures(
    executor_factory: ExecutorFactory,
    tool_store: EventStore,
) -> None:
    executor = executor_factory(registry=ToolRegistry([FailingTool()]), max_output_bytes=80)

    outcome = await executor.execute(ToolCall(tool_name="failing"), session_id=TOOL_SESSION_ID)

    assert not outcome.success
    assert outcome.truncated
    serialized = json.dumps(outcome.model_dump(mode="json"))
    assert "sk-abcdefghijklmnop123456" not in serialized
    assert "abcdefghijklmnop" not in serialized
    event_log = json.dumps(
        [event.to_json_dict() for event in tool_store.read_events(TOOL_SESSION_ID)]
    )
    assert "sk-abcdefghijklmnop123456" not in event_log


@pytest.mark.asyncio
async def test_cancelling_approval_wait_records_resolution_and_tool_result(
    executor_factory: ExecutorFactory,
    tool_store: EventStore,
) -> None:
    async def wait_forever(request: Any) -> ApprovalDecision:
        await asyncio.Future()
        raise AssertionError("unreachable")

    executor = executor_factory(approvals=CallbackApprovalGate(wait_forever))
    task = asyncio.create_task(
        executor.execute(
            ToolCall(tool_name="write_file", arguments={"path": "x.txt", "content": "x"}),
            session_id=TOOL_SESSION_ID,
        )
    )
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    events = tool_store.read_events(TOOL_SESSION_ID)
    assert [event.event_type for event in events[1:]] == [
        EventType.APPROVAL_REQUESTED,
        EventType.APPROVAL_RESOLVED,
        EventType.TOOL_RESULT,
    ]
    assert events[-1].payload.model_dump()["error_code"] == "cancelled"


@pytest.mark.asyncio
async def test_mismatched_approval_id_is_denied_and_fully_audited(
    executor_factory: ExecutorFactory,
    tool_store: EventStore,
) -> None:
    async def wrong_id(request: Any) -> ApprovalDecision:
        return ApprovalDecision(approval_id="apr_wrong", approved=True, approver="test")

    executor = executor_factory(approvals=CallbackApprovalGate(wrong_id))
    outcome = await executor.execute(
        ToolCall(tool_name="write_file", arguments={"path": "x.txt", "content": "x"}),
        session_id=TOOL_SESSION_ID,
    )

    assert not outcome.success
    assert outcome.error_code == "approval_denied"
    assert outcome.error_details["rule"] == "approval_id_mismatch"
    events = tool_store.read_events(TOOL_SESSION_ID)
    assert [event.event_type for event in events[1:]] == [
        EventType.APPROVAL_REQUESTED,
        EventType.APPROVAL_RESOLVED,
        EventType.TOOL_RESULT,
    ]
    assert events[-2].payload.model_dump()["approved"] is False


@pytest.mark.asyncio
async def test_read_calls_run_in_parallel_and_write_calls_run_serially(
    executor_factory: ExecutorFactory,
) -> None:
    tracker = ConcurrencyTracker()
    registry = ToolRegistry(
        [
            SlowTool("read_a", RiskLevel.READ, tracker),
            SlowTool("read_b", RiskLevel.READ, tracker),
            SlowTool("write_a", RiskLevel.WRITE, tracker),
            SlowTool("write_b", RiskLevel.WRITE, tracker),
        ]
    )
    executor: ToolExecutor = executor_factory(registry=registry)

    outcomes = await executor.execute_many(
        [
            ToolCall(tool_name="read_a"),
            ToolCall(tool_name="read_b"),
            ToolCall(tool_name="write_a"),
            ToolCall(tool_name="write_b"),
        ],
        session_id=TOOL_SESSION_ID,
    )

    assert all(outcome.success for outcome in outcomes)
    assert tracker.max_read_active == 2
    assert tracker.max_write_active == 1
