"""Server declarations: what a server is, and what it is allowed to be.

An MCP server is operator configuration, never model output. The plan forbids
``policy`` from consulting model output to decide access, and a server that could
name its own scopes would be exactly that with one extra hop: the model asks the
server for a tool, the server declares ``fs:write``, the write happens. So the
grant lives here, in a file an operator edits, and the bridge can only narrow it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from atlas_harness.events.models import MCP_TRANSPORTS
from atlas_harness.kernel.errors import ConfigurationError
from atlas_harness.tools.manifest import (
    DEFAULT_SCOPES,
    SCOPE_FS_READ,
    RiskLevel,
)

SERVER_NAME_MAX = 64
MAX_TOOLS_PER_SERVER = 64
"""A server offering hundreds of tools would crowd out the builtins in the
capability slot. The cap is a refusal, not a truncation: silently dropping the
tail would make the registry depend on dict ordering."""

DEFAULT_CONNECT_TIMEOUT_MS = 10_000
DEFAULT_CALL_TIMEOUT_MS = 30_000

MCP_CONFIG_FILENAMES = ("mcp.yaml", "mcp.yml", "mcp.json")
"""Searched in order inside the configured directory."""


class McpToolSpec(BaseModel):
    """One tool as the server describes it.

    This is untrusted input. Every field is treated as a claim: the name is
    normalized, the schema is only passed through to the model, and ``scopes`` is
    a request the bridge intersects against the server's grant rather than a
    grant of its own.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    risk: RiskLevel = RiskLevel.NETWORK
    """Defaults to ``network`` because a remote call is the honest description of
    what invoking an external tool does, and ``network`` requires approval."""

    scopes: tuple[str, ...] = ()
    idempotent: bool = False

    @field_validator("input_schema")
    @classmethod
    def _object_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Force an object schema, since a bridged call always carries a dict."""

        if not value:
            return {"type": "object", "properties": {}}
        return value


class McpServerSpec(BaseModel):
    """What a server said about itself during the handshake."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    protocol_version: str = ""
    server_version: str = ""
    capabilities: tuple[str, ...] = ()
    tools: tuple[McpToolSpec, ...] = ()


class McpServerConfig(BaseModel):
    """One configured server, with the limits that hold it in place.

    Every limit has a default, and the defaults are the restrictive ones. A
    config that only names a command gets no network scope, no write scope, a
    ten-second handshake and a thirty-second call ceiling.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=SERVER_NAME_MAX, pattern=r"^[a-z][a-z0-9_-]*$")
    transport: str = "stdio"
    command: tuple[str, ...] = ()
    """argv for a stdio server. Never passed through a shell."""

    url: str = ""
    """Base URL for an http server."""

    enabled: bool = True
    granted_scopes: tuple[str, ...] = (SCOPE_FS_READ,)
    """The ceiling for every tool this server contributes. A tool asking for more
    is rejected rather than downgraded: a half-granted tool would fail at the
    policy on its first call, which costs an iteration to learn what the config
    already knew."""

    connect_timeout_ms: int = Field(default=DEFAULT_CONNECT_TIMEOUT_MS, gt=0)
    call_timeout_ms: int = Field(default=DEFAULT_CALL_TIMEOUT_MS, gt=0)
    max_tools: int = Field(default=MAX_TOOLS_PER_SERVER, gt=0, le=MAX_TOOLS_PER_SERVER)
    max_output_bytes: int = Field(default=65_536, gt=0)
    max_concurrent_calls: int = Field(default=2, gt=0, le=16)
    """Per server, not global. One slow server must not exhaust the runtime's
    ability to call any other."""

    requires_approval: bool = True
    """External tools ask for approval by default regardless of the risk they
    claim. A server that could declare itself ``read`` would otherwise opt out
    of the gate by writing one word in its own tool list."""

    env: dict[str, str] = Field(default_factory=dict)
    """Passed to a stdio child instead of inheriting this process's environment.
    Credentials reach the child here and never reach an event."""

    env_passthrough: tuple[str, ...] = ()
    """Names -- not values -- of variables the child may inherit. Explicit so a
    server cannot read this runtime's API keys just by existing."""

    @field_validator("transport")
    @classmethod
    def _known_transport(cls, value: str) -> str:
        if value not in MCP_TRANSPORTS:
            expected = sorted(MCP_TRANSPORTS)
            raise ValueError(f"unknown transport {value!r}; expected one of {expected}")
        return value

    @field_validator("granted_scopes")
    @classmethod
    def _known_scopes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        unknown = sorted(set(value) - set(DEFAULT_SCOPES) - {"net:request"})
        if unknown:
            raise ValueError(f"unknown scopes {unknown}")
        return value

    def address(self) -> str:
        """How this server is reached, with nothing secret in it.

        Only the program name is reported for stdio. Later argv entries routinely
        carry tokens (``--api-key``), and this string is written to an event.
        """

        if self.transport == "stdio":
            return self.command[0] if self.command else ""
        return self.url

    def check(self) -> None:
        """Refuse a config that cannot be connected, before anything is started."""

        if self.transport == "stdio" and not self.command:
            raise ConfigurationError(
                "stdio MCP server needs a command",
                details={"server": self.name, "transport": self.transport},
            )
        if self.transport == "http" and not self.url:
            raise ConfigurationError(
                "http MCP server needs a url",
                details={"server": self.name, "transport": self.transport},
            )
        if self.transport == "http" and "net:request" not in self.granted_scopes:
            # An http server is reached over the network by definition. Letting
            # the connection happen and then denying the tool would put the
            # network call before the policy that governs it.
            raise ConfigurationError(
                "http MCP server requires the net:request scope",
                details={"server": self.name, "granted_scopes": list(self.granted_scopes)},
            )

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "transport": self.transport,
            "address": self.address(),
            "enabled": self.enabled,
            "granted_scopes": list(self.granted_scopes),
            "connect_timeout_ms": self.connect_timeout_ms,
            "call_timeout_ms": self.call_timeout_ms,
            "max_tools": self.max_tools,
            "max_concurrent_calls": self.max_concurrent_calls,
            "requires_approval": self.requires_approval,
        }


