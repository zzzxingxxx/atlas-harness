from pathlib import Path

import pytest

from atlas_harness.kernel.errors import PolicyDeniedError
from atlas_harness.policy import CommandPolicy, NetworkPolicy
from atlas_harness.policy.engine import PolicyEngine
from atlas_harness.policy.path_policy import PathPolicy
from atlas_harness.tools import SCOPE_NETWORK, PolicyRequest, RiskLevel, ToolManifest


@pytest.mark.parametrize(
    "command",
    [
        "python -c 'print(1)'",
        ["python", "-cprint(1)"],
        ["node", "--eval=console.log(1)"],
        "git status && echo injected",
        ["git", "push", "origin", "main"],
        ["python", "../outside.py"],
        ["rm", "-rf", "."],
    ],
)
def test_command_policy_blocks_injection_and_boundary_escapes(
    command: str | list[str],
) -> None:
    with pytest.raises(PolicyDeniedError):
        CommandPolicy().parse(command)


def test_command_policy_returns_an_argv_for_allowlisted_commands() -> None:
    assert CommandPolicy().parse("git status --short") == ("git", "status", "--short")
    assert CommandPolicy().parse(["python", "script.py", "value"]) == (
        "python",
        "script.py",
        "value",
    )


def test_network_policy_is_closed_by_default() -> None:
    with pytest.raises(PolicyDeniedError) as caught:
        NetworkPolicy().check("https://example.com")

    assert caught.value.details["rule"] == "network_disabled"


def test_network_scope_and_host_allowlist_are_both_required(tmp_path: Path) -> None:
    manifest = ToolManifest(
        name="http_get",
        version="1.0.0",
        description="test network boundary",
        input_schema={"type": "object"},
        risk=RiskLevel.NETWORK,
        scopes=(SCOPE_NETWORK,),
    )
    request = PolicyRequest(urls=("https://api.example.com/data",))
    paths = PathPolicy(tmp_path)

    with pytest.raises(PolicyDeniedError) as caught:
        PolicyEngine(paths=paths).preflight(manifest, request)
    assert caught.value.details["rule"] == "scope_not_granted"

    engine = PolicyEngine(
        paths=paths,
        network=NetworkPolicy(enabled=True, allowed_hosts=("example.com",)),
        granted_scopes=frozenset({SCOPE_NETWORK}),
    )
    decision = engine.preflight(manifest, request)

    assert decision.targets["urls"] == ["https://api.example.com/data"]
