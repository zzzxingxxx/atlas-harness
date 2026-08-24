"""The HTTP transport answers what the CLI answers, over the same services.

The plan's requirement is that the HTTP API and the CLI share a service layer
rather than duplicating business logic. That is not provable by reading two
handlers side by side, so it is asserted the only way it can be: the same run
driven over HTTP lands the same event sequence in the same log, and the trace,
audit and export bodies equal what building them directly from that log produces.

The store is opened per request inside the app, so every fixture here writes to
one on-disk data directory and reads it back with its own ``EventStore``. That is
also what makes the 404 and 409 cases real -- the state the app rejects is state
that exists on disk, not a mock.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from atlas_harness.config import Settings
from atlas_harness.events import EventStore, EventType
from atlas_harness.observability.export import EXPORT_FILENAMES, build_bundle
from atlas_harness.transport.http import build_app

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient


@pytest.fixture
def runtime(tmp_path: Path) -> Settings:
    """A workspace, an empty log beside it, and the canned provider."""

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("the answer is 42\n", encoding="utf-8")
    return Settings(
        data_dir=tmp_path / "runtime",
        workspace_root=workspace,
        model_provider="fake",
        model_name="fake-model",
    )


@pytest.fixture
def client(runtime: Settings) -> Iterator[Any]:
    with TestClient(build_app(runtime)) as opened:
        yield opened


def log_types(runtime: Settings, session_id: str) -> list[str]:
    with EventStore.from_settings(runtime) as store:
        return [event.event_type.value for event in store.read_events(session_id)]


def events_of(runtime: Settings, session_id: str) -> list[Any]:
    with EventStore.from_settings(runtime) as store:
        return store.read_events(session_id)


def run_once(client: Any, prompt: str = "hello") -> dict[str, Any]:
    response = client.post("/runs", json={"prompt": prompt})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def seed_crashed(runtime: Settings, session_id: str = "ses_crashed") -> str:
    """A session left mid-write, which is what makes resume owe a decision."""

    with EventStore.from_settings(runtime) as store:
        store.append_new(
            EventType.SESSION_CREATED,
            session_id=session_id,
            payload={"title": "crashed", "workspace_root": str(runtime.workspace_root)},
        )
        store.append_new(
            EventType.OPERATION_STARTED,
            session_id=session_id,
            operation_id="op_crashed",
            payload={"name": "agent_run"},
        )
        store.append_new(
            EventType.TOOL_STARTED,
            session_id=session_id,
            operation_id="op_crashed",
            payload={
                "tool_name": "write_file",
                "call_id": "call_write",
                "arguments": {"path": "notes.txt"},
                "idempotency_key": "key-write",
                "risk": "write",
                "idempotent": True,
            },
        )
    return session_id


# --------------------------------------------------------------------------- #
# the app comes up without a model or a session
# --------------------------------------------------------------------------- #


def test_healthz_names_the_configured_model(client: Any) -> None:
    body = client.get("/healthz").json()

    assert body == {"status": "ok", "provider": "fake", "model": "fake-model"}


def test_tools_lists_the_builtin_registry(client: Any) -> None:
    names = [tool["name"] for tool in client.get("/tools").json()["tools"]]

    assert "read_file" in names
    assert "write_file" in names


def test_sessions_is_empty_before_anything_runs(client: Any) -> None:
    assert client.get("/sessions").json() == {"sessions": []}


def test_skills_is_empty_before_anything_is_registered(client: Any) -> None:
    assert client.get("/skills").json() == {"skills": []}


# --------------------------------------------------------------------------- #
# a run over http is the same run
# --------------------------------------------------------------------------- #


def test_a_run_writes_the_same_event_sequence_the_cli_writes(
    client: Any, runtime: Settings
) -> None:
    """The canned provider's shape is fixed, so the log is comparable exactly."""

    body = run_once(client)
    session_id = body["summary"]["session_id"]

    assert log_types(runtime, session_id) == [
        "session_created",
        "operation_started",
        "model_requested",
        "model_stream_completed",
        "assistant_message",
        "operation_finished",
    ]
    assert body["answer"]
    assert body["summary"]["provider"] == "fake"
    assert body["summary"]["model"] == "fake-model"


def test_a_run_into_a_named_session_continues_it(client: Any, runtime: Settings) -> None:
    first = run_once(client)
    session_id = first["summary"]["session_id"]

    response = client.post(f"/sessions/{session_id}/run", json={"prompt": "again"})

    assert response.status_code == 200, response.text
    assert response.json()["summary"]["session_id"] == session_id
    assert log_types(runtime, session_id).count("operation_started") == 2


def test_a_steer_message_is_recorded_for_the_run(client: Any, runtime: Settings) -> None:
    body = client.post("/runs", json={"prompt": "hello", "steer": ["look at notes.txt"]}).json()
    session_id = body["summary"]["session_id"]

    assert "queue_message_enqueued" in log_types(runtime, session_id)


def test_an_empty_prompt_is_refused_by_the_request_model(client: Any) -> None:
    """422 comes from the schema, so the service is never entered."""

    assert client.post("/runs", json={"prompt": ""}).status_code == 422


def test_an_unknown_field_is_refused_rather_than_ignored(client: Any) -> None:
    response = client.post("/runs", json={"prompt": "hi", "max_iterations": 999})

    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# reading a session
# --------------------------------------------------------------------------- #


def test_get_session_carries_the_state_hash(client: Any, runtime: Settings) -> None:
    session_id = run_once(client)["summary"]["session_id"]

    body = client.get(f"/sessions/{session_id}").json()

    with EventStore.from_settings(runtime) as store:
        expected = store.load_state(session_id).state_hash()
    assert body["state_hash"] == expected
    assert body["session_id"] == session_id


