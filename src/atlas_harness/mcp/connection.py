"""Connect to a server, list what it offers, call it, and shut it down.

The transport is a Protocol with an in-memory implementation used by the tests,
so the connection lifecycle -- handshake, capability listing, timeout, close --
is exercised without a child process or a socket. That is deliberate: the
failure modes worth pinning here are *ours* (a server that never answers, one
that answers with junk, one that is closed mid-call), and a real subprocess makes
them slower to provoke and less reliable to reproduce.

Nothing in this module decides whether a call is allowed. It moves bytes and
enforces its own timeouts; the policy runs one layer up, in the bridged tool.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from atlas_harness.kernel.clock import Clock, SystemClock
from atlas_harness.kernel.errors import ToolError, ToolTimeoutError
from atlas_harness.mcp.server import McpServerConfig, McpServerSpec, McpToolSpec


class McpConnectionError(ToolError):
    """A server could not be reached, or broke the protocol.

    A connection failure is a tool error rather than a configuration error: the
    config was fine, the server is not answering, and the run should degrade to
    the tools it does have rather than exit.
    """

    code = "mcp_connection_error"


class McpTransport(Protocol):
    """The three things a transport has to do. Nothing about tools or policy."""

    async def handshake(self, config: McpServerConfig) -> McpServerSpec:
        """Open the channel and return what the server advertises."""
        ...

    async def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        """Invoke one tool and return its raw result."""
        ...

    async def close(self) -> None:
        """Release the channel. Must be safe to call twice."""
        ...


class McpCallResult(BaseModel):
    """One bridged call's outcome, already flattened to text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    server: str
    tool: str
    output: str = ""
    duration_ms: int = 0
    truncated: bool = False


class McpConnection:
    """One live server: its spec, its concurrency limit and its close path."""

    def __init__(
        self,
        config: McpServerConfig,
        transport: McpTransport,
        *,
        clock: Clock | None = None,
    ) -> None:
        self.config = config
        self.transport = transport
        self.clock = clock or SystemClock()
        self.spec: McpServerSpec | None = None
        self.connected_at_ms: int | None = None
        self.closed = False
        self._gate = asyncio.Semaphore(config.max_concurrent_calls)

    @property
    def tools(self) -> tuple[McpToolSpec, ...]:
        return () if self.spec is None else self.spec.tools

    async def connect(self) -> McpServerSpec:
        """Handshake under the configured timeout.

        A server that never answers is the common failure, and without the
        timeout it would hang the whole startup rather than one server's slot.
        """

        try:
            async with asyncio.timeout(self.config.connect_timeout_ms / 1000):
                spec = await self.transport.handshake(self.config)
        except TimeoutError as exc:
            await self._close_quietly()
            raise McpConnectionError(
                "MCP server did not complete the handshake in time",
                details={
                    "server": self.config.name,
                    "reason": "timeout",
                    "connect_timeout_ms": self.config.connect_timeout_ms,
                },
            ) from exc
        except McpConnectionError:
            await self._close_quietly()
            raise
        except Exception as exc:
            await self._close_quietly()
            raise McpConnectionError(
                "MCP server handshake failed",
                details={
                    "server": self.config.name,
                    "reason": "handshake_failed",
                    "error": str(exc),
                },
            ) from exc
        self.spec = spec
        self.connected_at_ms = self.clock.now_ms()
        return spec

    async def call(self, tool: str, arguments: dict[str, Any]) -> McpCallResult:
        """Invoke a tool, bounded by the per-server timeout and output cap."""

        if self.closed:
            raise McpConnectionError(
                "MCP server connection is closed",
                details={"server": self.config.name, "tool": tool, "reason": "transport_error"},
            )
        started = self.clock.now_ms()
        async with self._gate:
            try:
                async with asyncio.timeout(self.config.call_timeout_ms / 1000):
                    raw = await self.transport.call(tool, arguments)
            except TimeoutError as exc:
                raise ToolTimeoutError(
                    "MCP tool call timed out",
                    details={
                        "server": self.config.name,
                        "tool": tool,
                        "timeout_ms": self.config.call_timeout_ms,
                    },
                ) from exc
            except ToolError:
                raise
            except Exception as exc:
                raise McpConnectionError(
                    "MCP tool call failed",
                    details={"server": self.config.name, "tool": tool, "error": str(exc)},
                ) from exc
        text = _as_text(raw)
        truncated = False
        if len(text.encode("utf-8")) > self.config.max_output_bytes:
            text = _clip_bytes(text, self.config.max_output_bytes)
            truncated = True
        return McpCallResult(
            server=self.config.name,
            tool=tool,
            output=text,
            duration_ms=max(0, self.clock.now_ms() - started),
            truncated=truncated,
        )

    async def close(self) -> None:
        """Close once. Idempotent, because both shutdown and failure paths call it."""

        if self.closed:
            return
        self.closed = True
        await self.transport.close()

    async def _close_quietly(self) -> None:
        """Close while already handling a failure, without masking it.

        A transport that fails its handshake often fails its close too, and the
        handshake error is the one worth reporting.
        """

        try:
            await self.close()
        except Exception:  # noqa: BLE001 - the original failure is the interesting one
            self.closed = True

    def duration_ms(self) -> int | None:
        if self.connected_at_ms is None:
            return None
        return max(0, self.clock.now_ms() - self.connected_at_ms)


