"""Wire settings into a runnable agent: adapter, registry, policy, executor, log.

The loop takes its collaborators as arguments so a test can substitute any of
them. This module is the one place that knows how to build the default set from
:class:`~atlas_harness.config.Settings`, which keeps that knowledge out of both
the loop and the CLI.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from types import TracebackType
from typing import Any

from pydantic import BaseModel, ConfigDict

from atlas_harness.agent.loop import AgentLoop
from atlas_harness.agent.queues import QueueManager, QueueName, QueueRequest, QueueSnapshot
from atlas_harness.agent.state import DEFAULT_SYSTEM_PROMPT, BudgetLimits, RunResult
from atlas_harness.config import Settings
from atlas_harness.context.artifacts import ArtifactStore
from atlas_harness.context.compaction import REASON_MANUAL, CompactionSummary, Compactor
from atlas_harness.context.tokens import ContextBudget
from atlas_harness.events import DEFAULT_LANE, EventStore, EventType
from atlas_harness.events.reducer import OperationState
from atlas_harness.kernel.errors import (
    LifecycleError,
    RecoveryError,
    SessionNotFoundError,
)
from atlas_harness.model.catalog import build_adapter
from atlas_harness.model.protocol import ModelAdapter
from atlas_harness.policy.approval import ApprovalGate, FixedApprovalGate
from atlas_harness.policy.engine import PolicyEngine
from atlas_harness.session.recovery import RecoveryService
from atlas_harness.session.service import SessionService
from atlas_harness.tools.executor import ToolExecutor
from atlas_harness.tools.registry import ToolRegistry, default_registry


class RunReport(BaseModel):
    """One run plus the session it landed in, for the CLI to print."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    result: RunResult
    provider: str
    model: str
    queues: QueueSnapshot

    def summary(self) -> dict[str, Any]:
        return {
            **self.result.summary(),
            "provider": self.provider,
            "model": self.model,
            "pending_queue_messages": self.queues.total,
        }


def _limits(settings: Settings) -> BudgetLimits:
    return BudgetLimits(
        max_iterations=settings.max_iterations,
        max_tool_calls=settings.max_tool_calls,
    )


