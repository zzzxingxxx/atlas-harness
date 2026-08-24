"""Typer CLI for the application shell and event-log inspection."""

import asyncio
import json
import platform
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import typer

from atlas_harness import __version__
from atlas_harness.agent.queues import QueueName, QueueRequest
from atlas_harness.agent.service import AgentService
from atlas_harness.config import Settings, load_settings
from atlas_harness.events import EventStore, EventType, SessionState
from atlas_harness.evolution import (
    CandidateStatus,
    EvaluationRecord,
    Evaluator,
    EvolutionPipeline,
    FeedbackItem,
    FeedbackKind,
    SkillCandidate,
)
from atlas_harness.evolution.runner import SessionTaskRunner
from atlas_harness.kernel.clock import SystemClock
from atlas_harness.kernel.errors import (
    ApprovalDeniedError,
    AtlasError,
    BudgetExceededError,
    CancellationError,
    ConfigurationError,
    EventLogCorruptionError,
    PolicyDeniedError,
    ProviderError,
    RecoveryError,
    ToolError,
    ToolInputError,
    ToolNotFoundError,
    ToolTimeoutError,
    ToolVersionError,
)
from atlas_harness.kernel.ids import new_id
from atlas_harness.mcp.server import load_server_configs
from atlas_harness.memory import MemoryLayer
from atlas_harness.observability.audit import AUDIT_CATEGORIES, build_audit
from atlas_harness.observability.export import build_bundle
from atlas_harness.observability.logging import configure_logging
from atlas_harness.observability.trace import build_trace
from atlas_harness.ops import (
    create_backup,
    rebuild_index,
    rebuild_index_at,
    restore_backup,
    run_release_checks,
    verify_backup,
    verify_data_dir,
)
from atlas_harness.policy import ApprovalGate, ApprovalMode, FixedApprovalGate, PolicyEngine
from atlas_harness.session.branches import BranchService
from atlas_harness.session.recovery import RecoveryPlan
from atlas_harness.session.service import SessionService
from atlas_harness.skills import SkillRecord, SkillStatus, load_directory, parse_status
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


def _recovery_lines(plan: RecoveryPlan) -> list[str]:
    lines = [
        f"session: {plan.session_id}",
        f"last_valid_seq: {plan.last_valid_seq}",
        f"resumed_from_seq: {plan.resumed_from_seq}",
        f"snapshot: {plan.snapshot_id or 'none'}",
    ]
    if not plan.operations:
        lines.append("nothing unfinished")
    for operation in plan.operations:
        lines.append(f"operation {operation.operation_id} [{operation.status}]")
        if operation.model_request_incomplete:
            lines.append("  model request never returned a response")
        for decision in operation.decisions:
            lines.append(
                f"  {decision.action:<7} {decision.call_id} ({decision.tool_name}): "
                f"{decision.reason}"
            )
    lines.append(f"next: {plan.command()}")
    return lines


