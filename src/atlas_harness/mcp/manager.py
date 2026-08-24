"""Own the connections: start them, record them, tear them down.

This is the only module that writes MCP events. Connecting and disconnecting are
session facts, so they are appended as events rather than kept in memory -- an
audit asking "was this tool reachable when it was called" has to be answerable
from the log weeks later, and asking the server again answers a different
question.

The manager is deliberately not a context manager over a whole run. Servers are
started once and closed once, and the close path has to work from a failure as
well as from an orderly shutdown, so :meth:`shutdown` is idempotent and never
raises for a server that was already gone.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from atlas_harness.events.models import EventType
from atlas_harness.events.store import EventStore
from atlas_harness.kernel.clock import Clock, SystemClock
from atlas_harness.mcp.bridge import McpBridgeResult, bridge_tools, unbridge_tools
from atlas_harness.mcp.connection import (
    McpConnection,
    McpConnectionError,
    McpTransport,
    StdioTransport,
)
from atlas_harness.mcp.server import McpServerConfig
from atlas_harness.tools.registry import ToolRegistry


class McpServerStatus(BaseModel):
    """What ``atlas mcp list`` prints for one server."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    transport: str
    address: str = ""
    enabled: bool = True
    connected: bool = False
    protocol_version: str = ""
    tools: tuple[str, ...] = ()
    rejected: tuple[dict[str, Any], ...] = ()
    granted_scopes: tuple[str, ...] = ()
    error: str | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "transport": self.transport,
            "address": self.address,
            "enabled": self.enabled,
            "connected": self.connected,
            "protocol_version": self.protocol_version,
            "tools": list(self.tools),
            "rejected": [dict(item) for item in self.rejected],
            "granted_scopes": list(self.granted_scopes),
            "error": self.error,
        }


def default_transport(config: McpServerConfig) -> McpTransport:
    """Pick a transport for a config. Only stdio is implemented.

    An http server is refused rather than silently downgraded: the config asked
    for a remote endpoint, and connecting to something else would be worse than
    not connecting.
    """

    if config.transport == "stdio":
        return StdioTransport(config)
    raise McpConnectionError(
        "transport is not implemented",
        details={
            "server": config.name,
            "transport": config.transport,
            "reason": "transport_error",
        },
    )


