"""Turn external tool specs into vetted ``ToolManifest`` entries in the one registry.

This is the module the plan's first M8 test aims at: *a malicious MCP tool cannot
bypass the policy*. The argument has two halves, and both live here.

First, a bridged tool is an ordinary :class:`~atlas_harness.tools.manifest.Tool`.
It reaches the model only through the registry, and it reaches execution only
through :class:`~atlas_harness.tools.executor.ToolExecutor`, which runs
``policy.preflight`` before it will call anything. There is no second path -- the
bridge holds no reference to a connection the executor does not gate.

Second, the scopes a bridged manifest declares come from the *config*, narrowed
by what the tool asked for. A server claiming ``fs:write`` on a config that
granted ``fs:read`` is rejected at registration, so the lie is caught before the
tool exists rather than on its first call.

The naming rule matters more than it looks. ``ToolManifest.name`` is
``^[a-z][a-z0-9_]{0,63}$``, and every bridged name is prefixed with its server, so
an external tool can never collide with ``read_file`` or shadow a builtin.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict

from atlas_harness.kernel.errors import ToolError
from atlas_harness.mcp.connection import McpConnection
from atlas_harness.mcp.server import McpServerConfig, McpToolSpec
from atlas_harness.tools.manifest import (
    PolicyRequest,
    RiskLevel,
    Tool,
    ToolContext,
    ToolManifest,
)
from atlas_harness.tools.registry import ToolRegistry

MCP_NAME_PREFIX = "mcp"
"""Every bridged tool is ``mcp_<server>_<tool>``. The prefix is not decoration:
it makes shadowing a builtin impossible and tells an operator reading a
``tool_started`` event that the call left this process."""

REJECTION_REASONS = frozenset(
    {
        "unnamed",
        "invalid_name",
        "name_collision",
        "scope_not_granted",
        "too_many_tools",
        "duplicate",
    }
)
"""Why a bridged tool was refused. Closed, for the same reason the capability
skip reasons are closed: an empty tool list has to be explicable, and "the server
offered nothing" must not look like "everything it offered was refused"."""

_NAME_SAFE = re.compile(r"[^a-z0-9_]+")


class McpToolRejected(BaseModel):
    """One refused tool, with the reason and enough detail to fix the config."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    server: str
    tool: str
    reason: str
    detail: str = ""

    def summary(self) -> dict[str, Any]:
        return {
            "server": self.server,
            "tool": self.tool,
            "reason": self.reason,
            "detail": self.detail,
        }


class McpBridgeResult(BaseModel):
    """What one server contributed, and what it did not."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    server: str
    registered: tuple[str, ...] = ()
    rejected: tuple[McpToolRejected, ...] = ()
    granted_scopes: tuple[str, ...] = ()

    def summary(self) -> dict[str, Any]:
        return {
            "server": self.server,
            "registered": list(self.registered),
            "rejected": [item.summary() for item in self.rejected],
            "granted_scopes": list(self.granted_scopes),
        }


def _normalize(part: str) -> str:
    return _NAME_SAFE.sub("_", part.lower()).strip("_")


def bridged_name(server: str, tool: str) -> str:
    """Build the registry name for one external tool.

    Both halves are normalized because neither is under this runtime's control: a
    server may be named ``files-ro`` and offer ``readFile``, and the manifest
    pattern accepts neither.
    """

    return f"{MCP_NAME_PREFIX}_{_normalize(server)}_{_normalize(tool)}"[:64].rstrip("_")


class McpArguments(BaseModel):
    """Whatever the server's schema names, carried through as given.

    The arguments are validated by the *server*, not here. Reimplementing its JSON
    schema in this process would mean maintaining a second validator whose
    disagreements with the first surface as mysterious tool failures. What this
    model guarantees is the only shape the bridge itself relies on: a dict of named
    arguments. The schema the *model* is shown is the server's own, carried on the
    manifest, so nothing is hidden from the caller by this permissiveness.
    """

    model_config = ConfigDict(extra="allow")


class McpTool(Tool):
    """One external tool, wearing the same contract as a builtin.

    ``policy_request`` declares the *network* rather than any path. A bridged call
    leaves this process; claiming it reads a file would tell the policy engine
    something false, and claiming it touches nothing would let an external call
    slip past the network rule that governs every other remote request.
    """

    def __init__(
        self,
        connection: McpConnection,
        spec: McpToolSpec,
        manifest: ToolManifest,
    ) -> None:
        self.connection = connection
        self.spec = spec
        self.manifest = manifest  # type: ignore[misc]
        self.input_model = McpArguments  # type: ignore[misc]

    @property
    def server(self) -> str:
        return self.connection.config.name

    def policy_request(self, args: Any) -> PolicyRequest:
        config = self.connection.config
        if config.transport == "http":
            return PolicyRequest(urls=(config.url,))
        return PolicyRequest()

    def preview(self, args: Any, context: ToolContext) -> str | None:
        rendered = ", ".join(sorted(args.model_dump(mode="python")))
        return f"{self.server}:{self.spec.name}({rendered})"

    async def run(self, args: Any, context: ToolContext) -> Any:
        result = await self.connection.call(self.spec.name, args.model_dump(mode="python"))
        return {
            "server": result.server,
            "tool": result.tool,
            "output": result.output,
            "truncated": result.truncated,
            "duration_ms": result.duration_ms,
        }