class AgentService:
    """Build and drive agent runs against one event store."""

    def __init__(
        self,
        *,
        settings: Settings,
        store: EventStore,
        adapter: ModelAdapter | None = None,
        registry: ToolRegistry | None = None,
        executor: ToolExecutor | None = None,
        approvals: ApprovalGate | None = None,
        provider: str | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        lane_id: str = DEFAULT_LANE,
    ) -> None:
        self.settings = settings
        self.store = store
        self.registry = registry or default_registry()
        self.provider = provider or settings.model_provider
        self.adapter = adapter or build_adapter(settings, provider=self.provider)
        self.system_prompt = system_prompt
        self.lane_id = lane_id
        self.executor = executor or ToolExecutor(
            registry=self.registry,
            policy=PolicyEngine.from_settings(settings),
            store=store,
            approvals=approvals
            or FixedApprovalGate(
                False,
                reason="no approval gate configured for this run",
                approver="policy",
            ),
            max_output_bytes=settings.max_tool_output_bytes,
        )
        self.recovery = RecoveryService(store, snapshot_every=settings.snapshot_every_events)
        self.sessions = SessionService(store, recovery=self.recovery)

    @classmethod
    def from_settings(cls, settings: Settings, **kwargs: Any) -> AgentService:
        store = kwargs.pop("store", None) or EventStore.from_settings(settings)
        return cls(settings=settings, store=store, **kwargs)

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> AgentService:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _model_name(self) -> str:
        return self.adapter.capabilities().model

    def build_loop(self) -> AgentLoop:
        capabilities = self.adapter.capabilities()
        return AgentLoop(
            adapter=self.adapter,
            registry=self.registry,
            executor=self.executor,
            store=self.store,
            model=capabilities.model,
            provider=capabilities.provider,
            limits=_limits(self.settings),
            system_prompt=self.system_prompt,
            lane_id=self.lane_id,
            max_output_tokens=min(
                self.settings.model_max_output_tokens,
                capabilities.max_output_tokens,
            ),
            budget=self.context_budget(),
            keep_recent_messages=self.settings.context_keep_recent_turns,
            artifacts=ArtifactStore(
                self.store, inline_limit=self.settings.max_artifact_inline_bytes
            ),
        )

    def ensure_session(self, session_id: str | None, *, title: str) -> str:
        """Return an existing session id or open a new one with its first event."""

        target = session_id or self.store.new_session_id()
        if not self.store.session_exists(target):
            self.store.append_new(
                EventType.SESSION_CREATED,
                session_id=target,
                payload={
                    "title": title,
                    "workspace_root": str(self.settings.resolved_workspace_root()),
                },
            )
        return target

    def enqueue(self, request: QueueRequest, *, session_id: str) -> str:
        """Queue a message for a session that already exists.

        The message is attached to the session's most recent operation so a
        running loop picks it up on its next drain.
        """

        if not self.store.session_exists(session_id):
            raise SessionNotFoundError(
                "cannot queue a message for an unknown session",
                details={"session_id": session_id},
            )
        state = self.store.load_state(session_id)
        operation_id = _latest_operation_id(state.operations)
        queues = QueueManager(
            self.store,
            session_id=session_id,
            operation_id=operation_id,
            lane_id=self.lane_id,
        )
        message = queues.enqueue(request.queue, request.content, source=request.source)
        return message.message_id

    def pending_queues(self, session_id: str) -> QueueSnapshot:
        """Count queue messages no iteration has consumed yet."""

        state = self.store.load_state(session_id)
        operation_id = _latest_operation_id(state.operations)
        queues = QueueManager(
            self.store,
            session_id=session_id,
            operation_id=operation_id,
            lane_id=self.lane_id,
        )
        queues.hydrate(state)
        return queues.snapshot()

    async def run(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        steer: Sequence[str] = (),
        cancel: asyncio.Event | None = None,
    ) -> RunReport:
        """Open or continue a session and drive one operation to a stop cause."""

        report = self.sessions.startup(
            session_id=session_id,
            operation_name="agent_run",
            lane_id=self.lane_id,
            title=_title(prompt),
            workspace_root=str(self.settings.resolved_workspace_root()),
        )
        if report.blocked or report.operation_id is None:
            # A crashed run is owed a decision. Scheduling new work on top of it
            # would bury the question, so the caller has to answer it first.
            raise RecoveryError(
                "session has a suspended operation; resume or abort it before running again",
                details={
                    "session_id": report.session_id,
                    "suspended_operations": report.suspended_operation_ids,
                    "command": None if report.recovery is None else report.recovery.command(),
                },
            )
        target = report.session_id
        operation_id = report.operation_id
        queues = QueueManager(
            self.store,
            session_id=target,
            operation_id=operation_id,
            lane_id=self.lane_id,
        )
        for content in steer:
            queues.enqueue(QueueName.STEER, content, source="cli")
        loop = self.build_loop()
        result = await loop.run(
            prompt,
            session_id=target,
            operation_id=operation_id,
            queues=queues,
            cancel=cancel,
        )
        # After the terminal operation event, so a snapshot never points into the
        # middle of a run. A failure here costs a slower next recovery, nothing more.
        self.recovery.maybe_snapshot(target)
        return RunReport(
            result=result,
            provider=loop.provider,
            model=loop.model,
            queues=queues.snapshot(),
        )

    def run_sync(self, prompt: str, **kwargs: Any) -> RunReport:
        """Blocking entry point for the CLI."""

        return asyncio.run(self.run(prompt, **kwargs))

    # ----------------------------------------------------------------- compaction

    def compact(
        self,
        session_id: str,
        *,
        operation_id: str | None = None,
        reason: str = REASON_MANUAL,
        objective: str = "",
    ) -> CompactionSummary:
        """Compact a session from outside a run, and record that it happened.

        There is no live transcript here, so nothing is replaced: the summary is
        folded out of the log and written as a ``context_compacted`` event. That is
        the useful half for an operator, who wants to see the objective, blockers
        and evidence the session has accumulated and mark the point deliberately
        rather than wait for a threshold to do it.
        """

        if not self.store.session_exists(session_id):
            raise SessionNotFoundError(
                "cannot compact an unknown session",
                details={"session_id": session_id},
            )
        state = self.store.load_state(session_id)
        target = operation_id or _latest_operation_id(state.operations)
        if target is None:
            raise LifecycleError(
                "session has no operation to compact",
                details={"session_id": session_id},
            )
        compactor = Compactor(self.store, budget=self.context_budget())
        summary = compactor.summarize(
            session_id, operation_id=target, objective=objective, state=state
        )
        compactor.compact(
            session_id,
            operation_id=target,
            messages=(),
            used_tokens=0,
            reason=reason,
            lane_id=state.operations[target].lane_id,
        )
        return summary

    def context_budget(self) -> ContextBudget:
        """The same budget the loop uses, so CLI and loop agree on the marks."""

        capabilities = self.adapter.capabilities()
        return ContextBudget.for_model(
            max_context_tokens=capabilities.max_context_tokens,
            reserve_output_tokens=min(
                self.settings.model_max_output_tokens,
                capabilities.max_output_tokens,
            ),
            prepare_ratio=self.settings.context_prepare_ratio,
            compact_ratio=self.settings.context_compact_ratio,
            force_ratio=self.settings.context_force_ratio,
        )


def _latest_operation_id(operations: Mapping[str, OperationState]) -> str | None:
    """Pick the operation a new queue message belongs to.

    An open operation always wins, because that is the run a steer message is
    meant to reach. Otherwise the most recently started one is used so the
    message is still attached to something replayable.
    """

    if not operations:
        return None
    open_ids = [
        operation.operation_id for operation in operations.values() if operation.status == "started"
    ]
    if open_ids:
        return open_ids[-1]
    return list(operations)[-1]


def _title(prompt: str) -> str:
    head = prompt.strip().splitlines()[0] if prompt.strip() else "agent run"
    return head[:80]
