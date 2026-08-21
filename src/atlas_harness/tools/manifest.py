"""Tool declarations: risk, scopes, limits and the policy targets a call implies."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from atlas_harness.kernel.clock import Clock, SystemClock
from atlas_harness.kernel.errors import PolicyDeniedError, ToolInputError

VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")

SCOPE_FS_READ = "fs:read"
SCOPE_FS_WRITE = "fs:write"
SCOPE_PROCESS = "process:run"
SCOPE_NETWORK = "net:request"

DEFAULT_SCOPES = frozenset({SCOPE_FS_READ, SCOPE_FS_WRITE, SCOPE_PROCESS})
"""Network stays out of the default grant even though the interface exists."""


class RiskLevel(StrEnum):
    READ = "read"
    WRITE = "write"
    NETWORK = "network"
    DESTRUCTIVE = "destructive"


APPROVAL_BY_DEFAULT = frozenset({RiskLevel.WRITE, RiskLevel.NETWORK, RiskLevel.DESTRUCTIVE})


class ToolManifest(BaseModel):
    """The auditable contract of one tool version."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    version: str = Field(pattern=VERSION_PATTERN.pattern)
    description: str = Field(min_length=1)
    input_schema: dict[str, Any]
    risk: RiskLevel
    scopes: tuple[str, ...] = ()
    idempotent: bool = False
    timeout_ms: int = Field(default=30_000, gt=0)
    max_output_bytes: int = Field(default=131_072, gt=0)
    requires_approval: bool | None = None
    parallel_safe: bool | None = None

    @property
    def approval_required(self) -> bool:
        if self.requires_approval is not None:
            return self.requires_approval
        return self.risk in APPROVAL_BY_DEFAULT

    @property
    def can_run_in_parallel(self) -> bool:
        """Read-only tools may share a lane; anything with side effects serializes."""

        if self.parallel_safe is not None:
            return self.parallel_safe
        return self.risk is RiskLevel.READ

    def reference(self) -> str:
        return f"{self.name}@{self.version}"

    def describe(self) -> dict[str, Any]:
        """The subset a model or an operator needs, without internal knobs."""

        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "input_schema": self.input_schema,
            "risk": self.risk.value,
            "scopes": list(self.scopes),
            "idempotent": self.idempotent,
            "timeout_ms": self.timeout_ms,
            "requires_approval": self.approval_required,
            "parallel_safe": self.can_run_in_parallel,
        }


class PolicyRequest(BaseModel):
    """What a validated call wants to touch, expressed for the policy engine.

    Tools translate their own arguments into this shape so the engine never has
    to know any tool's argument names.
    """

    model_config = ConfigDict(extra="forbid")

    reads: tuple[str, ...] = ()
    dirs: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    commands: tuple[str | tuple[str, ...], ...] = ()
    urls: tuple[str, ...] = ()

    def summary(self) -> dict[str, Any]:
        return {
            "reads": list(self.reads),
            "dirs": list(self.dirs),
            "writes": list(self.writes),
            "commands": [
                command if isinstance(command, str) else list(command) for command in self.commands
            ],
            "urls": list(self.urls),
        }


class ToolContext(BaseModel):
    """Per-call execution context handed to a tool's ``run``."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    workspace_root: Path
    session_id: str
    call_id: str
    operation_id: str | None = None
    max_output_bytes: int = Field(default=131_072, gt=0)
    max_read_bytes: int = Field(default=1_048_576, gt=0)
    timeout_ms: int = Field(default=30_000, gt=0)
    approval_id: str | None = None
    clock: Clock = Field(default_factory=SystemClock)
    resolved_paths: dict[str, str] = Field(default_factory=dict)
    """Raw request string -> absolute path already vetted by the policy engine."""

    vetted_commands: tuple[tuple[str, ...], ...] = ()
    """Argv tuples produced by the command policy, in declaration order."""

    deny_globs: tuple[str, ...] = ()
    """Sensitive-file patterns a walking tool must skip on its own."""

    def path_for(self, raw: str) -> Path:
        """Return the vetted path for a target this call declared to the policy."""

        vetted = self.resolved_paths.get(raw)
        if vetted is None:
            raise PolicyDeniedError(
                "tool touched a path it never declared to the policy engine",
                details={"path": raw, "rule": "path_not_declared"},
            )
        path = Path(vetted)
        current = path
        while True:
            if current.is_symlink():
                raise PolicyDeniedError(
                    "path became a symbolic link after policy preflight",
                    details={"path": raw, "rule": "path_symlink", "link": str(current)},
                )
            if current == current.parent or current == self.workspace_root:
                break
            current = current.parent
        resolved = path.resolve()
        if resolved != self.workspace_root and not resolved.is_relative_to(self.workspace_root):
            raise PolicyDeniedError(
                "path escaped the workspace after policy preflight",
                details={"path": raw, "rule": "path_outside_workspace"},
            )
        return resolved

    def relative(self, path: Path) -> str:
        """Render a path for output without leaking the absolute workspace root."""

        try:
            return path.relative_to(self.workspace_root).as_posix()
        except ValueError:
            return path.as_posix()


class Tool(ABC):
    """Base class for every tool. Subclasses stay free of policy decisions."""

    manifest: ClassVar[ToolManifest]
    input_model: ClassVar[type[BaseModel]]

    @property
    def name(self) -> str:
        return self.manifest.name

    def parse(self, arguments: dict[str, Any]) -> BaseModel:
        """Validate raw arguments and surface a domain error, not Pydantic's."""

        try:
            return self.input_model.model_validate(arguments)
        except Exception as exc:
            raise ToolInputError(
                f"invalid arguments for {self.manifest.name}",
                details={"tool": self.manifest.name, "error": str(exc)},
            ) from exc

    def policy_request(self, args: Any) -> PolicyRequest:
        """Declare the resources this call needs. Default: nothing."""

        return PolicyRequest()

    def preview(self, args: Any, context: ToolContext) -> str | None:
        """Human-readable description shown when approval is requested."""

        return None

    @abstractmethod
    async def run(self, args: Any, context: ToolContext) -> Any:
        """Execute the call. Raise ToolError subclasses for expected failures."""


def json_schema_for(model: type[BaseModel]) -> dict[str, Any]:
    return model.model_json_schema()
