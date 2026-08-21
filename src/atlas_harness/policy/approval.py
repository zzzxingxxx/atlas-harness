"""Human approval interface used before any tool with side effects runs."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from atlas_harness.tools.manifest import RiskLevel


class ApprovalMode(StrEnum):
    AUTO = "auto"
    """Approve everything. Only for non-interactive runs the operator trusts."""

    ON_REQUEST = "on_request"
    """Ask whenever the manifest or the policy says approval is required."""

    ALWAYS = "always"
    """Ask for every tool call, including read-only ones."""

    NEVER = "never"
    """Refuse anything that needs approval instead of asking."""


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_id: str
    session_id: str
    operation_id: str | None = None
    call_id: str
    tool_name: str
    tool_version: str
    risk: RiskLevel
    reason: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    preview: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "reason": self.reason,
            "tool_name": self.tool_name,
            "tool_version": self.tool_version,
            "call_id": self.call_id,
            "risk": self.risk.value,
            "arguments": self.arguments,
            "preview": self.preview,
        }


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_id: str
    approved: bool
    reason: str | None = None
    approver: str = "operator"

    def as_payload(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "approved": self.approved,
            "reason": self.reason,
            "approver": self.approver,
        }


class ApprovalGate(ABC):
    """Resolve an approval request. Implementations must not perform tool work."""

    @abstractmethod
    async def resolve(self, request: ApprovalRequest) -> ApprovalDecision: ...


class FixedApprovalGate(ApprovalGate):
    """Answer every request the same way. Used by ``--yes`` and by tests."""

    def __init__(
        self, approved: bool, *, reason: str | None = None, approver: str = "auto"
    ) -> None:
        self.approved = approved
        self.reason = reason
        self.approver = approver
        self.requests: list[ApprovalRequest] = []

    async def resolve(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        return ApprovalDecision(
            approval_id=request.approval_id,
            approved=self.approved,
            reason=self.reason,
            approver=self.approver,
        )


class CallbackApprovalGate(ApprovalGate):
    """Delegate to a coroutine, so a CLI prompt or an HTTP wait can plug in."""

    def __init__(self, callback: Callable[[ApprovalRequest], Awaitable[ApprovalDecision]]) -> None:
        self._callback = callback

    async def resolve(self, request: ApprovalRequest) -> ApprovalDecision:
        return await self._callback(request)


def gate_for_mode(mode: ApprovalMode) -> ApprovalGate:
    """Build the non-interactive gate implied by an approval mode."""

    if mode is ApprovalMode.AUTO:
        return FixedApprovalGate(True, reason="approval mode is auto", approver="policy")
    if mode is ApprovalMode.NEVER:
        return FixedApprovalGate(False, reason="approval mode is never", approver="policy")
    return FixedApprovalGate(
        False,
        reason="interactive approval is required",
        approver="policy",
    )
