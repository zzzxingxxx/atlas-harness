"""Typer CLI for the application shell and event-log inspection."""

import json
import platform
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import typer

from atlas_harness import __version__
from atlas_harness.config import Settings, load_settings
from atlas_harness.events import EventStore, SessionState
from atlas_harness.kernel.errors import AtlasError
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


@contextmanager
def _store(ctx: typer.Context) -> Iterator[EventStore]:
    store = EventStore.from_settings(_settings(ctx))
    try:
        yield store
    finally:
        store.close()


def _emit(payload: dict[str, Any] | list[Any], *, json_output: bool, lines: list[str]) -> None:
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return
    for line in lines:
        typer.echo(line)


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


def _state_summary(state: SessionState) -> dict[str, Any]:
    return {
        "session_id": state.session_id,
        "status": state.status,
        "title": state.title,
        "workspace_root": state.workspace_root,
        "schema_version": state.schema_version,
        "created_at_ms": state.created_at_ms,
        "updated_at_ms": state.updated_at_ms,
        "last_seq": state.last_seq,
        "event_count": state.event_count,
        "lanes": sorted(state.lanes),
        "operations": len(state.operations),
        "open_operations": state.open_operation_ids,
        "pending_approvals": state.pending_approval_ids,
        "messages": len(state.messages),
        "snapshots": len(state.snapshots),
        "state_hash": state.state_hash(),
    }


@app.command()
def sessions(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """List sessions found in the data directory."""

    with _store(ctx) as store:
        summaries = [summary.model_dump(mode="json") for summary in store.list_sessions()]
    lines = [
        f"{row['session_id']}  {row['status']}  seq={row['last_seq']} "
        f"events={row['event_count']}  {row['title'] or ''}".rstrip()
        for row in summaries
    ] or ["no sessions"]
    _emit(summaries, json_output=json_output, lines=lines)


@app.command()
def inspect(
    ctx: typer.Context,
    session_id: str = typer.Argument(..., help="Session id to inspect."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """Project a session from its event log and print a summary."""

    with _store(ctx) as store:
        summary = _state_summary(store.load_state(session_id))
    lines = [f"{key}: {value}" for key, value in summary.items()]
    _emit(summary, json_output=json_output, lines=lines)


@app.command()
def replay(
    ctx: typer.Context,
    session_id: str = typer.Argument(..., help="Session id to replay."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """Rebuild the full session state from the event log without a model."""

    with _store(ctx) as store:
        state = store.load_state(session_id)
    payload = state.model_dump(mode="json")
    payload["state_hash"] = state.state_hash()
    lines = [
        f"replayed {state.event_count} events for {state.session_id}",
        f"last_seq: {state.last_seq}",
        f"state_hash: {payload['state_hash']}",
    ]
    _emit(payload, json_output=json_output, lines=lines)


def main() -> None:
    try:
        app()
    except AtlasError as error:
        typer.echo(json.dumps(error.as_dict(), ensure_ascii=False), err=True)
        raise SystemExit(error.exit_code) from error
