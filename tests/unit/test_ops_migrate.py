"""The index is a cache, and a rebuild is always the log's version of events.

The plan names ``SQLite/JSONL 不一致`` as a risk with a defined response: rebuild
the index from the log. These tests hold that response to three properties. It
repairs drift in either direction -- rows the index is missing and rows the log
does not vouch for. It is idempotent, because a repair tool nobody dares run
twice is a repair tool nobody runs. And it refuses a log it cannot read rather
than indexing the readable prefix, which would produce an index that looks
complete and is not.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

from atlas_harness.events import Event, EventStore
from atlas_harness.events.store import INDEX_FILENAME
from atlas_harness.ops.migrate import rebuild_index, rebuild_index_at, reindex_session

StoreFactory = Callable[..., EventStore]
SessionSeeder = Callable[..., list[Event]]

SESSION_ID = "ses_migrate"


def drop_index(tmp_path: Path) -> None:
    for path in tmp_path.glob(f"{INDEX_FILENAME}*"):
        path.unlink()


def rewrite_log(path: Path, lines: list[str]) -> None:
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8", newline="\n")


def log_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def test_a_consistent_session_is_left_alone(seeded: tuple[EventStore, str]) -> None:
    store, session_id = seeded

    result = reindex_session(store, session_id)

    assert result.ok
    assert not result.changed
    assert result.events == 7
    assert "already consistent" in result.render()


def test_an_empty_index_is_rebuilt_from_the_logs(
    store_factory: StoreFactory, seed: SessionSeeder, tmp_path: Path
) -> None:
    store = store_factory()
    seed(store, "ses_a")
    seed(store, "ses_b")
    expected = store.load_state("ses_a").state_hash()
    store.close()
    drop_index(tmp_path)

    report = rebuild_index(store_factory())

    assert report.ok
    assert len(report.changed) == 2
    assert all(entry.inserted == 7 and entry.deleted == 0 for entry in report.sessions)
    assert store_factory().load_state("ses_a").state_hash() == expected


def test_rebuilding_twice_changes_nothing_the_second_time(
    store_factory: StoreFactory, seed: SessionSeeder, tmp_path: Path
) -> None:
    """Idempotence is what makes this safe to run against production."""

    store = store_factory()
    seed(store, SESSION_ID)
    store.close()
    drop_index(tmp_path)

    first = rebuild_index(store_factory())
    second = rebuild_index(store_factory())

    assert first.ok and second.ok
    assert len(first.changed) == 1
    assert second.changed == ()


def test_rows_the_log_does_not_vouch_for_are_deleted(
    store_factory: StoreFactory, seed: SessionSeeder
) -> None:
    """The interesting direction: the index carried events the log never recorded."""

    store = store_factory()
    seed(store, SESSION_ID)
    log = store.log_path(SESSION_ID)
    lines = log_lines(log)
    store.close()
    rewrite_log(log, lines[:5])

    report = rebuild_index(store_factory())
    entry = report.sessions[0]

    assert report.ok
    assert entry.changed
    assert entry.deleted == 2
    assert entry.events == 5
    assert store_factory().index.last_seq(SESSION_ID) == 5


def test_an_event_id_that_moved_to_a_different_seq_does_not_collide(
    store_factory: StoreFactory, seed: SessionSeeder
) -> None:
    """Delete-then-insert in one transaction, or the row collides with its old self."""

    store = store_factory()
    seed(store, SESSION_ID)
    log = store.log_path(SESSION_ID)
    lines = log_lines(log)
    store.close()

    swapped = [json.loads(line) for line in lines]
    swapped[5]["seq"], swapped[6]["seq"] = swapped[6]["seq"], swapped[5]["seq"]
    swapped[5]["idempotency_key"], swapped[6]["idempotency_key"] = (
        swapped[6]["idempotency_key"],
        swapped[5]["idempotency_key"],
    )
    swapped.sort(key=lambda event: event["seq"])
    rewrite_log(log, [json.dumps(event) for event in swapped])

    result = reindex_session(store_factory(), SESSION_ID)

    assert result.ok
    assert result.changed
    assert result.events == 7


def test_a_log_with_a_gap_is_refused_rather_than_partially_indexed(
    store_factory: StoreFactory, seed: SessionSeeder, tmp_path: Path
) -> None:
    store = store_factory()
    seed(store, SESSION_ID)
    log = store.log_path(SESSION_ID)
    lines = log_lines(log)
    store.close()
    rewrite_log(log, lines[:3] + lines[4:])
    drop_index(tmp_path)

    report = rebuild_index(store_factory())
    entry = report.sessions[0]

    assert not report.ok
    assert not entry.ok
    assert entry.reason.startswith("log does not parse")
    assert entry.events == 0
    assert "skipped" in entry.render()


def test_a_session_with_no_log_is_reported_and_not_invented(store: EventStore) -> None:
    result = reindex_session(store, "ses_absent")

    assert not result.ok
    assert result.reason == "no log on disk"


def test_one_unreadable_session_does_not_mark_the_others_rebuilt(
    store_factory: StoreFactory, seed: SessionSeeder, tmp_path: Path
) -> None:
    """A partial success must not read as a pass to whatever gates on this."""

    store = store_factory()
    seed(store, "ses_good")
    seed(store, "ses_bad")
    log = store.log_path("ses_bad")
    lines = log_lines(log)
    store.close()
    rewrite_log(log, [*lines[:3], "{broken", *lines[3:]])
    drop_index(tmp_path)

    report = rebuild_index(store_factory())

    assert not report.ok
    assert len(report.skipped) == 1
    assert {entry.session_id for entry in report.changed} == {"ses_good"}
    assert report.render()[-1].startswith("verdict: incomplete")


def test_sessions_are_discovered_from_disk_and_not_from_the_index(
    store_factory: StoreFactory, seed: SessionSeeder, tmp_path: Path
) -> None:
    """A session the index has entirely forgotten is exactly the one to recover."""

    store = store_factory()
    seed(store, "ses_forgotten")
    store.close()
    connection = sqlite3.connect(str(tmp_path / INDEX_FILENAME), isolation_level=None)
    try:
        connection.execute("DELETE FROM events")
        connection.execute("DELETE FROM sessions")
    finally:
        connection.close()

    report = rebuild_index(store_factory())

    assert [entry.session_id for entry in report.sessions] == ["ses_forgotten"]
    assert report.ok


def test_rebuilding_a_named_subset_leaves_the_others_alone(
    store_factory: StoreFactory, seed: SessionSeeder, tmp_path: Path
) -> None:
    store = store_factory()
    seed(store, "ses_a")
    seed(store, "ses_b")
    store.close()
    drop_index(tmp_path)

    report = rebuild_index(store_factory(), sessions=["ses_a"])

    assert [entry.session_id for entry in report.sessions] == ["ses_a"]
    assert store_factory().index.last_seq("ses_b") == 0


def test_rebuild_index_at_opens_and_closes_its_own_store(
    store_factory: StoreFactory, seed: SessionSeeder, tmp_path: Path
) -> None:
    """The restore path has logs on disk and no store to reach them with."""

    store = store_factory()
    seed(store, SESSION_ID)
    store.close()
    drop_index(tmp_path)

    report = rebuild_index_at(tmp_path)

    assert report.ok
    assert len(report.changed) == 1
    assert report.data_dir == str(tmp_path)


def test_the_log_is_never_written_to(
    store_factory: StoreFactory, seed: SessionSeeder, tmp_path: Path
) -> None:
    store = store_factory()
    seed(store, SESSION_ID)
    log = store.log_path(SESSION_ID)
    before = log.read_bytes()
    store.close()
    drop_index(tmp_path)

    rebuild_index(store_factory())

    assert log.read_bytes() == before


def test_the_report_serializes_for_the_cli(
    store_factory: StoreFactory, seed: SessionSeeder, tmp_path: Path
) -> None:
    store = store_factory()
    seed(store, SESSION_ID)
    store.close()
    drop_index(tmp_path)

    payload = json.loads(json.dumps(rebuild_index(store_factory()).as_json()))

    assert payload["ok"] is True
    assert payload["changed"] == 1
    assert payload["skipped"] == 0