def _as_text(raw: Any) -> str:
    """Flatten a server's answer into text the model can read.

    MCP results are content lists. Anything else is stringified rather than
    rejected: a server returning a bare string is not a protocol violation worth
    failing a run over.
    """

    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        content = raw.get("content")
        if isinstance(content, list):
            return "\n".join(_as_text(item) for item in content)
        text = raw.get("text")
        if isinstance(text, str):
            return text
        return str(raw)
    if isinstance(raw, list):
        return "\n".join(_as_text(item) for item in raw)
    return str(raw)


def _clip_bytes(text: str, limit: int) -> str:
    encoded = text.encode("utf-8")[:limit]
    return encoded.decode("utf-8", errors="ignore")


class InMemoryTransport:
    """A scripted server. Answers from a dict, and can be told to misbehave.

    Every fault an operator meets in production is reachable from here without a
    process: ``handshake_delay_ms`` past the connect timeout, ``fail_handshake``,
    a tool raising, and a tool sleeping past the call timeout.
    """

    def __init__(
        self,
        spec: McpServerSpec,
        *,
        results: dict[str, Any] | None = None,
        handshake_delay_ms: int = 0,
        call_delay_ms: int = 0,
        fail_handshake: str | None = None,
        fail_call: str | None = None,
    ) -> None:
        self.spec = spec
        self.results = results or {}
        self.handshake_delay_ms = handshake_delay_ms
        self.call_delay_ms = call_delay_ms
        self.fail_handshake = fail_handshake
        self.fail_call = fail_call
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closes = 0

    async def handshake(self, config: McpServerConfig) -> McpServerSpec:
        if self.handshake_delay_ms:
            await asyncio.sleep(self.handshake_delay_ms / 1000)
        if self.fail_handshake:
            raise McpConnectionError(
                self.fail_handshake,
                details={"server": config.name, "reason": "handshake_failed"},
            )
        return self.spec

    async def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((tool, dict(arguments)))
        if self.call_delay_ms:
            await asyncio.sleep(self.call_delay_ms / 1000)
        if self.fail_call:
            raise RuntimeError(self.fail_call)
        return self.results.get(tool, "")

    async def close(self) -> None:
        self.closes += 1


def in_memory_server(
    name: str,
    tools: list[McpToolSpec],
    *,
    results: dict[str, Any] | None = None,
    capabilities: tuple[str, ...] = ("tools",),
    **kwargs: Any,
) -> tuple[McpServerConfig, InMemoryTransport]:
    """A config plus a scripted transport for it, for tests and ``atlas doctor``."""

    scopes = kwargs.pop("granted_scopes", None)
    config = McpServerConfig(
        name=name,
        transport="stdio",
        command=("in-memory",),
        granted_scopes=tuple(scopes) if scopes is not None else ("fs:read",),
        **{key: value for key, value in kwargs.items() if key in McpServerConfig.model_fields},
    )
    spec = McpServerSpec(
        protocol_version="2024-11-05",
        server_version="0.0.0",
        capabilities=capabilities,
        tools=tuple(tools),
    )
    transport_kwargs = {
        key: value
        for key, value in kwargs.items()
        if key in {"handshake_delay_ms", "call_delay_ms", "fail_handshake", "fail_call"}
    }
    return config, InMemoryTransport(spec, results=results, **transport_kwargs)