def _manifest_for(
    config: McpServerConfig,
    spec: McpToolSpec,
    *,
    name: str,
) -> ToolManifest:
    """Build the manifest a bridged tool is executed under.

    The scopes are the intersection of what the tool asked for and what the config
    granted, and an empty request inherits the whole grant. That asymmetry is
    intentional: a server that declares nothing gets the config's ceiling, and a
    server that declares something can only ever narrow it.
    """

    requested = set(spec.scopes) or set(config.granted_scopes)
    scopes = tuple(sorted(requested & set(config.granted_scopes)))
    return ToolManifest(
        name=name,
        version="1.0.0",
        description=spec.description or f"{spec.name} on MCP server {config.name}",
        input_schema=spec.input_schema,
        risk=spec.risk if isinstance(spec.risk, RiskLevel) else RiskLevel.NETWORK,
        scopes=scopes,
        idempotent=spec.idempotent,
        timeout_ms=config.call_timeout_ms,
        max_output_bytes=config.max_output_bytes,
        requires_approval=True if config.requires_approval else None,
        parallel_safe=False,
    )


def bridge_tools(
    connection: McpConnection,
    registry: ToolRegistry,
    *,
    replace: bool = False,
) -> McpBridgeResult:
    """Admit one server's tools into ``registry``, refusing the ones that fail.

    A refusal is never fatal. One badly declared tool on an otherwise healthy
    server should cost that tool, not the server -- and certainly not the run.
    """

    config = connection.config
    granted = set(config.granted_scopes)
    registered: list[str] = []
    rejected: list[McpToolRejected] = []
    seen: set[str] = set()

    for spec in connection.tools:
        if not spec.name.strip():
            rejected.append(
                McpToolRejected(server=config.name, tool="", reason="unnamed"),
            )
            continue
        if len(registered) >= config.max_tools:
            rejected.append(
                McpToolRejected(
                    server=config.name,
                    tool=spec.name,
                    reason="too_many_tools",
                    detail=f"server may contribute at most {config.max_tools} tools",
                )
            )
            continue
        name = bridged_name(config.name, spec.name)
        # A tool half that normalizes away would leave the bare server name, so two
        # unnamable tools would silently become one entry with no way back to either.
        if not _normalize(spec.name) or not re.fullmatch(r"^[a-z][a-z0-9_]{0,63}$", name):
            rejected.append(
                McpToolRejected(
                    server=config.name,
                    tool=spec.name,
                    reason="invalid_name",
                    detail=f"{spec.name!r} does not normalize to a usable tool name",
                )
            )
            continue
        if name in seen:
            rejected.append(
                McpToolRejected(
                    server=config.name,
                    tool=spec.name,
                    reason="duplicate",
                    detail=f"another tool already normalized to {name}",
                )
            )
            continue
        missing = sorted(set(spec.scopes) - granted)
        if missing:
            # The whole point of the config grant. A tool asking for more than the
            # operator gave is refused here, so it never becomes a manifest and
            # never reaches a policy decision at all.
            rejected.append(
                McpToolRejected(
                    server=config.name,
                    tool=spec.name,
                    reason="scope_not_granted",
                    detail=f"requested {missing}, server granted {sorted(granted)}",
                )
            )
            continue
        if name in registry and not replace:
            rejected.append(
                McpToolRejected(
                    server=config.name,
                    tool=spec.name,
                    reason="name_collision",
                    detail=f"{name} is already registered",
                )
            )
            continue
        manifest = _manifest_for(config, spec, name=name)
        try:
            registry.register(McpTool(connection, spec, manifest), replace=replace)
        except ToolError as error:
            rejected.append(
                McpToolRejected(
                    server=config.name,
                    tool=spec.name,
                    reason="name_collision",
                    detail=error.message,
                )
            )
            continue
        seen.add(name)
        registered.append(name)

    return McpBridgeResult(
        server=config.name,
        registered=tuple(registered),
        rejected=tuple(rejected),
        granted_scopes=tuple(sorted(granted)),
    )


def unbridge_tools(registry: ToolRegistry, names: tuple[str, ...]) -> None:
    """Remove a server's tools when it disconnects.

    A tool whose connection is gone would fail on its next call with a transport
    error, which reads as a broken tool rather than a server that went away.
    Withdrawing it keeps the registry honest about what can actually be called.
    """

    for name in names:
        registry.unregister(name)
