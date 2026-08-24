"""Connect, bridge, list and shut down MCP servers.

The tests use :class:`InMemoryTransport` rather than a child process because the
failure modes worth pinning are this runtime's, not a real server's: a handshake
that never answers, a tool list that lies about its scopes, a call that outlives
its timeout. All three are one keyword argument away in memory and slow and flaky
out of a subprocess.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.conftest import DEMO_SESSION_ID, seed_session

from atlas_harness.events import EventStore, EventType
from atlas_harness.kernel.errors import ConfigurationError
from atlas_harness.mcp.bridge import bridge_tools, bridged_name, unbridge_tools
from atlas_harness.mcp.connection import (
    InMemoryTransport,
    McpConnection,
    McpConnectionError,
    in_memory_server,
)
from atlas_harness.mcp.manager import McpManager, default_transport
from atlas_harness.mcp.server import McpServerConfig, McpToolSpec, load_server_configs
from atlas_harness.tools.manifest import RiskLevel
from atlas_harness.tools.registry import ToolRegistry


def spec(name: str, **kwargs: object) -> McpToolSpec:
    return McpToolSpec(name=name, **kwargs)  # type: ignore[arg-type]


async def connected(
    name: str = "files",
    tools: list[McpToolSpec] | None = None,
    **kwargs: object,
) -> McpConnection:
    config, transport = in_memory_server(name, tools or [spec("read_note")], **kwargs)
    connection = McpConnection(config, transport)
    await connection.connect()
    return connection


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #


def test_server_config_defaults_to_the_restrictive_grant() -> None:
    config = McpServerConfig(name="files", command=("server",))

    assert config.granted_scopes == ("fs:read",)
    assert config.requires_approval is True
    assert config.enabled is True


def test_stdio_address_hides_argv_after_the_program() -> None:
    """argv routinely carries a token, and the address is written to an event."""

    config = McpServerConfig(name="files", command=("server", "--api-key", "sk-secret"))

    assert config.address() == "server"
    assert "sk-secret" not in str(config.summary())


def test_http_server_without_the_network_scope_is_refused() -> None:
    config = McpServerConfig(name="api", transport="http", url="https://example.test")

    with pytest.raises(ConfigurationError) as caught:
        config.check()
    assert "net:request" in caught.value.message


def test_stdio_server_without_a_command_is_refused() -> None:
    with pytest.raises(ConfigurationError):
        McpServerConfig(name="files").check()


def test_unknown_scope_is_rejected_at_validation() -> None:
    with pytest.raises(ValueError, match="unknown scopes"):
        McpServerConfig(name="files", command=("s",), granted_scopes=("root:all",))


def test_missing_config_directory_is_not_an_error(tmp_path: Path) -> None:
    assert load_server_configs(tmp_path / "absent") == []


def test_config_file_is_read_from_a_servers_mapping(tmp_path: Path) -> None:
    (tmp_path / "mcp.yaml").write_text(
        "servers:\n  files:\n    command: [note-server]\n    granted_scopes: [fs:read]\n",
        encoding="utf-8",
    )

    configs = load_server_configs(tmp_path)

    assert [config.name for config in configs] == ["files"]
    assert configs[0].command == ("note-server",)


def test_malformed_config_raises_rather_than_reporting_no_servers(tmp_path: Path) -> None:
    """Falling back to "no servers" would silently disable tools an operator has."""

    (tmp_path / "mcp.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(ConfigurationError):
        load_server_configs(tmp_path)


def test_duplicate_server_names_are_refused(tmp_path: Path) -> None:
    (tmp_path / "mcp.yaml").write_text(
        "servers:\n- {name: files, command: [a]}\n- {name: files, command: [b]}\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError) as caught:
        load_server_configs(tmp_path)
    assert caught.value.details["server"] == "files"


# --------------------------------------------------------------------------- #
# connection lifecycle
# --------------------------------------------------------------------------- #


async def test_handshake_records_the_advertised_spec() -> None:
    connection = await connected(tools=[spec("read_note"), spec("list_notes")])

    assert connection.spec is not None
    assert connection.spec.protocol_version == "2024-11-05"
    assert [tool.name for tool in connection.tools] == ["read_note", "list_notes"]


async def test_handshake_past_the_connect_timeout_closes_the_transport() -> None:
    """A server that never answers must cost its own slot, not the startup."""

    config, transport = in_memory_server(
        "slow", [spec("read_note")], handshake_delay_ms=50, connect_timeout_ms=10
    )
    connection = McpConnection(config, transport)

    with pytest.raises(McpConnectionError) as caught:
        await connection.connect()
    assert caught.value.details["reason"] == "timeout"
    assert transport.closes == 1


async def test_failed_handshake_closes_the_transport() -> None:
    config, transport = in_memory_server("broken", [], fail_handshake="no")
    connection = McpConnection(config, transport)

    with pytest.raises(McpConnectionError):
        await connection.connect()
    assert transport.closes == 1


async def test_call_flattens_a_content_list() -> None:
    connection = await connected(
        results={"read_note": {"content": [{"text": "one"}, {"text": "two"}]}}
    )

    result = await connection.call("read_note", {"path": "a.md"})

    assert result.output == "one\ntwo"
    assert result.truncated is False


async def test_call_output_is_clipped_at_the_configured_ceiling() -> None:
    connection = await connected(results={"read_note": "x" * 200}, max_output_bytes=16)

    result = await connection.call("read_note", {})

    assert result.truncated is True
    assert len(result.output.encode("utf-8")) <= 16


async def test_call_past_the_call_timeout_is_a_tool_timeout() -> None:
    from atlas_harness.kernel.errors import ToolTimeoutError

    connection = await connected(call_delay_ms=50, call_timeout_ms=10)

    with pytest.raises(ToolTimeoutError):
        await connection.call("read_note", {})


async def test_call_on_a_closed_connection_is_refused() -> None:
    connection = await connected()
    await connection.close()

    with pytest.raises(McpConnectionError) as caught:
        await connection.call("read_note", {})
    assert caught.value.details["reason"] == "transport_error"


async def test_close_is_idempotent() -> None:
    """Both the shutdown path and the failure path close, and may overlap."""

    connection = await connected()
    transport = connection.transport
    assert isinstance(transport, InMemoryTransport)

    await connection.close()
    await connection.close()

    assert transport.closes == 1


def test_unimplemented_transport_is_refused_rather_than_downgraded() -> None:
    config = McpServerConfig(
        name="api",
        transport="http",
        url="https://example.test",
        granted_scopes=("net:request",),
    )

    with pytest.raises(McpConnectionError) as caught:
        default_transport(config)
    assert caught.value.details["reason"] == "transport_error"


# --------------------------------------------------------------------------- #
# bridging
# --------------------------------------------------------------------------- #


def test_bridged_name_normalizes_both_halves() -> None:
    assert bridged_name("files-ro", "readFile") == "mcp_files_ro_readfile"


async def test_bridged_tool_carries_the_servers_schema_and_needs_approval() -> None:
    schema = {"type": "object", "properties": {"path": {"type": "string"}}}
    connection = await connected(tools=[spec("read_note", input_schema=schema)])
    registry = ToolRegistry()

    result = bridge_tools(connection, registry)

    assert result.registered == ("mcp_files_read_note",)
    manifest = registry.get("mcp_files_read_note").manifest
    assert manifest.input_schema == schema
    assert manifest.approval_required is True
    assert manifest.risk is RiskLevel.NETWORK
    assert manifest.can_run_in_parallel is False


async def test_a_tool_declaring_nothing_inherits_the_configs_grant() -> None:
    connection = await connected(granted_scopes=("fs:read", "fs:write"))
    registry = ToolRegistry()

    bridge_tools(connection, registry)

    manifest = registry.get("mcp_files_read_note").manifest
    assert set(manifest.scopes) == {"fs:read", "fs:write"}


async def test_a_tool_declaring_less_narrows_the_grant() -> None:
    connection = await connected(
        tools=[spec("read_note", scopes=("fs:read",))],
        granted_scopes=("fs:read", "fs:write"),
    )
    registry = ToolRegistry()

    bridge_tools(connection, registry)

    assert registry.get("mcp_files_read_note").manifest.scopes == ("fs:read",)


async def test_a_tool_asking_beyond_the_grant_is_refused_at_registration() -> None:
    """The lie is caught before the tool exists, not on its first call."""

    connection = await connected(
        tools=[spec("write_note", scopes=("fs:write",))],
        granted_scopes=("fs:read",),
    )
    registry = ToolRegistry()

    result = bridge_tools(connection, registry)

    assert result.registered == ()
    assert [item.reason for item in result.rejected] == ["scope_not_granted"]
    assert "mcp_files_write_note" not in registry


async def test_a_tool_that_cannot_be_named_is_refused() -> None:
    connection = await connected(tools=[spec("!!!")])

    result = bridge_tools(connection, ToolRegistry())

    assert [item.reason for item in result.rejected] == ["invalid_name"]


async def test_two_tools_normalizing_to_one_name_keep_the_first() -> None:
    connection = await connected(tools=[spec("read-note"), spec("read_note")])
    registry = ToolRegistry()

    result = bridge_tools(connection, registry)

    assert result.registered == ("mcp_files_read_note",)
    assert [item.reason for item in result.rejected] == ["duplicate"]


async def test_a_server_cannot_shadow_a_builtin() -> None:
    """The prefix is the mechanism: a bridged name can never be ``read_file``."""

    from atlas_harness.tools import default_registry

    connection = await connected(name="files", tools=[spec("read_file")])
    registry = default_registry()

    result = bridge_tools(connection, registry)

    assert result.registered == ("mcp_files_read_file",)
    assert registry.get("read_file").manifest.name == "read_file"
    unbridge_tools(registry, result.registered)
    assert "mcp_files_read_file" not in registry


async def test_max_tools_refuses_the_tail_rather_than_truncating() -> None:
    connection = await connected(
        tools=[spec(f"tool_{index}") for index in range(4)],
        max_tools=2,
    )

    result = bridge_tools(connection, ToolRegistry())

    assert len(result.registered) == 2
    assert [item.reason for item in result.rejected] == ["too_many_tools"] * 2


async def test_a_name_collision_refuses_the_tool_without_replacing_it() -> None:
    connection = await connected()
    registry = ToolRegistry()
    bridge_tools(connection, registry)

    again = bridge_tools(connection, registry)

    assert again.registered == ()
    assert [item.reason for item in again.rejected] == ["name_collision"]


async def test_stdio_tool_requests_no_url_and_http_tool_does() -> None:
    """A bridged call over http is a network request and must say so."""

    stdio = await connected()
    registry = ToolRegistry()
    bridge_tools(stdio, registry)
    tool = registry.get("mcp_files_read_note")
    assert tool.policy_request(tool.parse({})).urls == ()

    config = McpServerConfig(
        name="api",
        transport="http",
        url="https://example.test",
        granted_scopes=("net:request",),
    )
    http = McpConnection(config, InMemoryTransport(stdio.spec))  # type: ignore[arg-type]
    await http.connect()
    http_registry = ToolRegistry()
    bridge_tools(http, http_registry)
    http_tool = http_registry.get("mcp_api_read_note")
    assert http_tool.policy_request(http_tool.parse({})).urls == ("https://example.test",)


# --------------------------------------------------------------------------- #
# manager
# --------------------------------------------------------------------------- #


async def test_connect_writes_connected_and_registered_events(store: EventStore) -> None:
    seed_session(store)
    config, transport = in_memory_server("files", [spec("read_note")])
    manager = McpManager(store, ToolRegistry(), configs=(config,))

    status = await manager.connect(DEMO_SESSION_ID, config, transport=transport)

    assert status.connected is True
    assert status.tools == ("mcp_files_read_note",)
    types = [event.event_type for event in store.read_events(DEMO_SESSION_ID)]
    assert EventType.MCP_SERVER_CONNECTED in types
    assert EventType.MCP_TOOLS_REGISTERED in types


async def test_a_failed_connect_is_recorded_as_a_disconnect(store: EventStore) -> None:
    """A server configured and never reached is a fact the log has to carry."""

    seed_session(store)
    config, transport = in_memory_server("broken", [], fail_handshake="refused")
    manager = McpManager(store, ToolRegistry(), configs=(config,))

    status = await manager.connect(DEMO_SESSION_ID, config, transport=transport)

    assert status.connected is False
    assert status.error is not None
    disconnects = [
        event
        for event in store.read_events(DEMO_SESSION_ID)
        if event.event_type is EventType.MCP_SERVER_DISCONNECTED
    ]
    assert len(disconnects) == 1
    assert disconnects[0].payload.model_dump()["reason"] == "handshake_failed"


async def test_one_unreachable_server_does_not_stop_the_others(store: EventStore) -> None:
    seed_session(store)
    good, good_transport = in_memory_server("files", [spec("read_note")])
    bad, bad_transport = in_memory_server("broken", [], fail_handshake="refused")
    manager = McpManager(store, ToolRegistry(), configs=(bad, good))

    statuses = await manager.connect_all(
        DEMO_SESSION_ID,
        transports={"files": good_transport, "broken": bad_transport},
    )

    assert [status.connected for status in statuses] == [False, True]


async def test_a_disabled_server_is_listed_but_never_contacted(store: EventStore) -> None:
    seed_session(store)
    config, transport = in_memory_server("files", [spec("read_note")], enabled=False)
    manager = McpManager(store, ToolRegistry(), configs=(config,))

    statuses = await manager.connect_all(DEMO_SESSION_ID, transports={"files": transport})

    assert statuses[0].enabled is False
    assert statuses[0].connected is False
    assert transport.calls == []
    assert not [
        event
        for event in store.read_events(DEMO_SESSION_ID)
        if event.event_type is EventType.MCP_SERVER_CONNECTED
    ]


async def test_shutdown_withdraws_the_tools_before_writing_the_event(store: EventStore) -> None:
    """No window may exist in which the log says gone and the tool still runs."""

    seed_session(store)
    config, transport = in_memory_server("files", [spec("read_note")])
    registry = ToolRegistry()
    manager = McpManager(store, registry, configs=(config,))
    await manager.connect(DEMO_SESSION_ID, config, transport=transport)
    assert "mcp_files_read_note" in registry

    closed = await manager.shutdown(DEMO_SESSION_ID)

    assert closed == ["files"]
    assert "mcp_files_read_note" not in registry
    assert transport.closes == 1
    assert manager.connections == {}


async def test_shutdown_twice_is_harmless(store: EventStore) -> None:
    seed_session(store)
    config, transport = in_memory_server("files", [spec("read_note")])
    manager = McpManager(store, ToolRegistry(), configs=(config,))
    await manager.connect(DEMO_SESSION_ID, config, transport=transport)

    await manager.shutdown(DEMO_SESSION_ID)
    assert await manager.shutdown(DEMO_SESSION_ID) == []


async def test_statuses_does_not_probe_a_server(store: EventStore) -> None:
    """``atlas mcp list`` must answer on a runtime whose servers are all down."""

    config, transport = in_memory_server("files", [spec("read_note")])
    manager = McpManager(store, ToolRegistry(), configs=(config,))

    rows = manager.statuses()

    assert [row.name for row in rows] == ["files"]
    assert rows[0].connected is False
    assert transport.closes == 0
    assert store.list_session_ids() == []


async def test_inspect_reports_offered_and_bridged_separately(store: EventStore) -> None:
    seed_session(store)
    config, transport = in_memory_server(
        "files",
        [spec("read_note"), spec("write_note", scopes=("fs:write",))],
    )
    manager = McpManager(store, ToolRegistry(), configs=(config,))
    await manager.connect(DEMO_SESSION_ID, config, transport=transport)

    report = manager.inspect("files")

    assert [item["name"] for item in report["offered"]] == ["read_note", "write_note"]
    assert report["bridged"] == ["mcp_files_read_note"]
    assert [item["reason"] for item in report["rejected"]] == ["scope_not_granted"]


async def test_inspect_names_the_available_servers_for_an_unknown_one(store: EventStore) -> None:
    config, _ = in_memory_server("files", [spec("read_note")])
    manager = McpManager(store, ToolRegistry(), configs=(config,))

    with pytest.raises(McpConnectionError) as caught:
        manager.inspect("absent")
    assert caught.value.details["available"] == ["files"]


async def test_mcp_events_project_onto_the_session(store: EventStore) -> None:
    seed_session(store)
    config, transport = in_memory_server("files", [spec("read_note")])
    manager = McpManager(store, ToolRegistry(), configs=(config,))
    await manager.connect(DEMO_SESSION_ID, config, transport=transport)

    state = store.load_state(DEMO_SESSION_ID)
    assert state.mcp_servers["files"] == "connected"
    assert state.mcp_tools["files"] == ["mcp_files_read_note"]
    assert state.connected_mcp_servers == ["files"]

    await manager.shutdown(DEMO_SESSION_ID)

    after = store.load_state(DEMO_SESSION_ID)
    assert after.mcp_servers["files"] == "shutdown"
    assert after.connected_mcp_servers == []
