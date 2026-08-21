from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from atlas_harness.kernel.errors import PolicyDeniedError, ToolInputError
from atlas_harness.tools import (
    SCOPE_FS_READ,
    PolicyRequest,
    RiskLevel,
    Tool,
    ToolContext,
    ToolManifest,
    json_schema_for,
)


class SampleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str


def manifest(**overrides: object) -> ToolManifest:
    base: dict[str, object] = {
        "name": "sample",
        "version": "1.2.3",
        "description": "sample tool",
        "input_schema": json_schema_for(SampleInput),
        "risk": RiskLevel.READ,
        "scopes": (SCOPE_FS_READ,),
    }
    base.update(overrides)
    return ToolManifest(**base)  # type: ignore[arg-type]


class SampleTool(Tool):
    manifest = manifest()
    input_model = SampleInput

    async def run(self, args: SampleInput, context: ToolContext) -> str:
        return args.path


def test_manifest_reference_and_describe() -> None:
    described = manifest().describe()

    assert manifest().reference() == "sample@1.2.3"
    assert described["requires_approval"] is False
    assert described["parallel_safe"] is True
    assert described["scopes"] == [SCOPE_FS_READ]
    assert described["input_schema"]["properties"]["path"]["type"] == "string"


@pytest.mark.parametrize(
    ("risk", "approval", "parallel"),
    [
        (RiskLevel.READ, False, True),
        (RiskLevel.WRITE, True, False),
        (RiskLevel.NETWORK, True, False),
        (RiskLevel.DESTRUCTIVE, True, False),
    ],
)
def test_risk_drives_approval_and_parallelism(
    risk: RiskLevel, approval: bool, parallel: bool
) -> None:
    entry = manifest(risk=risk)

    assert entry.approval_required is approval
    assert entry.can_run_in_parallel is parallel


def test_explicit_flags_override_risk_defaults() -> None:
    entry = manifest(risk=RiskLevel.READ, requires_approval=True, parallel_safe=False)

    assert entry.approval_required is True
    assert entry.can_run_in_parallel is False


@pytest.mark.parametrize("version", ["1.0", "v1.0.0", "1.0.0-rc1", ""])
def test_version_must_be_semver_triple(version: str) -> None:
    with pytest.raises(ValidationError):
        manifest(version=version)


@pytest.mark.parametrize("name", ["Sample", "1sample", "sample-tool", "sample tool", ""])
def test_name_must_be_a_lowercase_identifier(name: str) -> None:
    with pytest.raises(ValidationError):
        manifest(name=name)


def test_manifest_is_frozen_and_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        manifest(unexpected=True)
    with pytest.raises(ValidationError):
        manifest().name = "other"


def test_policy_request_summary_renders_both_command_shapes() -> None:
    request = PolicyRequest(
        reads=("a.txt",),
        dirs=("src",),
        writes=("b.txt",),
        commands=("git status", ("git", "log")),
        urls=("https://example.com",),
    )

    assert request.summary() == {
        "reads": ["a.txt"],
        "dirs": ["src"],
        "writes": ["b.txt"],
        "commands": ["git status", ["git", "log"]],
        "urls": ["https://example.com"],
    }


def test_parse_wraps_pydantic_errors_in_a_domain_error() -> None:
    with pytest.raises(ToolInputError) as caught:
        SampleTool().parse({"unknown": 1})

    assert caught.value.details["tool"] == "sample"


def test_default_policy_request_and_preview_are_empty(workspace: Path) -> None:
    tool = SampleTool()
    context = ToolContext(workspace_root=workspace, session_id="ses_x", call_id="call_x")

    assert tool.policy_request(SampleInput(path="a")) == PolicyRequest()
    assert tool.preview(SampleInput(path="a"), context) is None
    assert tool.name == "sample"


def test_path_for_refuses_an_undeclared_target(workspace: Path) -> None:
    context = ToolContext(
        workspace_root=workspace,
        session_id="ses_x",
        call_id="call_x",
        resolved_paths={"a.txt": str(workspace / "a.txt")},
    )

    assert context.path_for("a.txt") == workspace / "a.txt"
    with pytest.raises(PolicyDeniedError) as caught:
        context.path_for("b.txt")
    assert caught.value.details["rule"] == "path_not_declared"


def test_relative_falls_back_to_the_full_path_outside_the_workspace(workspace: Path) -> None:
    context = ToolContext(workspace_root=workspace, session_id="ses_x", call_id="call_x")

    assert context.relative(workspace / "src" / "a.txt") == "src/a.txt"
    assert context.relative(Path("/elsewhere/a.txt")) == "/elsewhere/a.txt"