def _parse(text: str, *, path: Path) -> Any:
    try:
        if path.suffix == ".json":
            return json.loads(text)
        return yaml.safe_load(text)
    except Exception as exc:
        raise ConfigurationError(
            "MCP config file could not be parsed",
            details={"path": str(path), "error": str(exc)},
        ) from exc


def load_server_configs(directory: Path) -> list[McpServerConfig]:
    """Read the server list from the first config file found in ``directory``.

    A missing directory or missing file yields no servers. Not configuring MCP is
    the ordinary case and must not be an error; a *malformed* config is an error,
    because falling back to "no servers" would silently disable tools an operator
    believes are available.
    """

    if not directory.is_dir():
        return []
    for filename in MCP_CONFIG_FILENAMES:
        path = directory / filename
        if not path.is_file():
            continue
        raw = _parse(path.read_text(encoding="utf-8"), path=path)
        return _configs_from(raw, path=path)
    return []


def _configs_from(raw: Any, *, path: Path) -> list[McpServerConfig]:
    entries: list[Any]
    if isinstance(raw, dict):
        servers = raw.get("servers", raw.get("mcpServers"))
        if isinstance(servers, dict):
            entries = [{"name": name, **value} for name, value in servers.items()]
        elif isinstance(servers, list):
            entries = list(servers)
        else:
            raise ConfigurationError(
                "MCP config needs a 'servers' mapping or list",
                details={"path": str(path)},
            )
    elif isinstance(raw, list):
        entries = list(raw)
    elif raw is None:
        return []
    else:
        raise ConfigurationError(
            "MCP config must be a mapping or a list",
            details={"path": str(path), "found": type(raw).__name__},
        )

    configs: list[McpServerConfig] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ConfigurationError(
                "each MCP server entry must be a mapping",
                details={"path": str(path), "found": type(entry).__name__},
            )
        try:
            config = McpServerConfig.model_validate(entry)
        except Exception as exc:
            raise ConfigurationError(
                "invalid MCP server entry",
                details={
                    "path": str(path),
                    "entry": str(entry.get("name", "?")),
                    "error": str(exc),
                },
            ) from exc
        if config.name in seen:
            raise ConfigurationError(
                "duplicate MCP server name",
                details={"path": str(path), "server": config.name},
            )
        seen.add(config.name)
        config.check()
        configs.append(config)
    return configs
