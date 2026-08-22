import pytest
from pydantic import BaseModel, ConfigDict

from atlas_harness.kernel.errors import ToolError, ToolNotFoundError, ToolVersionError
from atlas_harness.tools import (
    SCOPE_FS_READ,
    SCOPE_FS_WRITE,
    SCOPE_PROCESS,
    RiskLevel,
    Tool,
    ToolContext,
    ToolManifest,
    ToolRegistry,
    default_registry,
)


class EchoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str


class EchoTool(Tool):
    manifest = ToolManifest(
        name="echo",
        version="1.2.3",
        description="echo the input",
        input_schema=EchoInput.model_json_schema(),
        risk=RiskLevel.READ,
        scopes=(SCOPE_FS_READ,),
    )
    input_model = EchoInput

    async def run(self, args: EchoInput, context: ToolContext) -> str:
        return args.text


class OtherTool(EchoTool):
    manifest = EchoTool.manifest.model_copy(update={"name": "other", "version": "2.0.0"})


class EchoToolV2(EchoTool):
    manifest = EchoTool.manifest.model_copy(update={"version": "9.9.9"})


def test_registry_registers_and_reports_membership() -> None:
    registry = ToolRegistry([EchoTool(), OtherTool()])

    assert len(registry) == 2
    assert "echo" in registry
    assert "missing" not in registry
    assert registry.names() == ["echo", "other"]
    assert [tool.name for tool in registry] == ["echo", "other"]


def test_duplicate_registration_is_refused_unless_replacing() -> None:
    registry = ToolRegistry([EchoTool()])

    with pytest.raises(ToolError) as caught:
        registry.register(EchoToolV2())
    assert caught.value.details["registered_version"] == "1.2.3"
    assert caught.value.details["offered_version"] == "9.9.9"

    registry.register(EchoToolV2(), replace=True)
    assert registry.manifest("echo").version == "9.9.9"


def test_unregister_is_idempotent() -> None:
    registry = ToolRegistry([EchoTool()])

    registry.unregister("echo")
    registry.unregister("echo")
    assert len(registry) == 0


def test_unknown_tool_lists_what_is_available() -> None:
    registry = ToolRegistry([EchoTool()])

    with pytest.raises(ToolNotFoundError) as caught:
        registry.get("nope")
    assert caught.value.details["available"] == ["echo"]


def test_version_pin_must_match_the_registered_version() -> None:
    registry = ToolRegistry([EchoTool()])

    assert registry.get("echo", version="1.2.3").name == "echo"
    with pytest.raises(ToolVersionError) as caught:
        registry.get("echo", version="1.2.4")
    assert caught.value.details["registered_version"] == "1.2.3"


def test_describe_and_required_scopes_span_every_tool() -> None:
    registry = ToolRegistry([EchoTool(), OtherTool()])

    assert [row["name"] for row in registry.describe()] == ["echo", "other"]
    assert registry.required_scopes() == {SCOPE_FS_READ}


def test_default_registry_holds_the_builtin_tools() -> None:
    registry = default_registry()

    assert registry.names() == [
        "compact_context",
        "read_file",
        "run_command",
        "search",
        "write_file",
    ]
    assert registry.required_scopes() == {SCOPE_FS_READ, SCOPE_FS_WRITE, SCOPE_PROCESS}
    assert all(manifest.description for manifest in registry.manifests())
    assert all(manifest.input_schema["type"] == "object" for manifest in registry.manifests())
