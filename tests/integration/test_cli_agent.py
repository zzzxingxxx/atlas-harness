"""The CLI's ``run``, ``messages`` and ``trace`` commands over a real store.

These tests drive the whole stack the way an operator does: a shell command, an
event log on disk, and nothing stubbed except the provider. The fake provider
needs no key and no network, so the M3 acceptance path runs on a fresh checkout.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from atlas_harness.config import Settings
from atlas_harness.events import EventStore, EventType
from atlas_harness.model.catalog import register_provider, unregister_provider
from atlas_harness.model.providers.fake import FakeAdapter, text_turn, tool_call_turn
from atlas_harness.transport.cli import app

runner = CliRunner()


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace with a file to read and an empty event log beside it."""

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("the answer is 42\n", encoding="utf-8")
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ATLAS_MODEL_PROVIDER", "fake")
    monkeypatch.setenv("ATLAS_MODEL_NAME", "fake-model")
    return workspace


@pytest.fixture
def scripted() -> Iterator[list[FakeAdapter]]:
    """Register a provider whose adapter reads a file, then answers.

    ``--provider fake`` builds the canned adapter, which never asks for a tool.
    Registering a scripted factory under its own name is the supported seam for
    driving the full loop through the real CLI, and it is undone afterwards so
    the provider table is left as it was found.
    """

    built: list[FakeAdapter] = []

    def factory(settings: Settings) -> FakeAdapter:
        adapter = FakeAdapter(
            [
                tool_call_turn("read_file", {"path": "notes.txt"}),
                text_turn("The file says the answer is 42."),
            ],
            model=settings.model_name,
            provider="scripted",
        )
        built.append(adapter)
        return adapter

    register_provider("scripted", factory)
    try:
        yield built
    finally:
        unregister_provider("scripted")


def read_log(tmp_path: Path, session_id: str) -> list[str]:
    """Reopen the log the CLI wrote and report its event types in order."""

    with EventStore(tmp_path / "runtime") as store:
        return [event.event_type.value for event in store.read_events(session_id)]


def session_ids(tmp_path: Path) -> list[str]:
    with EventStore(tmp_path / "runtime") as store:
        return store.list_session_ids()


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #


