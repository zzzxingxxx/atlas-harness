"""HTTP routes over the application services.

Every route here is a translation, not an implementation. The plan's rule is that
``transport`` may only call application services, and the reason is testability
rather than tidiness: the moment an HTTP handler decides something the CLI does
not, the two entry points can disagree, and a replay test that passes through one
proves nothing about the other. So a route's whole job is to turn a request into
service arguments, and a service result or an :class:`AtlasError` into a response.

Error mapping is the one piece of real logic, and it is a table. Every
:class:`AtlasError` already carries a ``code`` and an ``exit_code``; the status
code is derived from the error class, so a new error type gets a sensible 500 and
nothing silently becomes a 200.

FastAPI is an optional import. The runtime is a library and a CLI first, and a
missing web framework should not stop ``atlas run`` from working -- so the failure
is raised when a server is actually asked for, naming the extra to install.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from atlas_harness.config import Settings
from atlas_harness.events.models import Event
from atlas_harness.events.store import EventStore
from atlas_harness.kernel.errors import (
    ApprovalDeniedError,
    AtlasError,
    BudgetExceededError,
    ConfigurationError,
    LifecycleError,
    PolicyDeniedError,
    ProviderError,
    RecoveryError,
    SessionNotFoundError,
    ToolInputError,
    ToolNotFoundError,
)
from atlas_harness.observability.export import build_bundle
from atlas_harness.session.service import SessionService
from atlas_harness.skills.repository import SkillRepository

if TYPE_CHECKING:  # pragma: no cover - import for annotations only
    from fastapi import FastAPI

HTTP_BAD_REQUEST = 400
HTTP_FORBIDDEN = 403
HTTP_NOT_FOUND = 404
HTTP_CONFLICT = 409
HTTP_UNPROCESSABLE = 422
HTTP_INTERNAL_ERROR = 500
HTTP_BAD_GATEWAY = 502

_STATUS_BY_ERROR: tuple[tuple[type[AtlasError], int], ...] = (
    (SessionNotFoundError, HTTP_NOT_FOUND),
    (ToolNotFoundError, HTTP_NOT_FOUND),
    (ToolInputError, HTTP_UNPROCESSABLE),
    (ConfigurationError, HTTP_UNPROCESSABLE),
    (PolicyDeniedError, HTTP_FORBIDDEN),
    (ApprovalDeniedError, HTTP_FORBIDDEN),
    (RecoveryError, HTTP_CONFLICT),
    (LifecycleError, HTTP_CONFLICT),
    (BudgetExceededError, HTTP_CONFLICT),
    (ProviderError, HTTP_BAD_GATEWAY),
)
"""Ordered most specific first, because ``ToolNotFoundError`` is a ``ToolError``
and ``ProviderTimeoutError`` is a ``ProviderError``. A dict keyed on the exact
class would miss every subclass; a first-match walk over this tuple does not."""


def status_for(error: AtlasError) -> int:
    """Map one error onto its HTTP status.

    A ``RecoveryError`` is a 409 rather than a 500 because a suspended session is
    not a server fault -- it is a state the caller has to resolve, and the body
    carries the command that resolves it. Treating it as a 500 would tell a
    client to retry, which is exactly what must not happen.
    """

    for error_type, status in _STATUS_BY_ERROR:
        if isinstance(error, error_type):
            return status
    return HTTP_INTERNAL_ERROR


def error_body(error: AtlasError) -> dict[str, Any]:
    """The same structured error the CLI prints, plus its exit code.

    Carrying ``exit_code`` over HTTP looks odd until a script drives both: a
    caller that shells out for one operation and posts for another can branch on
    one field instead of two.
    """

    body = dict(error.as_dict())
    body["exit_code"] = error.exit_code
    return body


class RunRequest(BaseModel):
    """A run as the HTTP caller states it."""

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1)
    session_id: str | None = None
    steer: tuple[str, ...] = ()


class CompactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: str | None = None
    objective: str = ""


class AbortRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = "aborted by operator"


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: tuple[str, ...] = ()
    """Tool call ids a human authorizes to run again. Empty means "replay only
    what is provably safe", which is the same default the CLI has."""


