import json

from typer.testing import CliRunner

from atlas_harness.transport.cli import app

runner = CliRunner()


def test_help_is_available() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "AtlasHarness Agent Runtime" in result.stdout
    assert "version" in result.stdout


def test_version_command() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "atlas-harness 0.1.0"


def test_version_json_command() -> None:
    result = runner.invoke(app, ["version", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"name": "atlas-harness", "version": "0.1.0"}