def test_get_session_on_an_unknown_id_is_a_404_with_the_exit_code(client: Any) -> None:
    response = client.get("/sessions/ses_absent")

    assert response.status_code == 404
    body = response.json()
    assert body["error"] == "session_not_found"
    assert body["exit_code"] == 9


def test_events_page_on_seq_and_report_the_next_cursor(client: Any, runtime: Settings) -> None:
    session_id = run_once(client)["summary"]["session_id"]

    first = client.get(f"/sessions/{session_id}/events", params={"limit": 2}).json()

    assert len(first["events"]) == 2
    assert first["next_after"] == first["events"][-1]["seq"]

    second = client.get(
        f"/sessions/{session_id}/events", params={"after": first["next_after"]}
    ).json()

    assert second["events"][0]["seq"] > first["next_after"]
    assert len(first["events"]) + len(second["events"]) == len(events_of(runtime, session_id))


def test_sessions_lists_what_ran(client: Any) -> None:
    session_id = run_once(client)["summary"]["session_id"]

    listed = [summary["session_id"] for summary in client.get("/sessions").json()["sessions"]]

    assert listed == [session_id]


# --------------------------------------------------------------------------- #
# the observability routes agree with the exporter
# --------------------------------------------------------------------------- #


def test_trace_matches_the_bundle_built_from_the_log(client: Any, runtime: Settings) -> None:
    session_id = run_once(client)["summary"]["session_id"]

    body = client.get(f"/sessions/{session_id}/trace").json()

    expected = build_bundle(events_of(runtime, session_id), session_id=session_id)
    assert body == expected.trace.summary()


def test_trace_as_text_renders_one_line_per_event(client: Any, runtime: Settings) -> None:
    session_id = run_once(client)["summary"]["session_id"]

    response = client.get(f"/sessions/{session_id}/trace", params={"text": "true"})

    assert response.headers["content-type"].startswith("text/plain")
    lines = response.text.strip().splitlines()
    assert len(lines) == len(events_of(runtime, session_id))


def test_audit_answers_the_model_question(client: Any, runtime: Settings) -> None:
    session_id = run_once(client)["summary"]["session_id"]

    body = client.get(f"/sessions/{session_id}/audit").json()

    assert body["summary"]["models"] == ["fake/fake-model"]
    assert [record["category"] for record in body["records"]] == ["model", "model"]


def test_export_carries_all_four_artefacts(client: Any, runtime: Settings) -> None:
    """The route serves the same bundle the CLI writes to disk."""

    session_id = run_once(client)["summary"]["session_id"]

    body = client.get(f"/sessions/{session_id}/export").json()

    assert set(body) == {"session_id", "trace", "audit", "metrics", "replay"}
    expected = build_bundle(events_of(runtime, session_id), session_id=session_id)
    assert body == expected.summary()
    assert set(expected.files()) == set(EXPORT_FILENAMES)


def test_export_of_a_finished_run_replays_clean(client: Any) -> None:
    session_id = run_once(client)["summary"]["session_id"]

    body = client.get(f"/sessions/{session_id}/export").json()

    assert body["replay"]["clean"] is True
    assert body["replay"]["problems"] == []
    assert body["metrics"]["model_requests"] == 1


def test_trace_on_an_unknown_session_is_a_404(client: Any) -> None:
    assert client.get("/sessions/ses_absent/trace").status_code == 404


# --------------------------------------------------------------------------- #
# lifecycle: compact, abort, resume
# --------------------------------------------------------------------------- #


def test_compact_records_a_context_compacted_event(client: Any, runtime: Settings) -> None:
    session_id = run_once(client)["summary"]["session_id"]

    response = client.post(f"/sessions/{session_id}/compact", json={"objective": "keep the answer"})

    assert response.status_code == 200, response.text
    assert response.json()["current_objective"] == "keep the answer"
    assert "context_compacted" in log_types(runtime, session_id)


def test_compact_on_an_unknown_session_is_a_404(client: Any) -> None:
    response = client.post("/sessions/ses_absent/compact", json={})

    assert response.status_code == 404


def test_abort_closes_the_unfinished_operation(client: Any, runtime: Settings) -> None:
    session_id = seed_crashed(runtime)

    body = client.post(f"/sessions/{session_id}/abort", json={"reason": "operator"}).json()

    assert body["aborted_operations"] == ["op_crashed"]
    assert "operation_aborted" in log_types(runtime, session_id)


def test_resume_owing_a_confirmation_is_a_409_not_a_500(client: Any, runtime: Settings) -> None:
    """A held call is the caller's decision, so a client must not retry into it."""

    session_id = seed_crashed(runtime)

    response = client.post(f"/sessions/{session_id}/resume", json={})

    assert response.status_code == 409
    body = response.json()
    assert body["needs_confirmation"] is True
    assert body["operations"][0]["confirm"] == ["call_write"]


def test_resume_with_the_confirmation_succeeds(client: Any, runtime: Settings) -> None:
    session_id = seed_crashed(runtime)

    response = client.post(f"/sessions/{session_id}/resume", json={"confirm": ["call_write"]})

    assert response.status_code == 200, response.text
    assert response.json()["needs_confirmation"] is False


def test_running_into_a_suspended_session_is_a_409(client: Any, runtime: Settings) -> None:
    """The recovery rule holds over HTTP: new work cannot bury an owed decision."""

    session_id = seed_crashed(runtime)
    client.post(f"/sessions/{session_id}/resume", json={})

    response = client.post(f"/sessions/{session_id}/run", json={"prompt": "carry on"})

    assert response.status_code == 409
    assert response.json()["error"] == "recovery_error"
