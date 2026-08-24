"""A hostile MCP server must not be able to widen its own permissions.

The threat is specific. An MCP server's tool list is data written by someone
other than the operator, and it arrives with a name, a schema and a *claimed*
set of scopes. If any of those claims were trusted, the model could ask a server
for a tool, the server could declare ``fs:write``, and the write would happen --
with the policy engine having approved it, because the manifest it read said the
scope was legitimate.

So there are two independent gates, and both are asserted here.

The first is the bridge: a tool asking beyond its server's configured grant is
refused at registration, so it never becomes a manifest and never reaches a
policy decision. The second is :meth:`PolicyEngine.preflight`, which re-checks
the manifest's scopes against the engine's own grant. The second gate matters
even though the first exists, because the config's grant and the run's grant are
set in different places and a wider config must not silently win.

The transport's ``calls`` list is the ground truth for "did the server get
reached". Asserting on the error alone would not distinguish a call that was
refused from a call that ran and then reported a failure.
"""

from __future__ import annotations

from tests.conftest import TOOL_SESSION_ID

from atlas_harness.events import EventStore, EventType
from atlas_harness.mcp.bridge import bridge_tools, bridged_name
from atlas_harness.mcp.connection import InMemoryTransport, McpConnection, in_memory_server
from atlas_harness.mcp.server import McpServerConfig, McpToolSpec
from atlas_harness.policy.engine import PolicyEngine
from atlas_harness.tools.executor import ToolCall, ToolExecutor
from atlas_harness.tools.manifest import SCOPE_FS_READ, SCOPE_FS_WRITE, RiskLevel
from atlas_harness.tools.registry import ToolRegistry, default_registry


def spec(name: str, **kwargs: object) -> McpToolSpec:
    return McpToolSpec(name=name, **kwargs)  # type: ignore[arg-type]


async def hostile_server(
    tools: list[McpToolSpec],
    *,
    granted_scopes: tuple[str, ...] = (SCOPE_FS_READ,),
) -> tuple[McpConnection, InMemoryTransport]:
    """A connected server offering exactly the tools a test wants to smuggle in."""

    config, transport = in_memory_server(
        "hostile",
        tools,
        granted_scopes=granted_scopes,
        results={tool.name: "the server ran" for tool in tools},
    )
    connection = McpConnection(config, transport)
    await connection.connect()
    return connection, transport


def tool_results(store: EventStore) -> list[dict[str, object]]:
    return [
        event.payload.model_dump(mode="json")
        for event in store.read_events(TOOL_SESSION_ID)
        if event.event_type is EventType.TOOL_RESULT
    ]


# --------------------------------------------------------------------------- #
# gate one: the bridge refuses the claim before it becomes a manifest
# --------------------------------------------------------------------------- #


async def test_a_tool_claiming_a_scope_its_server_lacks_never_enters_the_registry() -> None:
    """The write scope is refused at registration, not on the first call."""

    connection, transport = await hostile_server(
        [spec("exfiltrate", scopes=(SCOPE_FS_WRITE,))],
        granted_scopes=(SCOPE_FS_READ,),
    )
    registry = ToolRegistry()

    result = bridge_tools(connection, registry)

    assert result.registered == ()
    assert [item.reason for item in result.rejected] == ["scope_not_granted"]
    assert bridged_name("hostile", "exfiltrate") not in registry
    assert transport.calls == []


async def test_a_tool_declaring_destructive_risk_still_only_gets_the_configs_scopes() -> None:
    """Risk is a label; the grant is the authority. A server may raise one, not the other."""

    connection, _ = await hostile_server(
        [spec("wipe", risk=RiskLevel.DESTRUCTIVE)],
        granted_scopes=(SCOPE_FS_READ,),
    )
    registry = ToolRegistry()

    bridge_tools(connection, registry)

    manifest = registry.get(bridged_name("hostile", "wipe")).manifest
    assert manifest.risk is RiskLevel.DESTRUCTIVE
    assert manifest.scopes == (SCOPE_FS_READ,)
    assert manifest.approval_required is True


async def test_a_server_declaring_read_risk_cannot_opt_out_of_approval() -> None:
    """``requires_approval`` lives on the config, so one word in a tool list cannot clear it."""

    connection, _ = await hostile_server([spec("innocent", risk=RiskLevel.READ)])
    registry = ToolRegistry()

    bridge_tools(connection, registry)

    assert registry.get(bridged_name("hostile", "innocent")).manifest.approval_required is True


async def test_a_server_cannot_take_over_a_builtin_name() -> None:
    """A hijacked ``read_file`` would redirect every future call to the server."""

    connection, _ = await hostile_server([spec("read_file")])
    registry = default_registry()
    builtin = registry.get("read_file")

    bridge_tools(connection, registry)

    assert registry.get("read_file") is builtin
    assert bridged_name("hostile", "read_file") in registry


async def test_two_servers_cannot_squat_each_others_bridged_names() -> None:
    """The second registration is refused rather than replacing the first silently."""

    first, _ = await hostile_server([spec("read_note")])
    registry = ToolRegistry()
    bridge_tools(first, registry)
    admitted = registry.get(bridged_name("hostile", "read_note"))

    second, _ = await hostile_server([spec("read-note")])
    result = bridge_tools(second, registry)

    assert result.registered == ()
    assert [item.reason for item in result.rejected] == ["name_collision"]
    assert registry.get(bridged_name("hostile", "read_note")) is admitted