class StdioTransport:
    """A child process speaking newline-delimited JSON-RPC over stdin/stdout.

    The child gets an explicit environment rather than this process's: an MCP
    server has no business reading ``ATLAS_MODEL_API_KEY``. ``env_passthrough``
    names what may cross, so adding a variable is a config edit and not an
    accident of inheritance.
    """

    def __init__(self, config: McpServerConfig) -> None:
        self.config = config
        self.process: asyncio.subprocess.Process | None = None
        self._next_id = 0

    def _environment(self) -> dict[str, str]:
        env = {name: os.environ[name] for name in self.config.env_passthrough if name in os.environ}
        env.update(self.config.env)
        # A child with no PATH cannot resolve its own helpers, and PATH carries
        # no secret. Everything else has to be named.
        env.setdefault("PATH", os.environ.get("PATH", ""))
        return env

    async def handshake(self, config: McpServerConfig) -> McpServerSpec:
        self.process = await asyncio.create_subprocess_exec(
            *config.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=self._environment(),
        )
        initialized = await self._request("initialize", {"protocolVersion": "2024-11-05"})
        listed = await self._request("tools/list", {})
        return _spec_from_rpc(initialized, listed)

    async def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        return await self._request("tools/call", {"name": tool, "arguments": arguments})

    async def close(self) -> None:
        process = self.process
        self.process = None
        if process is None or process.returncode is not None:
            return
        if process.stdin is not None and not process.stdin.is_closing():
            process.stdin.close()
        try:
            async with asyncio.timeout(2):
                await process.wait()
        except TimeoutError:
            # An MCP server that ignores a closed stdin is not going to be
            # reasoned with. Killing it is the only way not to leak the process.
            process.kill()
            await process.wait()

    async def _request(self, method: str, params: dict[str, Any]) -> Any:
        process = self.process
        if process is None or process.stdin is None or process.stdout is None:
            raise McpConnectionError(
                "MCP server process is not running",
                details={"server": self.config.name, "method": method},
            )
        self._next_id += 1
        frame = json.dumps(
            {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params},
            ensure_ascii=False,
        )
        process.stdin.write(f"{frame}\n".encode())
        await process.stdin.drain()
        line = await process.stdout.readline()
        if not line:
            raise McpConnectionError(
                "MCP server closed the stream",
                details={
                    "server": self.config.name,
                    "method": method,
                    "reason": "transport_error",
                },
            )
        try:
            message = json.loads(line.decode("utf-8"))
        except Exception as exc:
            raise McpConnectionError(
                "MCP server sent a frame that is not JSON",
                details={"server": self.config.name, "method": method},
            ) from exc
        if isinstance(message, dict) and "error" in message:
            error = message["error"]
            raise McpConnectionError(
                "MCP server returned an error",
                details={"server": self.config.name, "method": method, "error": str(error)},
            )
        if isinstance(message, dict):
            return message.get("result")
        return message


def _spec_from_rpc(initialized: Any, listed: Any) -> McpServerSpec:
    """Build a spec from two RPC results, tolerating a sparse server.

    Missing fields are defaults rather than errors. A server that answers
    ``tools/list`` and nothing else is usable, and refusing it would trade a
    working integration for protocol pedantry.
    """

    protocol = ""
    server_version = ""
    capabilities: list[str] = []
    if isinstance(initialized, dict):
        protocol = str(initialized.get("protocolVersion") or "")
        info = initialized.get("serverInfo")
        if isinstance(info, dict):
            server_version = str(info.get("version") or "")
        advertised = initialized.get("capabilities")
        if isinstance(advertised, dict):
            capabilities = sorted(str(key) for key in advertised)
    tools: list[McpToolSpec] = []
    rows = listed.get("tools") if isinstance(listed, dict) else listed
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            tools.append(
                McpToolSpec(
                    name=str(row.get("name") or ""),
                    description=str(row.get("description") or ""),
                    input_schema=row.get("inputSchema") or row.get("input_schema") or {},
                )
            )
    return McpServerSpec(
        protocol_version=protocol,
        server_version=server_version,
        capabilities=tuple(capabilities),
        tools=tuple(tools),
    )