@app.command()
def recover(
    ctx: typer.Context,
    session_id: str = typer.Argument(..., help="Session id to examine."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """Show what recovery would do without writing anything."""

    with _store(ctx) as store:
        service = SessionService(store)
        try:
            plan = service.recovery.plan(session_id)
        except AtlasError as error:
            typer.echo(json.dumps(error.as_dict(), ensure_ascii=False), err=True)
            raise typer.Exit(code=error.exit_code) from error
    _emit(plan.summary(), json_output=json_output, lines=_recovery_lines(plan))


@app.command()
def resume(
    ctx: typer.Context,
    session_id: str = typer.Argument(..., help="Session id to resume."),
    confirm: list[str] = typer.Option(
        [],
        "--confirm",
        help="Tool call id a human authorizes to run again. Repeatable.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """Resume a crashed session, replaying only what is provably safe.

    A call that already recorded a ``tool_result`` is never re-executed. A call whose
    side effect cannot be proven safe stays suspended until it is named by --confirm.
    """

    with _store(ctx) as store:
        service = SessionService(store)
        try:
            plan = service.resume(session_id, confirm=confirm)
        except AtlasError as error:
            typer.echo(json.dumps(error.as_dict(), ensure_ascii=False), err=True)
            raise typer.Exit(code=error.exit_code) from error
    lines = _recovery_lines(plan)
    _emit(plan.summary(), json_output=json_output, lines=lines)
    if plan.needs_confirmation:
        raise typer.Exit(code=RecoveryError.exit_code)


@app.command()
def abort(
    ctx: typer.Context,
    session_id: str = typer.Argument(..., help="Session id to abort."),
    reason: str = typer.Option("aborted by operator", "--reason", help="Recorded with the event."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """Close every unfinished operation in a session without running anything."""

    with _store(ctx) as store:
        service = SessionService(store)
        try:
            written = service.abort(session_id, reason=reason)
        except AtlasError as error:
            typer.echo(json.dumps(error.as_dict(), ensure_ascii=False), err=True)
            raise typer.Exit(code=error.exit_code) from error
        state = store.load_state(session_id)
    payload = {
        "session_id": session_id,
        "aborted_operations": [event.operation_id for event in written],
        "reason": reason,
        "status": state.status,
        "last_seq": state.last_seq,
    }
    lines = [f"aborted {len(written)} operation(s) in {session_id}", f"status: {state.status}"]
    _emit(payload, json_output=json_output, lines=lines)


@app.command()
def lanes(
    ctx: typer.Context,
    session_id: str = typer.Argument(..., help="Session id to list lanes for."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """List the lanes of a session and where each one branched from."""

    with _store(ctx) as store:
        service = BranchService(store)
        try:
            views = service.lanes(session_id)
            current = service.current_lane(session_id)
        except AtlasError as error:
            typer.echo(json.dumps(error.as_dict(), ensure_ascii=False), err=True)
            raise typer.Exit(code=error.exit_code) from error
    payload = {
        "session_id": session_id,
        "current_lane": current,
        "lanes": [view.model_dump(mode="json") for view in views],
    }
    lines = [
        f"{'*' if view.lane_id == current else ' '} {view.lane_id}  [{view.status}] "
        f"parent={view.parent_lane or '-'} from_seq={view.forked_from_seq or '-'} "
        f"operations={len(view.operation_ids)}"
        for view in views
    ] or ["no lanes"]
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


_STOP_EXIT_CODES: dict[str, int] = {
    "provider_error": ProviderError.exit_code,
    "budget_exceeded": BudgetExceededError.exit_code,
    "cancelled": CancellationError.exit_code,
}


@app.command()
def run(
    ctx: typer.Context,
    prompt: str = typer.Argument(..., help="What the agent should do."),
    session_id: str | None = typer.Option(None, "--session", help="Continue an existing session."),
    provider: str | None = typer.Option(
        None, "--provider", help="Override ATLAS_MODEL_PROVIDER, e.g. fake."
    ),
    model: str | None = typer.Option(None, "--model", help="Override ATLAS_MODEL_NAME."),
    steer: list[str] = typer.Option(
        [], "--steer", help="Queue a steer message before the first turn. Repeatable."
    ),
    approve: bool = typer.Option(False, "--yes", help="Approve tool calls that need approval."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """Run one model -> tool -> result -> model loop and record every step."""

    settings = _settings(ctx)
    if provider is not None:
        settings.model_provider = provider
    if model is not None:
        settings.model_name = model
    with _store(ctx) as store:
        service = AgentService(
            settings=settings,
            store=store,
            approvals=_gate(ApprovalMode(settings.approval_mode), approve),
        )
        report = service.run_sync(prompt, session_id=session_id, steer=list(steer))
    payload = report.summary()
    payload["answer"] = report.result.answer
    lines = [
        f"session: {report.result.session_id}",
        f"operation: {report.result.operation_id}",
        f"model: {report.provider}/{report.model}",
        f"stop: {report.result.stop_cause.value} after {report.result.iterations} "
        f"iterations, {report.result.tool_calls} tool calls",
        f"tokens: {report.result.usage.total_tokens}",
        "",
        report.result.answer or f"(no answer: {report.result.error or 'none'})",
    ]
    _emit(payload, json_output=json_output, lines=lines)
    if not report.result.succeeded:
        code = _STOP_EXIT_CODES.get(report.result.error_code or "", ProviderError.exit_code)
        raise typer.Exit(code=code)


@app.command()
def compact(
    ctx: typer.Context,
    session_id: str = typer.Argument(..., help="Session to compact."),
    operation_id: str | None = typer.Option(
        None, "--operation", help="Compact one operation instead of the latest."
    ),
    objective: str = typer.Option(
        "", "--objective", help="Override the objective recorded in the summary."
    ),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """Compact a session's context, keeping the events and artifacts intact.

    Nothing is deleted. The structured summary is recorded as an event, and the
    original history stays exactly where it was -- compaction replaces what the
    model reads, not what the log holds.
    """

    settings = _settings(ctx)
    with _store(ctx) as store:
        service = AgentService(settings=settings, store=store)
        try:
            summary = service.compact(session_id, operation_id=operation_id, objective=objective)
        except AtlasError as error:
            typer.echo(json.dumps(error.as_dict(), ensure_ascii=False), err=True)
            raise typer.Exit(code=error.exit_code) from error
        state = store.load_state(session_id)
    payload = {
        "session_id": session_id,
        "compactions": state.compactions,
        "last_seq": state.last_seq,
        "summary": summary.model_dump(mode="json"),
    }
    lines = [
        f"compacted {session_id} (compaction #{state.compactions})",
        "",
        summary.as_text(),
    ]
    _emit(payload, json_output=json_output, lines=lines)


def _skill_line(record: SkillRecord) -> str:
    return (
        f"{record.label}  [{record.status.value}]  "
        f"scopes={','.join(record.required_scopes) or '-'}  "
        f"triggers={','.join(record.triggers) or '-'}  "
        f"source={record.source_path or '-'}"
    )


@app.command()
def skills(
    ctx: typer.Context,
    status: SkillStatus | None = typer.Option(
        None, "--status", help="Only show versions in this status.", case_sensitive=False
    ),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """List registered skill versions with their status, scopes and source."""

    settings = _settings(ctx)
    with _store(ctx) as store:
        service = AgentService(settings=settings, store=store)
        records = service.skills.all(status=status)
    payload = [record.to_payload() for record in records]
    lines = [_skill_line(record) for record in records] or ["no skills registered"]
    _emit(payload, json_output=json_output, lines=lines)


@app.command(name="skills-load")
def skills_load(
    ctx: typer.Context,
    directory: Path | None = typer.Option(
        None, "--dir", help="Directory to read from (default: ATLAS_SKILLS_DIR)."
    ),
    session_id: str | None = typer.Option(
        None, "--session", help="Session to record the registrations in."
    ),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """Register the skill files found on disk.

    Loading is not activating. Every file arrives as draft or candidate, so a freshly
    loaded skill is known to the harness but not yet readable by the model; promote it
    with ``atlas skill-status ... --to active`` once it has an evaluation behind it.
    """

    settings = _settings(ctx)
    root = (directory or settings.resolved_skills_dir()).expanduser()
    result = load_directory(root)
    with _store(ctx) as store:
        service = AgentService(settings=settings, store=store)
        try:
            target = service.ensure_session(session_id, title="skill registration")
            registered = [
                service.skills.register(record, session_id=target) for record in result.records
            ]
        except AtlasError as error:
            typer.echo(json.dumps(error.as_dict(), ensure_ascii=False), err=True)
            raise typer.Exit(code=error.exit_code) from error
    payload: dict[str, Any] = {
        "directory": str(root),
        "session_id": target,
        "registered": [record.to_payload() for record in registered],
        "errors": [{"path": str(item.path), "message": item.message} for item in result.errors],
    }
    lines = [f"loaded {len(registered)} skill(s) from {root} into {target}"]
    lines.extend(_skill_line(record) for record in registered)
    lines.extend(f"error {item.path}: {item.message}" for item in result.errors)
    _emit(payload, json_output=json_output, lines=lines)
    if result.errors:
        raise typer.Exit(code=ConfigurationError.exit_code)


@app.command(name="skill-status")
def skill_status(
    ctx: typer.Context,
    skill_id: str = typer.Argument(..., help="Skill to move along the lifecycle."),
    version: str = typer.Option("0.1.0", "--version", help="Which version to move."),
    to_status: str = typer.Option(..., "--to", help="Target status."),
    session_id: str | None = typer.Option(
        None, "--session", help="Session to record the transition in."
    ),
    reason: str | None = typer.Option(None, "--reason", help="Why the status is changing."),
    evaluation_ref: str | None = typer.Option(
        None, "--evaluation", help="Reference to the evaluation behind a promotion."
    ),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """Move one skill version along the lifecycle graph.

    ``draft`` cannot reach ``active`` directly: the missing edge is the evaluation
    gate, so a promotion goes draft to candidate first and carries ``--evaluation``.
    """

    settings = _settings(ctx)
    target_status = parse_status(to_status)
    with _store(ctx) as store:
        service = AgentService(settings=settings, store=store)
        try:
            target = service.ensure_session(session_id, title="skill lifecycle")
            record = service.skills.set_status(
                skill_id,
                version,
                target_status,
                session_id=target,
                reason=reason,
                evaluation_ref=evaluation_ref,
            )
        except AtlasError as error:
            typer.echo(json.dumps(error.as_dict(), ensure_ascii=False), err=True)
            raise typer.Exit(code=error.exit_code) from error
    payload = {"session_id": target, "skill": record.to_payload()}
    _emit(payload, json_output=json_output, lines=[_skill_line(record)])


def _candidate_line(candidate: SkillCandidate) -> str:
    return (
        f"{candidate.candidate_id}  {candidate.label}  [{candidate.status.value}]  "
        f"decision={candidate.decision.value}  "
        f"evidence={','.join(candidate.evidence_refs) or '-'}  "
        f"{candidate.description[:60]}"
    )


def _evaluation_lines(record: EvaluationRecord) -> list[str]:
    metrics = record.metrics
    lines = [
        f"{record.evaluation_id}  {record.skill_id}@{record.version}  "
        f"verdict={record.verdict.value}",
        f"dataset={record.dataset}  tasks={record.task_count}  "
        f"champion={record.champion_version or '-'}",
        f"stages={','.join(record.stages) or '-'}  failed={','.join(record.failed_stages) or '-'}",
        f"pass@1={metrics.pass_at_1:.2f}  completion={metrics.completion_rate:.2f}  "
        f"tools={metrics.tool_effectiveness:.2f}  cost=${metrics.cost_usd:.4f}",
        f"safety_violations={metrics.safety_violation_rate:.2f}  "
        f"regression={metrics.regression_rate:.2f}  recovery={metrics.recovery_rate:.2f}",
    ]
    lines.extend(f"failed task: {task_id}" for task_id in record.failures)
    lines.extend(f"note: {note}" for note in record.notes)
    return lines


@app.command()
def feedback(
    ctx: typer.Context,
    record: str | None = typer.Option(None, "--record", help="Record this text as feedback."),
    kind: FeedbackKind = typer.Option(
        FeedbackKind.CORRECTION, "--kind", help="What kind of feedback.", case_sensitive=False
    ),
    source_task: str | None = typer.Option(
        None, "--task", help="Task the feedback came from. Groups items into one proposal."
    ),
    tool_name: str | None = typer.Option(None, "--tool", help="Tool the feedback is about."),
    evidence: list[str] = typer.Option([], "--evidence", help="Evidence reference. Repeatable."),
    tag: list[str] = typer.Option([], "--tag", help="Tag to attach. Repeatable."),
    session_id: str | None = typer.Option(None, "--session", help="Session to record it in."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """Record feedback, or list what has been recorded.

    Recording is not proposing. Feedback lands in the log and nothing else happens
    until ``atlas skill-propose`` reads it, so an operator can correct the agent
    without that correction turning into a capability behind their back.
    """

    settings = _settings(ctx)
    with _store(ctx) as store:
        service = AgentService(settings=settings, store=store)
        try:
            target = service.ensure_session(session_id, title="feedback")
            stored = (
                None
                if record is None
                else service.evolution.record_feedback(
                    FeedbackItem(
                        feedback_id=new_id("fb"),
                        kind=kind,
                        content=record,
                        source_task=source_task,
                        source_session_id=target,
                        tool_name=tool_name,
                        evidence_refs=tuple(evidence),
                        tags=tuple(tag),
                    ),
                    session_id=target,
                )
            )
            items = service.evolution.all_feedback()
        except AtlasError as error:
            typer.echo(json.dumps(error.as_dict(), ensure_ascii=False), err=True)
            raise typer.Exit(code=error.exit_code) from error
    payload: dict[str, Any] = {
        "session_id": target,
        "recorded": None if stored is None else stored.to_payload(),
        "items": [item.to_payload() for item in items],
    }
    lines = [] if stored is None else [f"recorded {stored.feedback_id} in {target}"]
    lines.extend(
        f"{item.feedback_id}  [{item.kind.value}]  task={item.source_task or '-'}  "
        f"evidence={','.join(item.evidence_refs) or '-'}  {item.content[:60]}"
        for item in items
    )
    _emit(payload, json_output=json_output, lines=lines or ["no feedback recorded"])


@app.command()
def candidates(
    ctx: typer.Context,
    status: CandidateStatus | None = typer.Option(
        None, "--status", help="Only show candidates in this status.", case_sensitive=False
    ),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """List the pending window: proposals that exist but are not capabilities yet."""

    settings = _settings(ctx)
    with _store(ctx) as store:
        service = AgentService(settings=settings, store=store)
        records = service.evolution.candidates(status=status)
    payload = [item.to_payload() | {"status": item.status.value} for item in records]
    lines = [_candidate_line(item) for item in records] or ["no candidates"]
    _emit(payload, json_output=json_output, lines=lines)


@app.command(name="skill-propose")
def skill_propose(
    ctx: typer.Context,
    source_task: str | None = typer.Option(
        None, "--task", help="Only propose from feedback for this task."
    ),
    skill_id: str | None = typer.Option(
        None, "--skill", help="Propose a new version of this skill instead of a new one."
    ),
    session_id: str | None = typer.Option(
        None, "--session", help="Session to record the proposals in."
    ),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """Turn recorded feedback into candidate skill versions.

    A proposal is written at ``candidate`` status and cannot be injected. Refusals are
    written too, so the log distinguishes feedback that was examined and rejected from
    feedback nobody ever looked at.
    """

    settings = _settings(ctx)
    with _store(ctx) as store:
        service = AgentService(settings=settings, store=store)
        pipeline = EvolutionPipeline(service.evolution)
        try:
            target = service.ensure_session(session_id, title="skill proposal")
            items = [
                item
                for item in service.evolution.all_feedback()
                if source_task is None or item.source_task == source_task
            ]
            outcomes = (
                [pipeline.propose_from(items, session_id=target, skill_id=skill_id)]
                if source_task is not None or skill_id is not None
                else pipeline.propose_all(items, session_id=target)
            )
        except AtlasError as error:
            typer.echo(json.dumps(error.as_dict(), ensure_ascii=False), err=True)
            raise typer.Exit(code=error.exit_code) from error
    payload = {
        "session_id": target,
        "considered": len(items),
        "outcomes": [
            {
                "accepted": outcome.accepted,
                "decision": outcome.decision.value,
                "reason": outcome.reason,
                "detail": outcome.detail,
                "notes": list(outcome.notes),
                "candidate": None if outcome.candidate is None else outcome.candidate.to_payload(),
            }
            for outcome in outcomes
        ],
    }
    lines = [f"considered {len(items)} feedback item(s) in {target}"]
    for outcome in outcomes:
        if outcome.accepted and outcome.candidate is not None:
            lines.append(f"proposed {_candidate_line(outcome.candidate)}")
        else:
            detail = "" if outcome.detail is None else f" ({outcome.detail})"
            lines.append(f"rejected: {outcome.reason or 'unknown'}{detail}")
        lines.extend(f"  {note}" for note in outcome.notes)
    _emit(payload, json_output=json_output, lines=lines)


@app.command(name="skill-evaluate")
def skill_evaluate(
    ctx: typer.Context,
    candidate_id: str = typer.Argument(..., help="Candidate to measure."),
    session_id: str | None = typer.Option(
        None, "--session", help="Session to record the verdict in."
    ),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """Run a candidate against the fixed task sets and record the verdict.

    The candidate is made effective for the duration of each task and for nothing
    else: the run sees a view of the skill library, not a changed one, so a crash
    part-way through cannot leave the candidate live.
    """

    settings = _settings(ctx)
    with _store(ctx) as store:
        service = AgentService(settings=settings, store=store)
        pipeline = EvolutionPipeline(
            service.evolution,
            evaluator=Evaluator(runner=SessionTaskRunner(service)),
        )
        try:
            target = service.ensure_session(session_id, title="skill evaluation")
            evaluation = pipeline.evaluate(candidate_id, session_id=target)
        except AtlasError as error:
            typer.echo(json.dumps(error.as_dict(), ensure_ascii=False), err=True)
            raise typer.Exit(code=error.exit_code) from error
    payload = {"session_id": target, "evaluation": evaluation.to_payload()}
    _emit(payload, json_output=json_output, lines=_evaluation_lines(evaluation))
    if not evaluation.passed:
        raise typer.Exit(code=ToolError.exit_code)


@app.command(name="skill-promote")
def skill_promote(
    ctx: typer.Context,
    candidate_id: str = typer.Argument(..., help="Candidate to make effective."),
    reason: str | None = typer.Option(None, "--reason", help="Why it is being promoted."),
    session_id: str | None = typer.Option(
        None, "--session", help="Session to record the promotion in."
    ),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """Make an evaluated candidate the champion, using its most recent verdict.

    A candidate with no evaluation, or whose newest evaluation did not pass, is
    refused here. That refusal is the evaluation gate: it is the only way a version
    reaches ``active``.
    """

    settings = _settings(ctx)
    with _store(ctx) as store:
        service = AgentService(settings=settings, store=store)
        pipeline = EvolutionPipeline(service.evolution)
        try:
            target = service.ensure_session(session_id, title="skill promotion")
            record = pipeline.promote(candidate_id, session_id=target, reason=reason)
        except AtlasError as error:
            typer.echo(json.dumps(error.as_dict(), ensure_ascii=False), err=True)
            raise typer.Exit(code=error.exit_code) from error
    payload = {"session_id": target, "skill": record.to_payload()}
    _emit(payload, json_output=json_output, lines=[_skill_line(record)])


@app.command(name="skill-rollback")
def skill_rollback(
    ctx: typer.Context,
    skill_id: str = typer.Argument(..., help="Skill to roll back."),
    to_version: str = typer.Option(..., "--to", help="Version to make effective again."),
    reason: str | None = typer.Option(None, "--reason", help="Why it is being rolled back."),
    session_id: str | None = typer.Option(
        None, "--session", help="Session to record the rollback in."
    ),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """Make a previously deprecated version effective again.

    Only a version this skill already had can be named. Rolling back to something
    that was never registered would be a promotion with no evaluation behind it, so
    the command lists the available versions and refuses instead.
    """

    settings = _settings(ctx)
    with _store(ctx) as store:
        service = AgentService(settings=settings, store=store)
        pipeline = EvolutionPipeline(service.evolution)
        try:
            target = service.ensure_session(session_id, title="skill rollback")
            record = pipeline.rollback(skill_id, to_version, session_id=target, reason=reason)
        except AtlasError as error:
            typer.echo(json.dumps(error.as_dict(), ensure_ascii=False), err=True)
            raise typer.Exit(code=error.exit_code) from error
    payload = {"session_id": target, "skill": record.to_payload()}
    _emit(payload, json_output=json_output, lines=[_skill_line(record)])


@app.command()
def memory(
    ctx: typer.Context,
    remember: str | None = typer.Option(None, "--remember", help="Store this text as a memory."),
    layer: MemoryLayer = typer.Option(
        MemoryLayer.SEMANTIC, "--layer", help="Which memory layer to write.", case_sensitive=False
    ),
    source_task: str | None = typer.Option(
        None, "--source-task", help="Task this memory came from."
    ),
    confidence: float = typer.Option(0.5, "--confidence", min=0.0, max=1.0),
    evidence: list[str] = typer.Option([], "--evidence", help="Evidence reference. Repeatable."),
    tag: list[str] = typer.Option([], "--tag", help="Tag to attach. Repeatable."),
    session_id: str | None = typer.Option(None, "--session", help="Session to record it in."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """Store a memory, or list what is stored and whether it still counts."""

    settings = _settings(ctx)
    now_ms = SystemClock().now_ms()
    with _store(ctx) as store:
        service = AgentService(settings=settings, store=store)
        try:
            target = service.ensure_session(session_id, title="memory management")
            stored = (
                None
                if remember is None
                else service.memories.remember(
                    remember,
                    session_id=target,
                    layer=layer,
                    source_task=source_task,
                    confidence=confidence,
                    evidence_refs=tuple(evidence),
                    tags=tuple(tag),
                )
            )
            records = service.memories.all()
        except AtlasError as error:
            typer.echo(json.dumps(error.as_dict(), ensure_ascii=False), err=True)
            raise typer.Exit(code=error.exit_code) from error
    payload: dict[str, Any] = {
        "session_id": target,
        "stored": None if stored is None else stored.to_payload(),
        "records": [record.to_payload() for record in records],
    }
    lines = [] if stored is None else [f"stored {stored.memory_id} in {target}"]
    lines.extend(
        f"{record.memory_id}  [{record.layer.value}]  "
        f"confidence={record.confidence:.2f}  "
        f"{'expired' if record.is_expired(now_ms) else 'live'}  "
        f"{record.content[:60]}"
        for record in records
    )
    _emit(payload, json_output=json_output, lines=lines or ["no memories stored"])


@app.command(name="memory-expire")
def memory_expire(
    ctx: typer.Context,
    memory_id: str | None = typer.Argument(None, help="Memory to retire."),
    sweep: bool = typer.Option(False, "--sweep", help="Retire everything already past its TTL."),
    reason: str = typer.Option("manual", "--reason", help="Why it is being retired."),
    session_id: str | None = typer.Option(
        None, "--session", help="Session to record the expiry in."
    ),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """Take memories out of the retrievable set.

    Expiry is not deletion. The row and the original ``memory_stored`` event both
    stay, so a replay can still say the memory existed and when it stopped being
    used; removing it for good needs an explicit management command and a backup.
    """

    if memory_id is None and not sweep:
        typer.echo("pass a memory id or --sweep", err=True)
        raise typer.Exit(code=ToolInputError.exit_code)
    settings = _settings(ctx)
    with _store(ctx) as store:
        service = AgentService(settings=settings, store=store)
        try:
            target = service.ensure_session(session_id, title="memory management")
            if sweep:
                expired = service.memories.sweep(session_id=target)
            else:
                assert memory_id is not None  # guarded above
                record = service.memories.expire(memory_id, session_id=target, reason=reason)
                expired = [] if record is None else [record.memory_id]
        except AtlasError as error:
            typer.echo(json.dumps(error.as_dict(), ensure_ascii=False), err=True)
            raise typer.Exit(code=error.exit_code) from error
    payload = {"session_id": target, "expired": expired}
    lines = [f"expired {len(expired)} memory record(s)", *expired] or ["nothing to expire"]
    _emit(payload, json_output=json_output, lines=lines)


@app.command()
def capabilities(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Task text to retrieve capabilities for."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """Show what a task would retrieve, and why everything else was left out.

    This runs the same selector the agent loop runs, against the same grant the
    executor enforces, so an unpermitted skill shows up here as skipped rather than
    as low-ranked -- which is the explanation the plan asks the trace to give.
    """

    settings = _settings(ctx)
    with _store(ctx) as store:
        service = AgentService(settings=settings, store=store)
        selector = service.build_selector()
        plan = None if selector is None else selector.select(query)
    if plan is None:
        payload: dict[str, Any] = {"query": query, "injection": "disabled"}
        _emit(payload, json_output=json_output, lines=["capability injection is disabled"])
        return
    explained = plan.explain()
    lines = [
        f"query: {query}",
        f"tokens: {plan.tokens_used}/{plan.token_budget}",
        f"scopes: {','.join(plan.granted_scopes) or '-'}",
        "",
    ]
    lines.extend(
        f"+ {choice.kind} {choice.ref_id}"
        f"{'' if choice.version is None else '@' + choice.version}  "
        f"score={choice.score:.4f} tokens={choice.tokens}"
        for choice in plan.selected
    )
    lines.extend(
        f"- {skip.kind} {skip.ref_id}  {skip.reason}"
        f"{'' if skip.detail is None else ' (' + skip.detail + ')'}"
        for skip in plan.skipped
    )
    _emit(explained, json_output=json_output, lines=lines)


@app.command()
def messages(
    ctx: typer.Context,
    session_id: str = typer.Argument(..., help="Session to queue a message for."),
    content: str | None = typer.Option(None, "--send", help="Message content to enqueue."),
    queue: QueueName = typer.Option(
        QueueName.STEER, "--queue", help="Which queue to write to.", case_sensitive=False
    ),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """Queue a message for a session, or show what is still pending."""

    settings = _settings(ctx)
    with _store(ctx) as store:
        service = AgentService(settings=settings, store=store)
        message_id: str | None = None
        if content is not None:
            message_id = service.enqueue(
                QueueRequest(queue=queue, content=content, source="cli"),
                session_id=session_id,
            )
        pending = service.pending_queues(session_id)
    payload: dict[str, Any] = {
        "session_id": session_id,
        "enqueued": message_id,
        "pending": pending.model_dump(mode="json"),
    }
    lines = [f"session: {session_id}"]
    if message_id is not None:
        lines.append(f"enqueued {message_id} on {queue.value}")
    lines.append(
        f"pending: steer={pending.steer} follow_up={pending.follow_up} next_run={pending.next_run}"
    )
    _emit(payload, json_output=json_output, lines=lines)


@app.command()
def trace(
    ctx: typer.Context,
    session_id: str = typer.Argument(..., help="Session to render as a timeline."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """Print every recorded step of a session in order."""

    with _store(ctx) as store:
        rendered = build_trace(store.read_events(session_id), session_id=session_id)
    payload = rendered.model_dump(mode="json")
    payload["counts"] = rendered.counts()
    _emit(payload, json_output=json_output, lines=rendered.render() or ["no events"])


mcp_app = typer.Typer(
    name="mcp",
    help="Inspect the configured MCP servers.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(mcp_app)


def _mcp_service(ctx: typer.Context, store: EventStore) -> AgentService:
    """Build a service whose MCP configs are read even when MCP is disabled.

    ``atlas mcp list`` has to be able to say "configured but disabled". A service
    built with the plain settings would report an empty list, which reads as "no
    servers configured" and is the one answer an operator must not be given here.
    """

    settings = _settings(ctx)
    configs = load_server_configs(settings.resolved_mcp_config_dir())
    return AgentService(settings=settings, store=store, mcp_configs=configs)


@mcp_app.command("list")
def mcp_list(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """List the configured MCP servers without connecting to any of them."""

    settings = _settings(ctx)
    with _store(ctx) as store:
        service = _mcp_service(ctx, store)
        statuses = service.mcp_statuses()
    payload: dict[str, Any] = {
        "enabled": settings.enable_mcp,
        "config_dir": str(settings.resolved_mcp_config_dir()),
        "servers": [status.summary() for status in statuses],
    }
    lines = [f"config_dir: {payload['config_dir']}", f"enabled: {settings.enable_mcp}"]
    if not statuses:
        lines.append("no servers configured")
    for status in statuses:
        state = "connected" if status.connected else ("enabled" if status.enabled else "disabled")
        lines.append(
            f"{status.name:<20} {status.transport:<8} {state:<10} tools={len(status.tools)}"
        )
    _emit(payload, json_output=json_output, lines=lines)


@mcp_app.command("inspect")
def mcp_inspect(
    ctx: typer.Context,
    server: str = typer.Argument(..., help="Server name from the MCP config."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """Print one server's capability manifest, tool list and refusals."""

    with _store(ctx) as store:
        service = _mcp_service(ctx, store)
        payload = service.mcp.inspect(server)
    config = payload["config"]
    lines = [
        f"server: {config['name']}",
        f"transport: {config['transport']}",
        f"address: {config.get('address') or 'n/a'}",
        f"enabled: {config['enabled']}",
        f"connected: {payload['connected']}",
        f"protocol_version: {payload['protocol_version'] or 'unknown'}",
        f"granted_scopes: {', '.join(config['granted_scopes']) or 'none'}",
        f"capabilities: {', '.join(payload['capabilities']) or 'none'}",
    ]
    # Offered, bridged and refused are three different lists on purpose: a server
    # can offer a tool that never became callable, and collapsing them would hide
    # exactly the case an operator runs this command to see.
    lines.extend(f"  offered {tool['name']} ({tool['risk']})" for tool in payload["offered"])
    lines.extend(f"  bridged {name}" for name in payload["bridged"])
    lines.extend(f"  refused {item['tool']}: {item['reason']}" for item in payload["rejected"])
    if payload["error"]:
        lines.append(f"error: {payload['error']}")
    _emit(payload, json_output=json_output, lines=lines)


@app.command()
def audit(
    ctx: typer.Context,
    session_id: str = typer.Argument(..., help="Session to account for."),
    category: str | None = typer.Option(
        None, "--category", help=f"Limit to one of: {', '.join(AUDIT_CATEGORIES)}."
    ),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """Answer the accountability questions for one session from its log."""

    if category is not None and category not in AUDIT_CATEGORIES:
        raise ConfigurationError(
            "unknown audit category",
            details={"category": category, "known": list(AUDIT_CATEGORIES)},
        )
    with _store(ctx) as store:
        log = build_audit(store.read_events(session_id), session_id=session_id)
    records = log.of_category(category) if category else log.records
    payload: dict[str, Any] = {
        "summary": log.summary(),
        "records": [record.as_json() for record in records],
    }
    lines = [record.render() for record in records] or ["no auditable events"]
    _emit(payload, json_output=json_output, lines=lines)


@app.command()
def export(
    ctx: typer.Context,
    session_id: str = typer.Argument(..., help="Session to export."),
    out_dir: Path | None = typer.Option(
        None, "--out", help="Directory to write the four artefacts into."
    ),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """Write trace.jsonl, audit.jsonl, metrics.json and replay-report.json."""

    settings = _settings(ctx)
    with _store(ctx) as store:
        bundle = build_bundle(store.read_events(session_id), session_id=session_id)
    target = out_dir or (settings.resolved_data_dir() / "exports" / session_id)
    written = bundle.write(target)
    payload: dict[str, Any] = {
        **bundle.summary(),
        "written": {name: str(path) for name, path in written.items()},
    }
    lines = [f"session: {session_id}", f"directory: {target}"]
    lines.extend(f"wrote {name}" for name in written)
    lines.extend(bundle.replay.render())
    _emit(payload, json_output=json_output, lines=lines)


@app.command()
def verify(
    ctx: typer.Context,
    session_id: str | None = typer.Option(None, "--session", help="Verify one session only."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """Check logs, index rows and artifacts against each other, changing nothing.

    The exit code is the operator's instruction rather than a mere pass or fail: 0
    means nothing is wrong, 1 means derived state drifted and ``atlas reindex``
    repairs it, and 8 means the log itself is damaged and only a restore will do.
    """

    with _store(ctx) as store:
        report = verify_data_dir(store, sessions=[session_id] if session_id else None)
    _emit(report.as_json(), json_output=json_output, lines=report.render())
    if report.ok:
        return
    raise typer.Exit(code=1 if report.repairable else EventLogCorruptionError.exit_code)


@app.command()
def reindex(
    ctx: typer.Context,
    session_id: str | None = typer.Option(None, "--session", help="Rebuild one session only."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """Rebuild the SQLite index from the logs, which are the only source of truth."""

    with _store(ctx) as store:
        report = rebuild_index(store, sessions=[session_id] if session_id else None)
    _emit(report.as_json(), json_output=json_output, lines=report.render())
    if not report.ok:
        raise typer.Exit(code=1)


@app.command()
def backup(
    ctx: typer.Context,
    out_dir: Path | None = typer.Option(None, "--out", help="Directory to write the backup into."),
    session_id: str | None = typer.Option(None, "--session", help="Back up one session only."),
    no_index: bool = typer.Option(False, "--no-index", help="Skip the SQLite index snapshot."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """Copy logs, artifacts and the index somewhere safe, then verify the copy.

    The verification runs here instead of being left to the operator because a
    backup nobody has checked is only a belief that a backup exists.
    """

    settings = _settings(ctx)
    target = out_dir or (
        settings.resolved_data_dir() / "backups" / f"backup-{SystemClock().now_ms()}"
    )
    with _store(ctx) as store:
        manifest = create_backup(
            store,
            target,
            sessions=[session_id] if session_id else None,
            include_index=not no_index,
        )
    verification = verify_backup(target)
    payload: dict[str, Any] = {
        "directory": str(target),
        "manifest": manifest.as_json(),
        "verification": verification.as_json(),
    }
    _emit(payload, json_output=json_output, lines=[*manifest.render(), *verification.render()])
    if not verification.ok:
        raise typer.Exit(code=1)


@app.command(name="backup-check")
def backup_check(
    source: Path = typer.Argument(..., help="Backup directory to re-hash."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """Re-hash a stored backup, so it can be checked long before it is needed."""

    verification = verify_backup(source)
    _emit(verification.as_json(), json_output=json_output, lines=verification.render())
    if not verification.ok:
        raise typer.Exit(code=1)


@app.command()
def restore(
    ctx: typer.Context,
    source: Path = typer.Argument(..., help="Backup directory to restore from."),
    target: Path | None = typer.Option(None, "--target", help="Data directory to write into."),
    force: bool = typer.Option(False, "--force", help="Allow restoring into a non-empty target."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """Restore a verified backup and prove the restored logs still fold the same.

    Exits non-zero when a restored log folds to a different state hash than it did
    when it was backed up. The bytes were already proven identical by the checksum,
    so that can only mean this build reads that log differently, which is the
    backward-compatibility break the release gate exists to catch.
    """

    settings = _settings(ctx)
    landing = target or settings.resolved_data_dir()
    report = restore_backup(source, landing, force=force)
    lines = list(report.render())
    payload: dict[str, Any] = {"restore": report.as_json()}
    if not report.index_restored:
        # A restore with no index in the backup leaves a directory nothing can serve
        # from, so the rebuild happens here rather than as advice in a runbook.
        rebuilt = rebuild_index_at(landing)
        lines.extend(rebuilt.render())
        payload["reindex"] = rebuilt.as_json()
    _emit(payload, json_output=json_output, lines=lines)
    if not report.compatible:
        raise typer.Exit(code=1)


@app.command(name="release-check")
def release_check(
    ctx: typer.Context,
    samples_dir: Path = typer.Option(
        Path("samples"), "--samples", help="Directory of frozen replay samples."
    ),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """Run the release checklist against a real data directory and print the verdict."""

    settings = _settings(ctx)
    with _store(ctx) as store:
        report = run_release_checks(
            store,
            samples_dir=samples_dir,
            workspace_root=settings.resolved_workspace_root(),
        )
    _emit(report.as_json(), json_output=json_output, lines=report.render())
    if not report.ok:
        raise typer.Exit(code=1)


def main() -> None:
    try:
        app()
    except AtlasError as error:
        typer.echo(json.dumps(error.as_dict(), ensure_ascii=False), err=True)
        raise SystemExit(error.exit_code) from error
