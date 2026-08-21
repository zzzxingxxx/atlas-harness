"""Policy preflight: one decision point in front of every tool call."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from atlas_harness.config import Settings
from atlas_harness.kernel.clock import Clock
from atlas_harness.kernel.errors import PolicyDeniedError
from atlas_harness.policy.approval import ApprovalMode
from atlas_harness.policy.command_policy import CommandPolicy
from atlas_harness.policy.network_policy import NetworkPolicy
from atlas_harness.policy.path_policy import PathPolicy
from atlas_harness.tools.manifest import (
    DEFAULT_SCOPES,
    PolicyRequest,
    RiskLevel,
    ToolManifest,
)


class PolicyDecision(BaseModel):
    """The outcome of a preflight. Denials are raised, not returned."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str
    risk: RiskLevel
    requires_approval: bool
    reason: str
    targets: dict[str, Any] = Field(default_factory=dict)
    resolved_paths: dict[str, str] = Field(default_factory=dict)
    """Raw request string -> vetted absolute path, handed to the tool as-is."""

    vetted_commands: tuple[tuple[str, ...], ...] = ()


class PolicyEngine:
    """Combine scope, path, command and network rules into one preflight."""

    def __init__(
        self,
        *,
        paths: PathPolicy,
        commands: CommandPolicy | None = None,
        network: NetworkPolicy | None = None,
        approval_mode: ApprovalMode = ApprovalMode.ON_REQUEST,
        granted_scopes: frozenset[str] = DEFAULT_SCOPES,
    ) -> None:
        self.paths = paths
        self.commands = commands or CommandPolicy()
        self.network = network or NetworkPolicy()
        self.approval_mode = approval_mode
        self.granted_scopes = granted_scopes

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        clock: Clock | None = None,
        granted_scopes: frozenset[str] = DEFAULT_SCOPES,
    ) -> PolicyEngine:
        return cls(
            paths=PathPolicy(
                settings.resolved_workspace_root(),
                max_read_bytes=settings.max_read_bytes,
            ),
            commands=CommandPolicy(),
            network=NetworkPolicy(enabled=settings.allow_network, clock=clock),
            approval_mode=ApprovalMode(settings.approval_mode),
            granted_scopes=granted_scopes,
        )

    def preflight(self, manifest: ToolManifest, request: PolicyRequest) -> PolicyDecision:
        """Vet a validated call. Raises before any side effect can happen.

        Resolution happens here and only here: the vetted paths and argv travel
        to the tool inside its context, so a tool can never re-resolve a target
        into something the boundary did not approve.
        """

        self._check_scopes(manifest)
        resolved: dict[str, str] = {}
        for raw in request.reads:
            path = self.paths.resolve_read(raw)
            self.paths.assert_readable_file(path)
            resolved[raw] = str(path)
        for raw in request.dirs:
            directory = self.paths.resolve_read(raw)
            self.paths.assert_directory(directory)
            resolved[raw] = str(directory)
        for raw in request.writes:
            resolved[raw] = str(self.paths.resolve_write(raw))
        vetted = tuple(self.commands.parse(command) for command in request.commands)
        for url in request.urls:
            self.network.check(url)
        return PolicyDecision(
            tool=manifest.name,
            risk=manifest.risk,
            requires_approval=self._requires_approval(manifest),
            reason=self._reason(manifest, request),
            targets=request.summary(),
            resolved_paths=resolved,
            vetted_commands=vetted,
        )

    def _check_scopes(self, manifest: ToolManifest) -> None:
        missing = sorted(set(manifest.scopes) - self.granted_scopes)
        if missing:
            raise PolicyDeniedError(
                "tool requires scopes that were not granted",
                details={
                    "rule": "scope_not_granted",
                    "tool": manifest.name,
                    "missing_scopes": missing,
                    "granted_scopes": sorted(self.granted_scopes),
                },
            )

    def _requires_approval(self, manifest: ToolManifest) -> bool:
        if self.approval_mode is ApprovalMode.ALWAYS:
            return True
        return manifest.approval_required

    def _reason(self, manifest: ToolManifest, request: PolicyRequest) -> str:
        if request.writes:
            return f"{manifest.name} writes {', '.join(request.writes)}"
        if request.commands:
            first = request.commands[0]
            rendered = first if isinstance(first, str) else " ".join(first)
            return f"{manifest.name} runs {rendered}"
        if request.urls:
            return f"{manifest.name} requests {request.urls[0]}"
        return f"{manifest.name} has risk {manifest.risk.value}"