def build_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI app.

    The store is opened per request rather than once per app. SQLite connections
    are not safe to share across threads, and an ASGI server will hand a
    synchronous handler to a worker thread -- so a single long-lived store would
    be a race that only appears under concurrent load.
    """

    try:
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse, PlainTextResponse
    except ModuleNotFoundError as error:  # pragma: no cover - depends on install extras
        raise ConfigurationError(
            "the HTTP transport needs fastapi; install atlas-harness[http]",
            details={"missing": error.name},
        ) from error

    resolved = settings or Settings()
    app = FastAPI(title="AtlasHarness", version="0.1.0")

    @contextmanager
    def service_for() -> Iterator[Any]:
        """Open an AgentService for one request and always close it.

        Imported here rather than at module scope so a process that only reads
        sessions never builds a model adapter.
        """

        from atlas_harness.agent.service import AgentService

        store = EventStore.from_settings(resolved)
        try:
            yield AgentService(settings=resolved, store=store)
        finally:
            store.close()

    @contextmanager
    def store_for() -> Iterator[EventStore]:
        store = EventStore.from_settings(resolved)
        try:
            yield store
        finally:
            store.close()

    def fail(error: AtlasError) -> JSONResponse:
        return JSONResponse(status_code=status_for(error), content=error_body(error))

    def read_log(store: EventStore, session_id: str) -> list[Event]:
        """Read a session's events, refusing a session that was never written.

        ``read_events`` answers an unknown id with an empty list, which is right for
        a projection but wrong for a route: a mistyped id would come back as a clean
        empty trace instead of a 404, and the caller would believe it.
        """

        if not store.session_exists(session_id):
            raise SessionNotFoundError(
                "no such session",
                details={"session_id": session_id},
            )
        return store.read_events(session_id)

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"status": "ok", "provider": resolved.model_provider, "model": resolved.model_name}

    @app.get("/sessions")
    def list_sessions() -> dict[str, Any]:
        with store_for() as store:
            summaries = [summary.model_dump(mode="json") for summary in store.list_sessions()]
        return {"sessions": summaries}

    @app.get("/sessions/{session_id}")
    def get_session(session_id: str) -> Any:
        with store_for() as store:
            try:
                state = store.load_state(session_id)
            except AtlasError as error:
                return fail(error)
            payload = state.model_dump(mode="json")
            payload["state_hash"] = state.state_hash()
        return payload

    @app.get("/sessions/{session_id}/events")
    def get_events(session_id: str, after: int = 0, limit: int = 200) -> Any:
        """Read a page of the log.

        Paged on ``seq`` rather than an offset because seq is what the log is
        ordered by and what every other surface refers to. An offset would shift
        under a concurrent append; a seq cursor cannot.
        """

        with store_for() as store:
            try:
                events = read_log(store, session_id)
            except AtlasError as error:
                return fail(error)
        page = [event for event in events if event.seq > after][: max(1, min(limit, 1000))]
        return {
            "session_id": session_id,
            "after": after,
            "events": [event.model_dump(mode="json") for event in page],
            "next_after": page[-1].seq if page else after,
        }

    @app.post("/sessions/{session_id}/run")
    async def run_session(session_id: str, request: RunRequest) -> Any:
        with service_for() as service:
            try:
                report = await service.run(
                    request.prompt,
                    session_id=session_id,
                    steer=list(request.steer),
                )
            except AtlasError as error:
                return fail(error)
            return {"summary": report.summary(), "answer": report.result.answer}

    @app.post("/runs")
    async def create_run(request: RunRequest) -> Any:
        """Run without naming a session, letting the service open one."""

        with service_for() as service:
            try:
                report = await service.run(
                    request.prompt,
                    session_id=request.session_id,
                    steer=list(request.steer),
                )
            except AtlasError as error:
                return fail(error)
            return {"summary": report.summary(), "answer": report.result.answer}

    @app.post("/sessions/{session_id}/abort")
    def abort_session(session_id: str, request: AbortRequest) -> Any:
        with store_for() as store:
            sessions = SessionService(store)
            try:
                written = sessions.abort(session_id, reason=request.reason)
                state = store.load_state(session_id)
            except AtlasError as error:
                return fail(error)
        return {
            "session_id": session_id,
            "aborted_operations": [event.operation_id for event in written],
            "reason": request.reason,
            "status": state.status,
            "last_seq": state.last_seq,
        }

    @app.post("/sessions/{session_id}/resume")
    def resume_session(session_id: str, request: ResumeRequest) -> Any:
        with store_for() as store:
            sessions = SessionService(store)
            try:
                plan = sessions.resume(session_id, confirm=list(request.confirm))
            except AtlasError as error:
                return fail(error)
        body = plan.summary()
        if plan.needs_confirmation:
            # Not an error: the resume did what it safely could. A 409 tells the
            # caller the session is still owed a decision and names the calls.
            return JSONResponse(status_code=HTTP_CONFLICT, content=body)
        return body

    @app.post("/sessions/{session_id}/compact")
    def compact_session(session_id: str, request: CompactRequest) -> Any:
        with service_for() as service:
            try:
                summary = service.compact(
                    session_id,
                    operation_id=request.operation_id,
                    objective=request.objective,
                )
            except AtlasError as error:
                return fail(error)
            return summary.model_dump(mode="json")

    @app.get("/sessions/{session_id}/trace")
    def get_trace(session_id: str, text: bool = False) -> Any:
        with store_for() as store:
            try:
                events = read_log(store, session_id)
            except AtlasError as error:
                return fail(error)
        bundle = build_bundle(events, session_id=session_id)
        if text:
            return PlainTextResponse("\n".join(bundle.trace.render()) + "\n")
        return bundle.trace.summary()

    @app.get("/sessions/{session_id}/audit")
    def get_audit(session_id: str) -> Any:
        with store_for() as store:
            try:
                events = read_log(store, session_id)
            except AtlasError as error:
                return fail(error)
        bundle = build_bundle(events, session_id=session_id)
        return {
            "summary": bundle.audit.summary(),
            "records": [record.as_json() for record in bundle.audit.records],
        }

    @app.get("/sessions/{session_id}/export")
    def get_export(session_id: str) -> Any:
        """All four plan artefacts as one JSON body.

        The same bundle the CLI writes to disk. Serving it rather than a file path
        is what lets a remote observability console read a session it has no
        filesystem access to.
        """

        with store_for() as store:
            try:
                events = read_log(store, session_id)
            except AtlasError as error:
                return fail(error)
        return build_bundle(events, session_id=session_id).summary()

    @app.get("/skills")
    def list_skills() -> Any:
        with store_for() as store:
            records = SkillRepository(store).all()
        return {"skills": [record.model_dump(mode="json") for record in records]}

    @app.get("/tools")
    def list_tools() -> Any:
        from atlas_harness.tools.registry import default_registry

        return {"tools": [manifest.describe() for manifest in default_registry().manifests()]}

    return app


def main() -> None:  # pragma: no cover - process entry point
    """Serve the app with uvicorn, for ``python -m atlas_harness.transport.http``."""

    try:
        import uvicorn
    except ModuleNotFoundError as error:
        raise ConfigurationError(
            "serving the HTTP transport needs uvicorn; install atlas-harness[http]",
            details={"missing": error.name},
        ) from error

    settings = Settings()
    uvicorn.run(build_app(settings), host=settings.http_host, port=settings.http_port)


if __name__ == "__main__":  # pragma: no cover
    main()
