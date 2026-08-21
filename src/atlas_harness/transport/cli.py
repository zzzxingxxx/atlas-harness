"""Typer CLI for the application shell and event-log inspection."""

import asyncio
import json
import platform
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import typer

from atlas_harness import __version__
from atlas_harness.config import Settings, load_settings
from atlas_harness.events import EventStore, EventType, SessionState
from atlas_harness.kernel.errors import (
    ApprovalDeniedError,
    AtlasError,
    PolicyDeniedError,
    ToolError,
    ToolInputError,
    ToolNotFoundError,
    ToolTimeoutError,
    ToolVersionError,
)
from atlas_harness.observability.logging import configure_logging
from atlas_harness.policy import ApprovalGate, ApprovalMode, FixedApprovalGate, PolicyEngine
from atlas_harness.tools import ToolContext, default_registry, redact, truncate_text
from atlas_harness.tools.executor import ToolCall, ToolExecutor

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


_TOOL_EXIT_CODES: dict[str, int] = {
    cls.code: cls.exit_code
    for cls in (
        PolicyDeniedError,
        ApprovalDeniedError,
        ToolError,
        ToolNotFoundError,
        ToolInputError,
        ToolTimeoutError,
        ToolVersionError,
    )
}


def _json_arguments(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ToolInputError(
            "--args is not valid JSON",
            details={"error": str(exc)},
        ) from exc
    if not isinstance(parsed, dict):
        raise ToolInputError(
            "--args must be a JSON object", details={"received": type(parsed).__name__}
        )
    return parsed


def _gate(mode: ApprovalMode, approve: bool) -> ApprovalGate:
    if mode is ApprovalMode.NEVER:
        return FixedApprovalGate(False, reason="approval mode is never", approver="cli")
    if mode is ApprovalMode.AUTO:
        return FixedApprovalGate(True, reason="approval mode is auto", approver="cli")
    if approve:
        return FixedApprovalGate(True, reason="approved with --yes", approver="cli")
    reason = "approval required; re-run with --yes"
    return FixedApprovalGate(False, reason=reason, approver="cli")


@app.command()
def tools(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """List the registered tools and their declared contract."""

    payload = default_registry().describe()
    lines = [
        f"{row['name']}@{row['version']}  risk={row['risk']}  "
        f"approval={'yes' if row['requires_approval'] else 'no'}  "
        f"parallel={'yes' if row['parallel_safe'] else 'no'}  "
        f"scopes={','.join(row['scopes']) or '-'}"
        for row in payload
    ]
    _emit(payload, json_output=json_output, lines=lines)


@app.command(name="tool-check")
def tool_check(
    ctx: typer.Context,
    tool_name: str = typer.Argument(..., help="Registered tool name."),
    args: str = typer.Option("{}", "--args", help="Tool arguments as a JSON object."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """Validate arguments and run the policy preflight without executing."""

    settings = _settings(ctx)
    tool = default_registry().get(tool_name)
    parsed = tool.parse(_json_arguments(args))
    decision = PolicyEngine.from_settings(settings).preflight(
        tool.manifest, tool.policy_request(parsed)
    )
    context = ToolContext(
        workspace_root=settings.resolved_workspace_root(),
        session_id="ses_preflight_0000",
        call_id="call_preflight",
        max_output_bytes=settings.max_tool_output_bytes,
        max_read_bytes=settings.max_read_bytes,
        timeout_ms=tool.manifest.timeout_ms,
        resolved_paths=decision.resolved_paths,
        vetted_commands=decision.vetted_commands,
    )
    raw_preview = tool.preview(parsed, context)
    preview = (
        None
        if raw_preview is None
        else truncate_text(redact(raw_preview), settings.max_tool_output_bytes)[0]
    )
    payload: dict[str, Any] = {
        "tool": tool.manifest.reference(),
        "risk": decision.risk.value,
        "requires_approval": decision.requires_approval,
        "reason": decision.reason,
        "targets": decision.targets,
        "preview": preview,
    }
    lines = [
        f"{payload['tool']} would be allowed",
        f"risk: {payload['risk']}",
        f"approval: {'required' if decision.requires_approval else 'not required'}",
        f"reason: {decision.reason}",
    ]
    if payload["preview"]:
        lines.append(str(payload["preview"]))
    _emit(payload, json_output=json_output, lines=lines)


@app.command(name="tool-run")
def tool_run(
    ctx: typer.Context,
    tool_name: str = typer.Argument(..., help="Registered tool name."),
    args: str = typer.Option("{}", "--args", help="Tool arguments as a JSON object."),
    session_id: str | None = typer.Option(None, "--session", help="Existing session id."),
    approve: bool = typer.Option(False, "--yes", help="Approve calls that need approval."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """Execute one tool call under the policy boundary and record its events."""

    settings = _settings(ctx)
    parsed = _json_arguments(args)
    with _store(ctx) as store:
        target = session_id or store.new_session_id()
        if not store.session_exists(target):
            store.append_new(
                EventType.SESSION_CREATED,
                session_id=target,
                payload={
                    "title": f"cli tool-run {tool_name}",
                    "workspace_root": str(settings.resolved_workspace_root()),
                },
            )
        executor = ToolExecutor(
            registry=default_registry(),
            policy=PolicyEngine.from_settings(settings),
            store=store,
            approvals=_gate(ApprovalMode(settings.approval_mode), approve),
            max_output_bytes=settings.max_tool_output_bytes,
        )
        outcome = asyncio.run(
            executor.execute(
                ToolCall(tool_name=tool_name, arguments=parsed),
                session_id=target,
            )
        )
    payload = outcome.model_dump(mode="json")
    payload["session_id"] = target
    lines = [
        f"{outcome.tool_name} {'succeeded' if outcome.success else 'failed'} "
        f"in {outcome.duration_ms}ms",
        f"session: {target}",
        f"call: {outcome.call_id}",
    ]
    if outcome.success:
        lines.append(json.dumps(outcome.output, ensure_ascii=False, indent=2))
    else:
        lines.append(f"error: {outcome.error_code}: {outcome.error}")
    _emit(payload, json_output=json_output, lines=lines)
    if not outcome.success:
        raise typer.Exit(code=_TOOL_EXIT_CODES.get(outcome.error_code or "", ToolError.exit_code))


def main() -> None:
    try:
        app()
    except AtlasError as error:
        typer.echo(json.dumps(error.as_dict(), ensure_ascii=False), err=True)
        raise SystemExit(error.exit_code) from error
