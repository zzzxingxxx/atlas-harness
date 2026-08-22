"""Execute one tool call: validate, preflight, approve, run, record.

Every path through :meth:`ToolExecutor.execute` ends in a ``tool_result`` event,
so a refused, timed out or crashed call is as auditable as a successful one.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field

from atlas_harness.events import DEFAULT_LANE, EventStore, EventType
from atlas_harness.kernel.clock import Clock, SystemClock
from atlas_harness.kernel.errors import (
    ApprovalDeniedError,
    AtlasError,
    ToolError,
    ToolTimeoutError,
)
from atlas_harness.kernel.faults import FaultInjector
from atlas_harness.kernel.ids import idempotency_key, new_id
from atlas_harness.policy.approval import (
    ApprovalDecision,
    ApprovalGate,
    ApprovalRequest,
    FixedApprovalGate,
)
from atlas_harness.policy.engine import PolicyDecision, PolicyEngine
from atlas_harness.tools.manifest import Tool, ToolContext, ToolManifest
from atlas_harness.tools.redaction import redact, redact_value, truncate_text
from atlas_harness.tools.registry import ToolRegistry

LOGGER = logging.getLogger(__name__)

FAULT_BEFORE_TOOL_STARTED = "tool_executor.before_tool_started"
FAULT_AFTER_TOOL_STARTED = "tool_executor.after_tool_started"
FAULT_BEFORE_TOOL_RESULT = "tool_executor.before_tool_result"
FAULT_AFTER_TOOL_RESULT = "tool_executor.after_tool_result"
"""A crash here is the case M4 exists for: the side effect landed *and* was
recorded, so recovery must restore the result instead of running the tool again."""

MAX_ARGUMENT_BYTES = 4_096
"""Arguments are echoed into the event log, so they get their own budget."""


@dataclass(frozen=True, slots=True)
class _Scope:
    """Where one call is recorded: session, operation, lane and start time."""

    session_id: str
    operation_id: str | None
    lane_id: str
    started_ms: int


class ToolCall(BaseModel):
    """A request to run one tool, as parsed from a model or a CLI argument."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    call_id: str = Field(default_factory=lambda: new_id("call"))
    tool_version: str | None = None


class ToolOutcome(BaseModel):
    """The normalized, redacted and truncated result of one tool call."""

    model_config = ConfigDict(extra="forbid")

    call_id: str
    tool_name: str
    tool_version: str | None = None
    success: bool
    output: Any = None
    error: str | None = None
    error_code: str | None = None
    error_details: dict[str, Any] = Field(default_factory=dict)
    truncated: bool = False
    duration_ms: int = 0
    approval_id: str | None = None
    approved: bool | None = None
    idempotency_key: str


