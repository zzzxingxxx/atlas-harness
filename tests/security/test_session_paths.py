"""Session ids become path segments, so they must not escape the data dir."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from atlas_harness.events import Event, EventStore, EventType
from atlas_harness.events.store import SESSIONS_DIRNAME
from atlas_harness.kernel import EventValidationError
from atlas_harness.kernel.ids import validate_session_id

StoreFactory = Callable[..., EventStore]

TRAVERSAL_IDS = [
    "../../etc/passwd",
    "..",
    ".",
    "a/b",
    "a\\b",
    "/absolute",
    "C:\\windows",
    "ses:1",
    "ses id",
    "-leading-dash",
    "_leading-underscore",
    "",
    "x" * 129,
    "ses\x00null",
    "ses\nnewline",
]


@pytest.mark.parametrize("session_id", TRAVERSAL_IDS)
def test_unsafe_session_ids_are_rejected(session_id: str) -> None:
    with pytest.raises(EventValidationError) as excinfo:
        validate_session_id(session_id)

    assert excinfo.value.details["session_id"] == session_id


@pytest.mark.parametrize("session_id", ["ses_a", "A1", "0", "a-b_c", "x" * 128])
def test_safe_session_ids_are_accepted(session_id: str) -> None:
    assert validate_session_id(session_id) == session_id


@pytest.mark.parametrize("session_id", ["../../escape", "a/b", ".."])
def test_log_path_refuses_traversal(store: EventStore, session_id: str) -> None:
    with pytest.raises(EventValidationError):
        store.log_path(session_id)


def test_log_paths_stay_inside_the_data_dir(store: EventStore, tmp_path: Path) -> None:
    path = store.log_path("ses_a").resolve()

    assert path.is_relative_to((tmp_path / SESSIONS_DIRNAME).resolve())


def test_append_refuses_a_traversal_session_id(store: EventStore, tmp_path: Path) -> None:
    event = Event.create(
        EventType.SESSION_CREATED,
        session_id="../../escape",
        seq=1,
        factory=store.ids,
    )

    with pytest.raises(EventValidationError):
        store.append(event)

    assert list(tmp_path.parent.glob("escape")) == []


def test_listing_skips_unsafe_directories(store: EventStore, tmp_path: Path) -> None:
    store.append_new(EventType.SESSION_CREATED, session_id="ses_ok")
    stray = tmp_path / SESSIONS_DIRNAME / ".hidden"
    stray.mkdir()
    (stray / "events.jsonl").write_text("{}\n", encoding="utf-8")

    assert store.list_session_ids() == ["ses_ok"]
