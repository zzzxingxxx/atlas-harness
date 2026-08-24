"""Run a task in a child session, under the child's own limits.

Isolation here is structural rather than promised. The child gets a fresh session
id, a fresh :class:`~atlas_harness.agent.queues.QueueManager`, a registry narrowed
to ``allowed_tools`` and an executor built over that narrow registry. The parent's
own objects are never handed to it, so there is no path by which a child could
steer the parent's run or call a tool the task did not name.

The one thing they share is the event store, and they must: the child's events have
to be replayable from the same log, and the parent's ``subagent_task_finished``
has to point at them. Sharing the log is not sharing state -- the child writes its
own session, and the reducer never folds a child's events into the parent.

Resource recovery is the other half. A task that times out, exhausts its budget or
raises still ends in exactly one ``subagent_task_finished`` event, and the child's
operation is still closed. A child left with an open operation would block its own
session forever and would look, to recovery, like a crash that needs a human.
"""

from __future__ import annotations

import asyncio
from typing import Any

from atlas_harness.agent.loop import AgentLoop
from atlas_harness.agent.queues import QueueManager
from atlas_harness.agent.state import DEFAULT_SYSTEM_PROMPT, BudgetLimits, RunResult, StopCause
from atlas_harness.config import Settings
from atlas_harness.context.artifacts import ArtifactStore
from atlas_harness.events import DEFAULT_LANE, EventStore, EventType
from atlas_harness.kernel.clock import Clock, SystemClock
from atlas_harness.kernel.errors import AtlasError
from atlas_harness.kernel.ids import new_id
from atlas_harness.model.protocol import ModelAdapter
from atlas_harness.policy.approval import ApprovalGate, FixedApprovalGate
from atlas_harness.policy.engine import PolicyEngine
from atlas_harness.subagent.task import SubagentResult, SubagentTask
from atlas_harness.tools.executor import ToolExecutor
from atlas_harness.tools.registry import ToolRegistry

CHILD_SYSTEM_PROMPT = """You are a sub-agent with one narrow task.

Do only what the task asks. You have a small budget and a limited tool set: if the
task cannot be finished with them, say what is missing instead of trying something
else. Answer with the result only, no preamble."""

CHILD_OPERATION_NAME = "subagent_task"

_OUTCOME_BY_STOP_CAUSE = {
    StopCause.COMPLETED: "completed",
    StopCause.MAX_ITERATIONS: "budget_exceeded",
    StopCause.MAX_TOOL_CALLS: "budget_exceeded",
    StopCause.TOKEN_BUDGET: "budget_exceeded",
    StopCause.PROVIDER_ERROR: "failed",
    StopCause.CANCELLED: "timeout",
    StopCause.TOOL_DENIED: "denied",
}
"""Map the loop's stop cause onto the plan's task outcomes.

``cancelled`` becomes ``timeout`` because the only thing that cancels a child here
is its deadline. A parent aborting a child is not modelled: the parent awaits the
task, so there is no window in which it could.
"""


