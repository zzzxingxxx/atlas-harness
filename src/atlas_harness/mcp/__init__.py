"""MCP: external tool servers, admitted through the same boundary as builtins.

The whole point of this package is that it adds no second path. An MCP server
describes tools; this package turns those descriptions into ordinary
:class:`~atlas_harness.tools.manifest.ToolManifest` values and registers them in
the one registry, so every external call goes through the same policy preflight,
approval gate, timeout, truncation and redaction that a builtin does.

Nothing here decides whether a call is allowed. That decision stays in
:mod:`atlas_harness.policy`, which is why a malicious server cannot widen its own
reach: the manifest it gets is the one this package wrote, not the one it asked
for.
"""

from atlas_harness.mcp.bridge import (
    McpBridgeResult,
    McpTool,
    McpToolRejected,
    bridge_tools,
    bridged_name,
    unbridge_tools,
)
from atlas_harness.mcp.connection import (
    InMemoryTransport,
    McpCallResult,
    McpConnection,
    McpConnectionError,
    McpTransport,
    StdioTransport,
    in_memory_server,
)
from atlas_harness.mcp.manager import McpManager, McpServerStatus
from atlas_harness.mcp.server import (
    McpServerConfig,
    McpServerSpec,
    McpToolSpec,
    load_server_configs,
)

__all__ = [
    "InMemoryTransport",
    "McpBridgeResult",
    "McpCallResult",
    "McpConnection",
    "McpConnectionError",
    "McpManager",
    "McpServerConfig",
    "McpServerSpec",
    "McpServerStatus",
    "McpTool",
    "McpToolRejected",
    "McpToolSpec",
    "McpTransport",
    "StdioTransport",
    "bridge_tools",
    "bridged_name",
    "in_memory_server",
    "load_server_configs",
    "unbridge_tools",
]
