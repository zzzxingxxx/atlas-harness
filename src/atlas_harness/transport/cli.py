"""Typer CLI for the M0 application shell."""

import json
import platform
from typing import Any

import typer

from atlas_harness import __version__
from atlas_harness.config import Settings, load_settings
from atlas_harness.observability.logging import configure_logging

app = typer.Typer(
    name="atlas",
    help="AtlasHarness Agent Runtime command line interface.",
    no_args_is_help=True,
    add_completion=False,
)


def _settings(ctx: typer.Context) -> Settings:
    settings = ctx.ensure_object(dict).get("settings")
    if not isinstance(settings, Settings):
        settings = load_settings()
    return settings


@app.callback()
def callback(
    ctx: typer.Context,
    log_level: str | None = typer.Option(None, "--log-level", help="Override ATLAS_LOG_LEVEL."),
    json_logs: bool | None = typer.Option(None, "--json-logs", help="Emit JSON logs."),
) -> None:
    """Initialize validated settings and process logging."""

    settings = load_settings()
    if log_level is not None:
        settings.log_level = log_level
    if json_logs is not None:
        settings.json_logs = json_logs
    configure_logging(settings.log_level, json_logs=settings.json_logs)
    ctx.ensure_object(dict)["settings"] = settings


@app.command()
def version(
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """Print the installed AtlasHarness version."""

    payload = {"name": "atlas-harness", "version": __version__}
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False))
    else:
        typer.echo(f"atlas-harness {__version__}")


@app.command()
def doctor(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """Validate the M0 configuration without creating runtime state."""

    settings = _settings(ctx)
    payload: dict[str, Any] = {
        "status": "ok",
        "python": platform.python_version(),
        "workspace_root": str(settings.resolved_workspace_root()),
        "data_dir": str(settings.resolved_data_dir()),
        "log_level": settings.log_level.upper(),
    }
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False))
    else:
        typer.echo("AtlasHarness configuration: ok")
        for key, value in payload.items():
            if key != "status":
                typer.echo(f"{key}: {value}")


def main() -> None:
    app()