def _limit_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Redact and shrink arguments before they are written to the log."""

    normalized = redact_value(arguments)
    serialized = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(serialized.encode("utf-8")) <= MAX_ARGUMENT_BYTES:
        return cast(dict[str, Any], json.loads(serialized))
    preview, _ = truncate_text(serialized, MAX_ARGUMENT_BYTES)
    return {"truncated": True, "preview": preview}


def _limit_preview(preview: str | None) -> str | None:
    """An approval preview is operator-facing text, so it is redacted too."""

    if preview is None:
        return None
    text, _ = truncate_text(redact(preview), MAX_ARGUMENT_BYTES)
    return text


def _normalize_output(value: Any, max_bytes: int) -> tuple[Any, bool]:
    """Redact secrets and cap the serialized result at one global byte budget."""

    redacted = redact_value(value)
    serialized = json.dumps(redacted, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(serialized.encode("utf-8")) <= max_bytes:
        return json.loads(serialized), False
    preview, _ = truncate_text(serialized, max_bytes)
    return preview, True


class ToolExecutor:
    """Run tool calls under the policy boundary and record standard events."""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        policy: PolicyEngine,
        store: EventStore,
        approvals: ApprovalGate | None = None,
        clock: Clock | None = None,
        faults: FaultInjector | None = None,
        max_output_bytes: int = 131_072,
    ) -> None:
        self.registry = registry
        self.policy = policy
        self.store = store
        self.approvals: ApprovalGate = approvals or FixedApprovalGate(
            False, reason="no approval gate configured", approver="policy"
        )
        self.clock: Clock = clock or SystemClock()
        self.faults = faults or FaultInjector()
        self.max_output_bytes = max_output_bytes

    async def execute(
        self,
        call: ToolCall,
        *,
        session_id: str,
        operation_id: str | None = None,
        lane_id: str = DEFAULT_LANE,
    ) -> ToolOutcome:
        """Run one call. Expected failures become a failed outcome, not a raise."""

        started_ms = self.clock.now_ms()
        scope = _Scope(
            session_id=session_id,
            operation_id=operation_id,
            lane_id=lane_id,
            started_ms=started_ms,
        )
        try:
            tool = self.registry.get(call.tool_name, version=call.tool_version)
        except AtlasError as error:
            return self._failed(call, scope, error)
        manifest = tool.manifest
        try:
            args = tool.parse(call.arguments)
            request = tool.policy_request(args)
            decision = self.policy.preflight(manifest, request)
        except AtlasError as error:
            return self._failed(call, scope, error, manifest=manifest)
        context = self._context(manifest, call, scope, decision)
        try:
            approval = await self._approve(tool, args, context, call, scope, decision)
        except asyncio.CancelledError:
            self._failed(
                call,
                scope,
                ToolError("approval wait was cancelled", details={"tool": manifest.name}),
                manifest=manifest,
                error_code="cancelled",
            )
            raise
        except AtlasError as error:
            return self._failed(call, scope, error, manifest=manifest)
        except Exception as exc:
            LOGGER.exception("approval gate failed for %s", manifest.name)
            return self._failed(
                call,
                scope,
                ToolError(
                    "approval gate raised an unexpected error",
                    details={"tool": manifest.name, "error": str(exc)},
                ),
                manifest=manifest,
            )
        if approval is not None and not approval.approved:
            return self._failed(
                call,
                scope,
                ApprovalDeniedError(
                    "approval was not granted",
                    details={"rule": "approval_denied", "reason": approval.reason},
                ),
                manifest=manifest,
                approval_id=approval.approval_id,
                approved=False,
                error_code="approval_denied",
            )
        return await self._run(tool, args, context, call, scope, approval)

    async def execute_many(
        self,
        calls: Sequence[ToolCall],
        *,
        session_id: str,
        operation_id: str | None = None,
        lane_id: str = DEFAULT_LANE,
    ) -> list[ToolOutcome]:
        """Run read-only calls concurrently and anything with effects serially."""

        outcomes: list[ToolOutcome | None] = [None] * len(calls)
        batch: list[int] = []

        async def flush() -> None:
            if not batch:
                return
            gathered = await asyncio.gather(
                *(
                    self.execute(
                        calls[index],
                        session_id=session_id,
                        operation_id=operation_id,
                        lane_id=lane_id,
                    )
                    for index in batch
                )
            )
            for index, outcome in zip(batch, gathered, strict=True):
                outcomes[index] = outcome
            batch.clear()

        for index, call in enumerate(calls):
            if self._parallel_safe(call):
                batch.append(index)
                continue
            await flush()
            outcomes[index] = await self.execute(
                call,
                session_id=session_id,
                operation_id=operation_id,
                lane_id=lane_id,
            )
        await flush()
        return [outcome for outcome in outcomes if outcome is not None]

    def _parallel_safe(self, call: ToolCall) -> bool:
        try:
            manifest = self.registry.get(call.tool_name).manifest
        except AtlasError:
            return False
        return manifest.can_run_in_parallel

    def _context(
        self,
        manifest: ToolManifest,
        call: ToolCall,
        scope: _Scope,
        decision: PolicyDecision,
    ) -> ToolContext:
        return ToolContext(
            workspace_root=self.policy.paths.workspace_root,
            session_id=scope.session_id,
            operation_id=scope.operation_id,
            call_id=call.call_id,
            max_output_bytes=min(self.max_output_bytes, manifest.max_output_bytes),
            max_read_bytes=self.policy.paths.max_read_bytes,
            timeout_ms=manifest.timeout_ms,
            clock=self.clock,
            resolved_paths=decision.resolved_paths,
            vetted_commands=decision.vetted_commands,
            deny_globs=self.policy.paths.deny_globs,
        )

    async def _approve(
        self,
        tool: Tool,
        args: BaseModel,
        context: ToolContext,
        call: ToolCall,
        scope: _Scope,
        decision: PolicyDecision,
    ) -> ApprovalDecision | None:
        if not decision.requires_approval:
            return None
        manifest = tool.manifest
        approval_id = new_id("apr")
        approval_request = ApprovalRequest(
            approval_id=approval_id,
            session_id=scope.session_id,
            operation_id=scope.operation_id,
            call_id=call.call_id,
            tool_name=manifest.name,
            tool_version=manifest.version,
            risk=manifest.risk,
            reason=decision.reason,
            arguments=_limit_arguments(call.arguments),
            preview=_limit_preview(tool.preview(args, context)),
        )
        self._append(EventType.APPROVAL_REQUESTED, scope, approval_request.as_payload())
        try:
            resolved = await self.approvals.resolve(approval_request)
        except asyncio.CancelledError:
            cancelled = ApprovalDecision(
                approval_id=approval_id,
                approved=False,
                reason="approval wait was cancelled",
                approver="policy",
            )
            self._append(EventType.APPROVAL_RESOLVED, scope, cancelled.as_payload())
            raise
        except Exception as exc:
            resolved = ApprovalDecision(
                approval_id=approval_id,
                approved=False,
                reason=f"approval gate failed: {redact(str(exc))}",
                approver="policy",
            )
        if resolved.approval_id != approval_id:
            denied = ApprovalDecision(
                approval_id=approval_id,
                approved=False,
                reason="approval gate returned a mismatched approval id",
                approver="policy",
            )
            self._append(EventType.APPROVAL_RESOLVED, scope, denied.as_payload())
            raise ApprovalDeniedError(
                "approval gate returned a mismatched approval id",
                details={
                    "rule": "approval_id_mismatch",
                    "expected": approval_id,
                    "actual": resolved.approval_id,
                },
            )
        self._append(EventType.APPROVAL_RESOLVED, scope, resolved.as_payload())
        context.approval_id = approval_id
        return resolved

    async def _run(
        self,
        tool: Tool,
        args: BaseModel,
        context: ToolContext,
        call: ToolCall,
        scope: _Scope,
        approval: ApprovalDecision | None,
    ) -> ToolOutcome:
        manifest = tool.manifest
        approval_id = approval.approval_id if approval is not None else None
        tool_key = self._tool_idempotency_key(call, scope)
        self.faults.check(FAULT_BEFORE_TOOL_STARTED)
        self._append(
            EventType.TOOL_STARTED,
            scope,
            {
                "tool_name": manifest.name,
                "call_id": call.call_id,
                "arguments": _limit_arguments(call.arguments),
                "tool_version": manifest.version,
                "risk": manifest.risk.value,
                "idempotent": manifest.idempotent,
                "approval_id": approval_id,
                "idempotency_key": tool_key,
            },
        )
        self.faults.check(FAULT_AFTER_TOOL_STARTED)
        try:
            async with asyncio.timeout(manifest.timeout_ms / 1000):
                raw = await tool.run(args, context)
        except TimeoutError:
            error: AtlasError = ToolTimeoutError(
                "tool exceeded its timeout",
                details={"tool": manifest.name, "timeout_ms": manifest.timeout_ms},
            )
            return self._failed(
                call,
                scope,
                error,
                manifest=manifest,
                approval_id=approval_id,
                approved=None if approval is None else approval.approved,
            )
        except asyncio.CancelledError:
            self._failed(
                call,
                scope,
                ToolError("tool call was cancelled", details={"tool": manifest.name}),
                manifest=manifest,
                approval_id=approval_id,
                approved=None if approval is None else approval.approved,
                error_code="cancelled",
            )
            raise
        except AtlasError as domain_error:
            return self._failed(
                call,
                scope,
                domain_error,
                manifest=manifest,
                approval_id=approval_id,
                approved=None if approval is None else approval.approved,
            )
        except Exception as exc:
            LOGGER.exception("tool %s raised an unexpected error", manifest.name)
            return self._failed(
                call,
                scope,
                ToolError(
                    "tool raised an unexpected error",
                    details={"tool": manifest.name, "error": str(exc)},
                ),
                manifest=manifest,
                approval_id=approval_id,
                approved=None if approval is None else approval.approved,
            )
        output, truncated = _normalize_output(raw, context.max_output_bytes)
        outcome = ToolOutcome(
            call_id=call.call_id,
            tool_name=manifest.name,
            tool_version=manifest.version,
            success=True,
            output=output,
            truncated=truncated,
            duration_ms=self.clock.now_ms() - scope.started_ms,
            approval_id=approval_id,
            approved=None if approval is None else approval.approved,
            idempotency_key=tool_key,
        )
        self.faults.check(FAULT_BEFORE_TOOL_RESULT)
        self._append(EventType.TOOL_RESULT, scope, self._result_payload(outcome))
        self.faults.check(FAULT_AFTER_TOOL_RESULT)
        return outcome

    def _failed(
        self,
        call: ToolCall,
        scope: _Scope,
        error: AtlasError,
        *,
        manifest: ToolManifest | None = None,
        approval_id: str | None = None,
        approved: bool | None = None,
        error_code: str | None = None,
    ) -> ToolOutcome:
        output_limit = min(
            self.max_output_bytes,
            self.max_output_bytes if manifest is None else manifest.max_output_bytes,
        )
        error_message, message_cut = truncate_text(redact(error.message), output_limit)
        normalized_details, details_cut = _normalize_output(error.details, output_limit)
        if not isinstance(normalized_details, dict):
            normalized_details = {"summary": normalized_details}
        outcome = ToolOutcome(
            call_id=call.call_id,
            tool_name=call.tool_name,
            tool_version=None if manifest is None else manifest.version,
            success=False,
            error=error_message,
            error_code=error_code or error.code,
            error_details=normalized_details,
            truncated=message_cut or details_cut,
            duration_ms=self.clock.now_ms() - scope.started_ms,
            approval_id=approval_id,
            approved=approved,
            idempotency_key=self._tool_idempotency_key(call, scope),
        )
        self._append(EventType.TOOL_RESULT, scope, self._result_payload(outcome))
        return outcome

    def _result_payload(self, outcome: ToolOutcome) -> dict[str, Any]:
        return {
            "tool_name": outcome.tool_name,
            "call_id": outcome.call_id,
            "success": outcome.success,
            "output": outcome.output,
            "error": outcome.error,
            "error_code": outcome.error_code,
            "error_details": outcome.error_details,
            "truncated": outcome.truncated,
            "duration_ms": outcome.duration_ms,
            "approval_id": outcome.approval_id,
            "tool_version": outcome.tool_version,
            "idempotency_key": outcome.idempotency_key,
        }

    def _tool_idempotency_key(self, call: ToolCall, scope: _Scope) -> str:
        return idempotency_key(
            scope.session_id,
            scope.operation_id or "-",
            call.call_id,
        )

    def _append(self, event_type: EventType, scope: _Scope, payload: dict[str, Any]) -> None:
        self.store.append_new(
            event_type,
            session_id=scope.session_id,
            payload=payload,
            lane_id=scope.lane_id,
            operation_id=scope.operation_id,
            idempotency_key_value=idempotency_key(
                scope.session_id,
                scope.operation_id or "-",
                payload.get("call_id") or payload.get("approval_id") or "-",
                event_type.value,
            ),
        )
