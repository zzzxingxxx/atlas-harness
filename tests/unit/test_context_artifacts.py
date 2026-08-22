"""Externalizing large tool output without losing it.

The property under test throughout: an artifact is an *additional* copy that lets
the prompt carry a reference. The event log keeps the output either way, so
nothing here may ever be the only place the bytes exist.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from atlas_harness.context.artifacts import (
    ARTIFACTS_DIRNAME,
    ArtifactRef,
    ArtifactStore,
)
from atlas_harness.events import EventStore, EventType

SESSION_ID = "ses_art"
OPERATION_ID = "op_art"

StoreFactory = Callable[..., EventStore]


@pytest.fixture
def store(store_factory: StoreFactory) -> EventStore:
    opened = store_factory()
    opened.append_new(
        EventType.SESSION_CREATED,
        session_id=SESSION_ID,
        payload={"title": "artifacts"},
    )
    opened.append_new(
        EventType.OPERATION_STARTED,
        session_id=SESSION_ID,
        operation_id=OPERATION_ID,
        payload={"name": "run"},
    )
    return opened


@pytest.fixture
def artifacts(store: EventStore) -> ArtifactStore:
    return ArtifactStore(store, inline_limit=64, preview_bytes=32)


def store_one(artifacts: ArtifactStore, value: object) -> ArtifactRef:
    return artifacts.store(
        value,
        session_id=SESSION_ID,
        operation_id=OPERATION_ID,
        tool_name="run_command",
        call_id="c1",
    )


# --------------------------------------------------------------------------- #
# the inline threshold
# --------------------------------------------------------------------------- #


def test_small_output_stays_inline(artifacts: ArtifactStore) -> None:
    """A reference costs context too, so below the limit it is not worth it."""

    assert artifacts.should_externalize("ok") is False


def test_large_output_is_externalized(artifacts: ArtifactStore) -> None:
    assert artifacts.should_externalize("x" * 500) is True


def test_the_threshold_measures_encoded_bytes(artifacts: ArtifactStore) -> None:
    """Multi-byte text counts what it costs on the wire, not character count."""

    assert artifacts.should_externalize("世界" * 10) is False
    assert artifacts.should_externalize("世界" * 40) is True


def test_a_structure_is_measured_serialized(artifacts: ArtifactStore) -> None:
    assert artifacts.should_externalize({"lines": ["x" * 200]}) is True


# --------------------------------------------------------------------------- #
# storing
# --------------------------------------------------------------------------- #


def test_storing_writes_the_file_and_the_event(artifacts: ArtifactStore, store: EventStore) -> None:
    reference = store_one(artifacts, "build log\n" * 100)

    path = artifacts.directory(SESSION_ID) / reference.path
    assert path.exists()
    assert path.parent.name == ARTIFACTS_DIRNAME

    stored = [
        event
        for event in store.read_events(SESSION_ID)
        if event.event_type is EventType.ARTIFACT_STORED
    ]
    assert len(stored) == 1
    payload = stored[0].payload.model_dump(mode="json")
    assert payload["artifact_id"] == reference.artifact_id
    assert payload["tool_name"] == "run_command"
    assert payload["call_id"] == "c1"


def test_the_recorded_size_is_the_artifacts_own_not_the_previews(
    artifacts: ArtifactStore,
) -> None:
    """Otherwise a reader cannot tell how much was set aside."""

    body = "y" * 5_000
    reference = store_one(artifacts, body)

    assert reference.size == len(body)
    assert len(reference.preview.encode("utf-8")) <= 32 + 32


def test_the_checksum_matches_the_bytes_on_disk(artifacts: ArtifactStore) -> None:
    reference = store_one(artifacts, "content " * 200)

    assert artifacts.verify(SESSION_ID, reference.artifact_id, reference.checksum) is True


def test_verify_fails_when_the_file_changed(artifacts: ArtifactStore) -> None:
    reference = store_one(artifacts, "content " * 200)
    path = artifacts.directory(SESSION_ID) / reference.path
    path.write_text("tampered", encoding="utf-8")

    assert artifacts.verify(SESSION_ID, reference.artifact_id, reference.checksum) is False


def test_verify_fails_when_the_file_is_missing(artifacts: ArtifactStore) -> None:
    assert artifacts.verify(SESSION_ID, "art_nope", "deadbeef") is False


def test_reading_a_missing_artifact_returns_none(artifacts: ArtifactStore) -> None:
    assert artifacts.read(SESSION_ID, "art_nope") is None


def test_an_artifact_round_trips(artifacts: ArtifactStore) -> None:
    body = {"lines": [f"line {index}" for index in range(200)]}
    reference = store_one(artifacts, body)

    loaded = artifacts.read(SESSION_ID, reference.artifact_id)
    assert loaded is not None
    assert json.loads(loaded) == body


def test_each_artifact_gets_its_own_id(artifacts: ArtifactStore) -> None:
    first = store_one(artifacts, "a" * 500)
    second = store_one(artifacts, "a" * 500)

    assert first.artifact_id != second.artifact_id
    assert first.checksum == second.checksum, "identical content hashes identically"


# --------------------------------------------------------------------------- #
# redaction
# --------------------------------------------------------------------------- #


def test_secrets_are_redacted_before_touching_disk(artifacts: ArtifactStore) -> None:
    """An artifact file is as durable as the log, so a secret in one outlives
    every filter downstream of it."""

    reference = store_one(artifacts, "export API_KEY=sk-abcdefghijklmnopqrstuvwx\n" * 50)

    body = artifacts.read(SESSION_ID, reference.artifact_id)
    assert body is not None
    assert "sk-abcdefghijklmnopqrstuvwx" not in body
    assert "[redacted]" in body


def test_secrets_are_redacted_inside_nested_structures(artifacts: ArtifactStore) -> None:
    reference = store_one(
        artifacts,
        {"env": [{"token": "ghp_" + "a" * 30}], "padding": "x" * 200},
    )

    body = artifacts.read(SESSION_ID, reference.artifact_id)
    assert body is not None
    assert "ghp_" + "a" * 30 not in body


def test_the_preview_is_redacted_too(artifacts: ArtifactStore) -> None:
    """The preview is what actually reaches the model."""

    reference = store_one(artifacts, "api_key=sk-abcdefghijklmnopqrstuvwx " * 50)

    assert "sk-abcdefghijklmnopqrstuvwx" not in reference.preview


# --------------------------------------------------------------------------- #
# what the model sees
# --------------------------------------------------------------------------- #


def test_the_context_value_carries_the_id_and_a_preview(artifacts: ArtifactStore) -> None:
    reference = store_one(artifacts, "log " * 500)

    value = reference.as_context_value()

    assert value["artifact_id"] == reference.artifact_id
    assert value["size"] == reference.size
    assert value["preview"] == reference.preview
    assert "preserved" in str(value["note"]), "the model should know the data still exists"


def test_the_context_value_is_far_smaller_than_the_artifact(
    artifacts: ArtifactStore,
) -> None:
    """The whole point: the prompt carries a reference, not a megabyte."""

    reference = store_one(artifacts, "x" * 100_000)

    rendered = json.dumps(reference.as_context_value())
    assert len(rendered) < 1_000
    assert reference.size == 100_000


# --------------------------------------------------------------------------- #
# ordering
# --------------------------------------------------------------------------- #


def test_the_file_is_written_before_the_event(artifacts: ArtifactStore, store: EventStore) -> None:
    """Same ordering snapshots use.

    A crash between the two leaves an orphan file nothing points at, which is
    harmless. The reverse would leave an event naming a file that never landed.
    """

    reference = store_one(artifacts, "z" * 500)

    events = store.read_events(SESSION_ID)
    announced = events[-1]
    assert announced.event_type is EventType.ARTIFACT_STORED
    # The file exists at the moment the event is readable, which is the property
    # the ordering buys.
    assert (artifacts.directory(SESSION_ID) / reference.path).exists()


def test_the_projection_tracks_the_artifact_id(artifacts: ArtifactStore, store: EventStore) -> None:
    reference = store_one(artifacts, "q" * 500)

    state = store.load_state(SESSION_ID)
    assert state.artifacts == [reference.artifact_id]
    assert state.operations[OPERATION_ID].artifact_ids == [reference.artifact_id]


def test_an_invalid_session_id_is_refused(artifacts: ArtifactStore) -> None:
    """The id becomes a directory path, so it is validated first."""

    from atlas_harness.kernel.errors import EventValidationError

    with pytest.raises(EventValidationError):
        artifacts.directory("../escape")
