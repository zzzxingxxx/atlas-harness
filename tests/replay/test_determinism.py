"""Replay must be deterministic and independent of any model."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from atlas_harness.events import Event, EventStore, Reducer, replay

StoreFactory = Callable[..., EventStore]
SessionSeeder = Callable[..., list[Event]]


def test_identical_streams_project_identical_state(seed: SessionSeeder, store: EventStore) -> None:
    events = seed(store, "ses_a")
    twin = [Event.model_validate(event.to_json_dict()) for event in events]

    first = replay(events)
    second = replay(twin)

    assert first == second
    assert first.state_hash() == second.state_hash()


def test_reopening_the_store_reproduces_the_state(
    store_factory: StoreFactory, seed: SessionSeeder
) -> None:
    store = store_factory()
    seed(store, "ses_a")
    before = store.load_state("ses_a")
    store.close()

    after = store_factory().load_state("ses_a")

    assert after == before
    assert after.state_hash() == before.state_hash()


def test_every_prefix_matches_incremental_reduction(store: EventStore, seed: SessionSeeder) -> None:
    events = seed(store, "ses_a")
    reducer = Reducer("ses_a")

    for index, event in enumerate(events, start=1):
        incremental = reducer.apply(event)
        prefix = replay(events[:index])
        assert prefix.state_hash() == incremental.state_hash()


def test_state_is_rebuilt_without_the_index(
    store_factory: StoreFactory, seed: SessionSeeder, tmp_path: Path
) -> None:
    store = store_factory()
    seed(store, "ses_a")
    expected = store.load_state("ses_a").state_hash()
    store.close()

    for path in tmp_path.glob("index.sqlite3*"):
        path.unlink()

    assert store_factory().load_state("ses_a").state_hash() == expected


def test_replay_ignores_unrelated_sessions(store: EventStore, seed: SessionSeeder) -> None:
    seed(store, "ses_a")
    seed(store, "ses_b")

    state_a = store.load_state("ses_a")
    state_b = store.load_state("ses_b")

    assert state_a.session_id == "ses_a"
    assert state_b.session_id == "ses_b"
    assert state_a.event_count == state_b.event_count == 7
    assert state_a.state_hash() != state_b.state_hash()