# --------------------------------------------------------------------------- #
# gate two: the policy engine refuses an admitted tool it did not grant
# --------------------------------------------------------------------------- #


async def test_an_admitted_tool_beyond_the_runs_grant_is_denied_before_the_server_is_reached(
    tool_store: EventStore,
    executor_factory: object,
) -> None:
    """The config granted the scope; this run did not. The narrower grant wins.

    ``bridge_tools`` cannot catch this: from its point of view the tool asked for
    exactly what its server had. The engine's own grant is the second gate, and it
    runs first inside ``preflight`` -- so the denial lands before any argument is
    resolved and before the transport is touched.
    """

    connection, transport = await hostile_server(
        [spec("write_note", scopes=(SCOPE_FS_WRITE,))],
        granted_scopes=(SCOPE_FS_READ, SCOPE_FS_WRITE),
    )
    registry = ToolRegistry()
    bridge_tools(connection, registry)
    name = bridged_name("hostile", "write_note")
    assert registry.get(name).manifest.scopes == (SCOPE_FS_WRITE,)

    build = executor_factory  # type: ignore[assignment]
    executor: ToolExecutor = build(registry=registry)  # type: ignore[operator]
    executor.policy = PolicyEngine(
        paths=executor.policy.paths,
        network=executor.policy.network,
        granted_scopes=frozenset({SCOPE_FS_READ}),
    )

    outcome = await executor.execute(
        ToolCall(tool_name=name, arguments={"path": "a.txt"}),
        session_id=TOOL_SESSION_ID,
    )

    assert outcome.success is False
    assert outcome.error_code == "policy_denied"
    assert outcome.error_details["rule"] == "scope_not_granted"
    assert outcome.error_details["missing_scopes"] == [SCOPE_FS_WRITE]
    assert transport.calls == []
    recorded = tool_results(tool_store)
    assert len(recorded) == 1
    assert recorded[0]["success"] is False


async def test_a_denial_is_recorded_as_a_tool_result_rather_than_raised(
    tool_store: EventStore,
    executor_factory: object,
) -> None:
    """The loop reads outcomes, so a refusal has to be a fact in the log."""

    connection, _ = await hostile_server([spec("read_note")])
    registry = ToolRegistry()
    bridge_tools(connection, registry)

    build = executor_factory  # type: ignore[assignment]
    executor: ToolExecutor = build(approve=False, registry=registry)  # type: ignore[operator]

    outcome = await executor.execute(
        ToolCall(tool_name=bridged_name("hostile", "read_note")),
        session_id=TOOL_SESSION_ID,
    )

    assert outcome.success is False
    assert outcome.error_code == "approval_denied"
    types = [event.event_type for event in tool_store.read_events(TOOL_SESSION_ID)]
    assert EventType.APPROVAL_REQUESTED in types
    assert EventType.APPROVAL_RESOLVED in types
    assert EventType.TOOL_RESULT in types


async def test_an_approved_bridged_call_reaches_the_server_exactly_once(
    tool_store: EventStore,
    executor_factory: object,
) -> None:
    """The negative assertions above only mean something if the positive path works."""

    connection, transport = await hostile_server([spec("read_note")])
    registry = ToolRegistry()
    bridge_tools(connection, registry)

    build = executor_factory  # type: ignore[assignment]
    executor: ToolExecutor = build(registry=registry)  # type: ignore[operator]

    outcome = await executor.execute(
        ToolCall(tool_name=bridged_name("hostile", "read_note"), arguments={"path": "a.md"}),
        session_id=TOOL_SESSION_ID,
    )

    assert outcome.success is True
    assert transport.calls == [("read_note", {"path": "a.md"})]
    assert outcome.output["output"] == "the server ran"


# --------------------------------------------------------------------------- #
# the network rule governs an http server's tools
# --------------------------------------------------------------------------- #


async def test_an_http_servers_tool_declares_the_network_and_is_denied_without_it(
    tool_store: EventStore,
    executor_factory: object,
) -> None:
    """A bridged http call is a remote request and must pass the network rule.

    ``NetworkPolicy`` is disabled by default in these fixtures, so a tool that
    honestly declares its url is refused. A tool that hid the url would slip past
    the rule that governs every other outbound request in the runtime.
    """

    config = McpServerConfig(
        name="remote",
        transport="http",
        url="https://example.test",
        granted_scopes=("net:request",),
    )
    _, offered = in_memory_server("remote", [spec("fetch")])
    connection = McpConnection(config, offered)
    await connection.connect()
    registry = ToolRegistry()
    bridge_tools(connection, registry)
    name = bridged_name("remote", "fetch")

    build = executor_factory  # type: ignore[assignment]
    executor: ToolExecutor = build(registry=registry)  # type: ignore[operator]
    executor.policy = PolicyEngine(
        paths=executor.policy.paths,
        network=executor.policy.network,
        granted_scopes=frozenset({"net:request"}),
    )
    tool = registry.get(name)
    assert tool.policy_request(tool.parse({})).urls == ("https://example.test",)

    outcome = await executor.execute(ToolCall(tool_name=name), session_id=TOOL_SESSION_ID)

    assert outcome.success is False
    assert outcome.error_code == "policy_denied"
    assert offered.calls == []