def test_run_answers_and_records_a_replayable_session(runtime: Path, tmp_path: Path) -> None:
    """The canned fake answers without tools; the log still explains the run."""

    result = runner.invoke(app, ["run", "say hello", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["stop_cause"] == "completed"
    assert payload["provider"] == "fake"
    assert payload["answer"]

    types = read_log(tmp_path, payload["session_id"])
    assert types == [
        "session_created",
        "operation_started",
        "model_requested",
        "model_stream_completed",
        "assistant_message",
        "operation_finished",
    ]


def test_run_human_output_names_the_session_and_the_stop_cause(runtime: Path) -> None:
    result = runner.invoke(app, ["run", "say hello"])

    assert result.exit_code == 0
    assert "session: ses_" in result.stdout
    assert "model: fake/fake-model" in result.stdout
    assert "stop: completed" in result.stdout


def test_run_continues_an_existing_session(runtime: Path, tmp_path: Path) -> None:
    first = json.loads(runner.invoke(app, ["run", "first", "--json"]).stdout)

    second = json.loads(
        runner.invoke(app, ["run", "second", "--session", first["session_id"], "--json"]).stdout
    )

    assert second["session_id"] == first["session_id"]
    assert second["operation_id"] != first["operation_id"]
    assert session_ids(tmp_path) == [first["session_id"]]
    assert read_log(tmp_path, first["session_id"]).count("operation_started") == 2


def test_run_records_a_steer_message_before_the_first_turn(runtime: Path, tmp_path: Path) -> None:
    payload = json.loads(
        runner.invoke(app, ["run", "be brief", "--steer", "one sentence", "--json"]).stdout
    )

    types = read_log(tmp_path, payload["session_id"])
    enqueued = types.index("queue_message_enqueued")
    consumed = types.index("queue_message_consumed")
    assert enqueued < consumed < types.index("model_requested")
    assert payload["pending_queue_messages"] == 0


def test_run_never_prints_the_api_key(runtime: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured key must not reach stdout, the log, or the summary."""

    monkeypatch.setenv("ATLAS_MODEL_API_KEY", "sk-do-not-leak-abcdefghijklmnop")

    result = runner.invoke(app, ["run", "say hello", "--json"])

    assert result.exit_code == 0
    assert "sk-do-not-leak" not in result.stdout


# --------------------------------------------------------------------------- #
# messages
# --------------------------------------------------------------------------- #


def test_messages_enqueues_and_then_reports_it_as_pending(runtime: Path, tmp_path: Path) -> None:
    session_id = json.loads(runner.invoke(app, ["run", "first", "--json"]).stdout)["session_id"]

    sent = json.loads(
        runner.invoke(app, ["messages", session_id, "--send", "look again", "--json"]).stdout
    )

    assert sent["enqueued"]
    assert sent["pending"]["steer"] == 1
    listed = json.loads(runner.invoke(app, ["messages", session_id, "--json"]).stdout)
    assert listed["enqueued"] is None
    assert listed["pending"]["steer"] == 1


def test_messages_can_target_the_follow_up_queue(runtime: Path) -> None:
    session_id = json.loads(runner.invoke(app, ["run", "first", "--json"]).stdout)["session_id"]

    payload = json.loads(
        runner.invoke(
            app,
            ["messages", session_id, "--send", "also check b", "--queue", "follow_up", "--json"],
        ).stdout
    )

    assert payload["pending"] == {"steer": 0, "follow_up": 1, "next_run": 0}


def test_a_queued_message_is_consumed_by_the_next_run(runtime: Path, tmp_path: Path) -> None:
    """This is the durable half of steering: enqueue now, consumed next turn."""

    session_id = json.loads(runner.invoke(app, ["run", "first", "--json"]).stdout)["session_id"]
    runner.invoke(app, ["messages", session_id, "--send", "be terse", "--json"])

    runner.invoke(app, ["run", "second", "--session", session_id, "--json"])

    remaining = json.loads(runner.invoke(app, ["messages", session_id, "--json"]).stdout)
    assert remaining["pending"]["steer"] == 0


def test_messages_for_an_unknown_session_exits_with_a_structured_error(
    runtime: Path,
) -> None:
    result = runner.invoke(app, ["messages", "ses_missing", "--send", "hi", "--json"])

    assert result.exit_code != 0


# --------------------------------------------------------------------------- #
# trace
# --------------------------------------------------------------------------- #


def test_trace_lists_every_recorded_step_in_order(runtime: Path, tmp_path: Path) -> None:
    session_id = json.loads(runner.invoke(app, ["run", "say hello", "--json"]).stdout)["session_id"]

    result = runner.invoke(app, ["trace", session_id])

    assert result.exit_code == 0
    printed = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(printed) == len(read_log(tmp_path, session_id))
    assert "session_created" in printed[0]
    assert "operation_finished" in printed[-1]


def test_trace_counts_match_the_log(runtime: Path, tmp_path: Path) -> None:
    session_id = json.loads(runner.invoke(app, ["run", "say hello", "--json"]).stdout)["session_id"]

    payload = json.loads(runner.invoke(app, ["trace", session_id, "--json"]).stdout)

    logged = read_log(tmp_path, session_id)
    assert sum(payload["counts"].values()) == len(logged)
    assert payload["counts"]["model_requested"] == logged.count("model_requested")


def test_trace_of_an_empty_session_says_so(runtime: Path) -> None:
    result = runner.invoke(app, ["trace", "ses_absent"])

    assert result.exit_code == 0
    assert "no events" in result.stdout


def test_trace_does_not_print_tool_output_verbatim(runtime: Path, tmp_path: Path) -> None:
    """A trace is a timeline; the log keeps the payload, the trace keeps a summary."""

    session_id = json.loads(runner.invoke(app, ["run", "say hello", "--json"]).stdout)["session_id"]
    with EventStore(tmp_path / "runtime") as store:
        store.append_new(
            EventType.TOOL_RESULT,
            session_id=session_id,
            payload={
                "tool_name": "read_file",
                "call_id": "c1",
                "success": True,
                "output": {"content": "a-very-distinctive-secret-body"},
                "duration_ms": 3,
            },
        )

    result = runner.invoke(app, ["trace", session_id])

    assert result.exit_code == 0
    assert "a-very-distinctive-secret-body" not in result.stdout
    assert "read_file ok 3ms" in result.stdout


# --------------------------------------------------------------------------- #
# the M3 acceptance path: read a file, answer, replay
# --------------------------------------------------------------------------- #


def test_the_cli_reads_a_file_and_answers_the_question(
    runtime: Path, tmp_path: Path, scripted: list[FakeAdapter]
) -> None:
    """M3's completion condition, driven through the real command.

    The model asks for ``read_file``, the executor runs it inside the workspace,
    the result goes back, and the second turn answers. Every step is one event.
    """

    result = runner.invoke(
        app, ["run", "what is in notes.txt?", "--provider", "scripted", "--json"]
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["stop_cause"] == "completed"
    assert payload["tool_calls"] == 1
    assert payload["answer"] == "The file says the answer is 42."

    # The tool-calling turn carries no prose, so it writes no ``assistant_message``:
    # that event is conditional on the turn actually having text.
    assert read_log(tmp_path, payload["session_id"]) == [
        "session_created",
        "operation_started",
        "model_requested",
        "model_stream_completed",
        "tool_started",
        "tool_result",
        "model_requested",
        "model_stream_completed",
        "assistant_message",
        "operation_finished",
    ]

    # The file's contents reached the model, not just the tool.
    second_request = scripted[0].requests[1]
    assert "the answer is 42" in json.dumps(
        [message.model_dump(mode="json") for message in second_request.messages]
    )


def test_the_recorded_run_replays_to_the_same_state(
    runtime: Path, tmp_path: Path, scripted: list[FakeAdapter]
) -> None:
    """Replaying the log twice must land on the same hash: no model involved."""

    session_id = json.loads(
        runner.invoke(
            app, ["run", "what is in notes.txt?", "--provider", "scripted", "--json"]
        ).stdout
    )["session_id"]

    first = json.loads(runner.invoke(app, ["replay", session_id, "--json"]).stdout)
    second = json.loads(runner.invoke(app, ["replay", session_id, "--json"]).stdout)

    assert first["state_hash"] == second["state_hash"]
    assert first["event_count"] == len(read_log(tmp_path, session_id))


def test_the_trace_of_the_acceptance_run_shows_the_tool_step(
    runtime: Path, scripted: list[FakeAdapter]
) -> None:
    session_id = json.loads(
        runner.invoke(
            app, ["run", "what is in notes.txt?", "--provider", "scripted", "--json"]
        ).stdout
    )["session_id"]

    result = runner.invoke(app, ["trace", session_id])

    assert result.exit_code == 0
    assert "read_file" in result.stdout
    assert "tool_result" in result.stdout