class SubagentRunner:
    """Dispatch tasks as isolated child runs and report their outcomes.

    The runner is built from the *parent's* collaborators but never hands them to a
    child. The adapter is shared because a model client is a stateless connection
    pool; the registry, executor and queues are all rebuilt per task.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        store: EventStore,
        adapter: ModelAdapter,
        registry: ToolRegistry,
        approvals: ApprovalGate | None = None,
        policy: PolicyEngine | None = None,
        clock: Clock | None = None,
        lane_id: str = DEFAULT_LANE,
    ) -> None:
        self.settings = settings
        self.store = store
        self.adapter = adapter
        self.registry = registry
        self.policy = policy or PolicyEngine.from_settings(settings)
        self.approvals: ApprovalGate = approvals or FixedApprovalGate(
            False,
            reason="a sub-agent cannot ask a human for approval",
            approver="policy",
        )
        # A child has no console. An approval-requiring tool it was granted is
        # denied rather than left hanging, and the denial is in its own log.
        self.clock = clock or SystemClock()
        self.lane_id = lane_id

    def child_registry(self, task: SubagentTask) -> ToolRegistry:
        """Build the only registry the child will ever see.

        A name the parent does not have is dropped rather than raised on. The
        parent's registry changes with configuration -- an MCP server that failed to
        connect takes its tools with it -- and a task naming one should run with a
        smaller tool set, not fail before it starts.
        """

        narrowed = ToolRegistry()
        for name in task.allowed_tools:
            if name in self.registry:
                narrowed.register(self.registry.get(name))
        return narrowed

    async def dispatch(
        self,
        task: SubagentTask,
        *,
        parent_session_id: str,
        parent_operation_id: str | None = None,
    ) -> SubagentResult:
        """Run one task and write both parent events around it.

        The started event is appended before anything runs and the finished event on
        every path out, including a raise. A dispatched task with no outcome is what
        ``SessionState.open_subagent_task_ids`` reports, and it must only ever mean
        "still running", never "the runner lost it".
        """

        child_session_id = self.store.new_session_id()
        started_ms = self.clock.now_ms()
        self.store.append_new(
            EventType.SUBAGENT_TASK_STARTED,
            session_id=parent_session_id,
            payload={
                "task_id": task.task_id,
                "child_session_id": child_session_id,
                "objective": task.objective,
                "allowed_tools": list(task.allowed_tools),
                "max_tokens": task.max_tokens,
                "deadline_ms": task.deadline_ms,
                "return_format": task.return_format,
                "parent_operation_id": parent_operation_id,
            },
        )
        try:
            result = await self._run_child(task, child_session_id)
            outcome = _OUTCOME_BY_STOP_CAUSE.get(result.stop_cause, "failed")
            report = SubagentResult(
                task_id=task.task_id,
                child_session_id=child_session_id,
                outcome=outcome,
                result=_formatted(result, task),
                error=result.error,
                error_code=result.error_code,
                tool_calls=result.tool_calls,
                total_tokens=result.usage.total_tokens,
                duration_ms=max(0, self.clock.now_ms() - started_ms),
                evidence_refs=self._evidence(child_session_id),
            )
        except TimeoutError:
            report = self._failure(
                task,
                child_session_id,
                outcome="timeout",
                error=f"task exceeded its {task.deadline_ms}ms deadline",
                code="subagent_deadline_exceeded",
                started_ms=started_ms,
            )
        except AtlasError as error:
            report = self._failure(
                task,
                child_session_id,
                outcome="failed",
                error=error.message,
                code=error.code,
                started_ms=started_ms,
            )
        except Exception as error:  # noqa: BLE001 - a child must never take the parent down
            report = self._failure(
                task,
                child_session_id,
                outcome="failed",
                error=str(error),
                code="subagent_task_error",
                started_ms=started_ms,
            )
        self.store.append_new(
            EventType.SUBAGENT_TASK_FINISHED,
            session_id=parent_session_id,
            payload={
                "task_id": report.task_id,
                "child_session_id": report.child_session_id,
                "outcome": report.outcome,
                "result": report.result,
                "error": report.error,
                "error_code": report.error_code,
                "tool_calls": report.tool_calls,
                "total_tokens": report.total_tokens,
                "duration_ms": report.duration_ms,
                "evidence_refs": list(report.evidence_refs),
            },
        )
        return report

    async def _run_child(self, task: SubagentTask, child_session_id: str) -> RunResult:
        """Open the child session, run it under its deadline, and close it out."""

        self.store.append_new(
            EventType.SESSION_CREATED,
            session_id=child_session_id,
            payload={
                "title": task.objective[:80],
                "workspace_root": str(self.settings.resolved_workspace_root()),
            },
        )
        operation_id = new_id("op")
        self.store.append_new(
            EventType.OPERATION_STARTED,
            session_id=child_session_id,
            lane_id=self.lane_id,
            operation_id=operation_id,
            payload={"name": CHILD_OPERATION_NAME, "input_summary": task.objective[:200]},
        )
        registry = self.child_registry(task)
        loop = AgentLoop(
            adapter=self.adapter,
            registry=registry,
            executor=ToolExecutor(
                registry=registry,
                policy=self.policy,
                store=self.store,
                approvals=self.approvals,
                clock=self.clock,
                max_output_bytes=self.settings.max_tool_output_bytes,
            ),
            store=self.store,
            model=self.adapter.capabilities().model,
            provider=self.adapter.capabilities().provider,
            limits=BudgetLimits(
                max_iterations=task.max_iterations,
                max_tool_calls=task.max_tool_calls,
                max_total_tokens=task.max_tokens,
            ),
            system_prompt=task.system_prompt or CHILD_SYSTEM_PROMPT,
            lane_id=self.lane_id,
            max_output_tokens=self.settings.model_max_output_tokens,
            artifacts=ArtifactStore(
                self.store, inline_limit=self.settings.max_artifact_inline_bytes
            ),
            capabilities=None,
        )
        # No capability injection for a child. Retrieval is scoped to a session's
        # own history, and a child has none -- it would spend the task's tokens on
        # memories retrieved for an objective one prompt long.
        cancel = asyncio.Event()
        queues = QueueManager(
            self.store,
            session_id=child_session_id,
            operation_id=operation_id,
            lane_id=self.lane_id,
        )
        try:
            async with asyncio.timeout(task.deadline_ms / 1000):
                return await loop.run(
                    task.objective,
                    session_id=child_session_id,
                    operation_id=operation_id,
                    queues=queues,
                    cancel=cancel,
                )
        except TimeoutError:
            # The loop was cancelled mid-iteration, so it never wrote a terminal
            # operation event. Writing one here is what keeps the child's session
            # out of recovery: an operation left open reads as a crash.
            cancel.set()
            self.store.append_new(
                EventType.OPERATION_FAILED,
                session_id=child_session_id,
                lane_id=self.lane_id,
                operation_id=operation_id,
                payload={
                    "error": f"sub-agent task exceeded its {task.deadline_ms}ms deadline",
                    "error_code": "subagent_deadline_exceeded",
                },
            )
            raise

    def _failure(
        self,
        task: SubagentTask,
        child_session_id: str,
        *,
        outcome: str,
        error: str,
        code: str,
        started_ms: int,
    ) -> SubagentResult:
        """Build the result for a task that did not produce one itself."""

        return SubagentResult(
            task_id=task.task_id,
            child_session_id=child_session_id,
            outcome=outcome,
            error=error,
            error_code=code,
            duration_ms=max(0, self.clock.now_ms() - started_ms),
            evidence_refs=self._evidence(child_session_id),
        )

    def _evidence(self, child_session_id: str) -> tuple[str, ...]:
        """Reference the child's tool results and answers, not its whole log.

        A full event list would grow the parent's log by the size of the child's,
        which defeats the point of delegating. These are the events that carry the
        work behind the answer.
        """

        interesting = {
            EventType.TOOL_RESULT,
            EventType.ASSISTANT_MESSAGE,
            EventType.OPERATION_FAILED,
        }
        return tuple(
            f"{child_session_id}#{event.seq}"
            for event in self.store.read_events(child_session_id)
            if event.event_type in interesting
        )


def _formatted(result: RunResult, task: SubagentTask) -> str:
    """Return the child's answer in the format the task asked for.

    A ``json`` task whose child answered with prose is left as-is rather than
    coerced. The parent asked for a format and can see it did not get one; guessing
    a wrapper here would hide a child that ignored its contract.
    """

    if task.return_format == "json":
        return result.answer.strip()
    return result.answer


def child_system_prompt(task: SubagentTask) -> str:
    """The prompt a child would run under, for ``atlas doctor`` and tests."""

    return task.system_prompt or CHILD_SYSTEM_PROMPT or DEFAULT_SYSTEM_PROMPT


def task_summary(task: SubagentTask, result: SubagentResult) -> dict[str, Any]:
    """Contract and outcome side by side, for the CLI's ``--json``."""

    return {"task": task.summary(), "result": result.summary()}