class McpManager:
    """Connect configured servers, bridge their tools, and record both."""

    def __init__(
        self,
        store: EventStore,
        registry: ToolRegistry,
        *,
        configs: tuple[McpServerConfig, ...] = (),
        clock: Clock | None = None,
    ) -> None:
        self.store = store
        self.registry = registry
        self.configs = configs
        self.clock = clock or SystemClock()
        self.connections: dict[str, McpConnection] = {}
        self.bridged: dict[str, McpBridgeResult] = {}
        self.failures: dict[str, str] = {}

    async def connect_all(
        self,
        session_id: str,
        *,
        transports: dict[str, McpTransport] | None = None,
    ) -> list[McpServerStatus]:
        """Bring up every enabled server, recording each outcome.

        One server failing does not stop the others. The runtime's job is to offer
        the tools it can reach; a single unreachable server should cost its own
        tools and nothing more.
        """

        statuses: list[McpServerStatus] = []
        for config in self.configs:
            if not config.enabled:
                statuses.append(
                    McpServerStatus(
                        name=config.name,
                        transport=config.transport,
                        address=config.address(),
                        enabled=False,
                        granted_scopes=config.granted_scopes,
                    )
                )
                continue
            transport = (transports or {}).get(config.name)
            statuses.append(
                await self.connect(session_id, config, transport=transport),
            )
        return statuses

    async def connect(
        self,
        session_id: str,
        config: McpServerConfig,
        *,
        transport: McpTransport | None = None,
    ) -> McpServerStatus:
        """Connect one server, bridge what it offers, and append both events."""

        config.check()
        connection = McpConnection(
            config,
            transport or default_transport(config),
            clock=self.clock,
        )
        try:
            spec = await connection.connect()
        except McpConnectionError as error:
            self.failures[config.name] = error.message
            # The failure is recorded as a disconnect rather than swallowed. A
            # server that was configured and never reached is a fact an operator
            # needs from the log, not from a stderr line that scrolled past.
            self.store.append_new(
                EventType.MCP_SERVER_DISCONNECTED,
                session_id=session_id,
                payload={
                    "server": config.name,
                    "reason": str(error.details.get("reason") or "handshake_failed"),
                    "detail": error.message,
                },
            )
            return McpServerStatus(
                name=config.name,
                transport=config.transport,
                address=config.address(),
                granted_scopes=config.granted_scopes,
                error=error.message,
            )

        self.connections[config.name] = connection
        self.failures.pop(config.name, None)
        self.store.append_new(
            EventType.MCP_SERVER_CONNECTED,
            session_id=session_id,
            payload={
                "server": config.name,
                "transport": config.transport,
                "address": config.address(),
                "protocol_version": spec.protocol_version,
                "tool_count": len(spec.tools),
                "capabilities": list(spec.capabilities),
                "connected_at_ms": connection.connected_at_ms,
            },
        )
        result = bridge_tools(connection, self.registry)
        self.bridged[config.name] = result
        self.store.append_new(
            EventType.MCP_TOOLS_REGISTERED,
            session_id=session_id,
            payload={
                "server": config.name,
                "tools": list(result.registered),
                "rejected": [item.summary() for item in result.rejected],
                "granted_scopes": list(result.granted_scopes),
            },
        )
        return McpServerStatus(
            name=config.name,
            transport=config.transport,
            address=config.address(),
            connected=True,
            protocol_version=spec.protocol_version,
            tools=result.registered,
            rejected=tuple(item.summary() for item in result.rejected),
            granted_scopes=result.granted_scopes,
        )

    async def shutdown(self, session_id: str, *, reason: str = "shutdown") -> list[str]:
        """Close every connection and withdraw its tools.

        The tools come out of the registry before the event is written, so there is
        no window in which the log says a server is gone while its tools are still
        callable.
        """

        closed: list[str] = []
        for name in sorted(self.connections):
            connection = self.connections[name]
            duration_ms = connection.duration_ms()
            result = self.bridged.pop(name, None)
            if result is not None:
                unbridge_tools(self.registry, result.registered)
            try:
                await connection.close()
            except Exception as error:  # noqa: BLE001 - a failed close is still a close
                self.store.append_new(
                    EventType.MCP_SERVER_DISCONNECTED,
                    session_id=session_id,
                    payload={
                        "server": name,
                        "reason": "transport_error",
                        "detail": str(error),
                        "duration_ms": duration_ms,
                    },
                )
                closed.append(name)
                continue
            self.store.append_new(
                EventType.MCP_SERVER_DISCONNECTED,
                session_id=session_id,
                payload={"server": name, "reason": reason, "duration_ms": duration_ms},
            )
            closed.append(name)
        self.connections.clear()
        return closed

    def statuses(self) -> list[McpServerStatus]:
        """Current state, without touching any server.

        Read-only on purpose: ``atlas mcp list`` must be answerable on a runtime
        whose servers are down, and probing them to print a list would turn a
        listing into a connection attempt.
        """

        rows: list[McpServerStatus] = []
        for config in self.configs:
            connection = self.connections.get(config.name)
            result = self.bridged.get(config.name)
            spec = connection.spec if connection is not None else None
            rows.append(
                McpServerStatus(
                    name=config.name,
                    transport=config.transport,
                    address=config.address(),
                    enabled=config.enabled,
                    connected=connection is not None and not connection.closed,
                    protocol_version="" if spec is None else spec.protocol_version,
                    tools=() if result is None else result.registered,
                    rejected=()
                    if result is None
                    else tuple(item.summary() for item in result.rejected),
                    granted_scopes=config.granted_scopes,
                    error=self.failures.get(config.name),
                )
            )
        return rows

    def inspect(self, name: str) -> dict[str, Any]:
        """Everything known about one server, for ``atlas mcp inspect``."""

        config = next((item for item in self.configs if item.name == name), None)
        if config is None:
            raise McpConnectionError(
                "unknown MCP server",
                details={"server": name, "available": [item.name for item in self.configs]},
            )
        connection = self.connections.get(name)
        result = self.bridged.get(name)
        spec = connection.spec if connection is not None else None
        return {
            "config": config.summary(),
            "connected": connection is not None and not connection.closed,
            "protocol_version": "" if spec is None else spec.protocol_version,
            "server_version": "" if spec is None else spec.server_version,
            "capabilities": [] if spec is None else list(spec.capabilities),
            "offered": []
            if spec is None
            else [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "risk": tool.risk.value,
                    "scopes": list(tool.scopes),
                }
                for tool in spec.tools
            ],
            "bridged": [] if result is None else list(result.registered),
            "rejected": [] if result is None else [item.summary() for item in result.rejected],
            "error": self.failures.get(name),
        }
