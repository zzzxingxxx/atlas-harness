"""An unverified backup is a belief, so every one of these tests checks the copy.

The plan's release step is 备份并校验 SQLite、JSONL 和 artifacts, and the second
verb is the one under test here. A backup tool that copies bytes is easy; what
these tests pin is the part that makes the copy trustworthy -- the manifest is
written last so an interrupted backup is recognisably incomplete, every listed
file is re-hashed before a restore writes anything, and a restored log is folded
again on today's build so a backward-compatibility break shows up as a failed
restore rather than as a wrong answer weeks later.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from atlas_harness.events import Event, EventStore, EventType
from atlas_harness.events.models import CURRENT_SCHEMA_VERSION
from atlas_harness.events.store import INDEX_FILENAME
from atlas_harness.kernel.errors import ConfigurationError
from atlas_harness.ops.backup import (
    MANIFEST_FILENAME,
    create_backup,
    read_manifest,
    restore_backup,
    verify_backup,
)

StoreFactory = Callable[..., EventStore]
SessionSeeder = Callable[..., list[Event]]

SESSION_ID = "ses_backup"


def store_artifact(store: EventStore, session_id: str, body: str) -> Path:
    """Write an artifact the way the runtime does: file first, then the event."""

    directory = store.log_path(session_id).parent / "artifacts"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "art_backup.txt"
    path.write_text(body, encoding="utf-8", newline="\n")
    store.append_new(
        EventType.ARTIFACT_STORED,
        session_id=session_id,
        payload={
            "artifact_id": "art_backup",
            "path": path.name,
            "kind": "tool_output",
            "size_bytes": len(body),
            "checksum": "0" * 64,
        },
    )
    return path


def test_a_backup_lists_every_log_artifact_and_the_index(
    store_factory: StoreFactory, seed: SessionSeeder, tmp_path: Path
) -> None:
    store = store_factory()
    seed(store, SESSION_ID)
    store_artifact(store, SESSION_ID, "evidence")
    destination = tmp_path / "backup"

    manifest = create_backup(store, destination)

    assert [entry.session_id for entry in manifest.sessions] == [SESSION_ID]
    entry = manifest.sessions[0]
    assert entry.events == 8
    assert entry.last_seq == 8
    assert entry.foldable
    assert entry.state_hash
    assert len(entry.artifacts) == 1
    assert manifest.index is not None
    assert manifest.current_schema_version == CURRENT_SCHEMA_VERSION


def test_the_manifest_is_the_last_file_written(
    store_factory: StoreFactory, seed: SessionSeeder, tmp_path: Path
) -> None:
    """Which is what makes an interrupted backup refusable instead of restorable."""

    store = store_factory()
    seed(store, SESSION_ID)
    destination = tmp_path / "backup"
    create_backup(store, destination)
    manifest_path = destination / MANIFEST_FILENAME
    others = [path for path in destination.rglob("*") if path.is_file() and path != manifest_path]

    assert others
    assert all(path.stat().st_mtime_ns <= manifest_path.stat().st_mtime_ns for path in others)


def test_a_directory_with_no_manifest_is_not_a_backup(tmp_path: Path) -> None:
    empty = tmp_path / "not-a-backup"
    empty.mkdir()

    with pytest.raises(ConfigurationError):
        read_manifest(empty)


def test_a_manifest_that_is_not_json_is_refused(tmp_path: Path) -> None:
    directory = tmp_path / "backup"
    directory.mkdir()
    (directory / MANIFEST_FILENAME).write_text("{oops", encoding="utf-8")

    with pytest.raises(ConfigurationError):
        read_manifest(directory)


def test_a_manifest_round_trips_through_json(
    store_factory: StoreFactory, seed: SessionSeeder, tmp_path: Path
) -> None:
    store = store_factory()
    seed(store, SESSION_ID)
    destination = tmp_path / "backup"

    created = create_backup(store, destination)
    loaded = read_manifest(destination)

    assert loaded == created


def test_a_fresh_backup_verifies(
    store_factory: StoreFactory, seed: SessionSeeder, tmp_path: Path
) -> None:
    store = store_factory()
    seed(store, SESSION_ID)
    destination = tmp_path / "backup"
    manifest = create_backup(store, destination)

    verification = verify_backup(destination)

    assert verification.ok
    assert verification.checked == len(manifest.files())
    assert verification.missing == ()
    assert verification.corrupt == ()
    assert verification.unlisted == ()


def test_an_edited_log_in_the_backup_is_corrupt(
    store_factory: StoreFactory, seed: SessionSeeder, tmp_path: Path
) -> None:
    store = store_factory()
    seed(store, SESSION_ID)
    destination = tmp_path / "backup"
    create_backup(store, destination)
    copied = destination / "sessions" / SESSION_ID / "events.jsonl"
    with copied.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("junk\n")

    verification = verify_backup(destination)

    assert not verification.ok
    assert verification.corrupt == (f"sessions/{SESSION_ID}/events.jsonl",)
    assert verification.render()[-1] == "verdict: damaged"


def test_a_deleted_file_in_the_backup_is_missing(
    store_factory: StoreFactory, seed: SessionSeeder, tmp_path: Path
) -> None:
    store = store_factory()
    seed(store, SESSION_ID)
    destination = tmp_path / "backup"
    create_backup(store, destination)
    (destination / INDEX_FILENAME).unlink()

    verification = verify_backup(destination)

    assert not verification.ok
    assert verification.missing == (INDEX_FILENAME,)


def test_a_file_the_manifest_does_not_mention_is_only_unlisted(
    store_factory: StoreFactory, seed: SessionSeeder, tmp_path: Path
) -> None:
    """Two backups written on top of each other, and the leftovers are uncovered."""

    store = store_factory()
    seed(store, SESSION_ID)
    destination = tmp_path / "backup"
    create_backup(store, destination)
    (destination / "sessions" / "ses_leftover").mkdir(parents=True)
    (destination / "sessions" / "ses_leftover" / "events.jsonl").write_text("{}", encoding="utf-8")

    verification = verify_backup(destination)

    assert verification.ok
    assert verification.unlisted == ("sessions/ses_leftover/events.jsonl",)


def test_a_restore_reproduces_the_state_hash_it_recorded(
    store_factory: StoreFactory, seed: SessionSeeder, tmp_path: Path
) -> None:
    """The compatibility answer the plan asks for: the log still folds the same."""

    store = store_factory()
    seed(store, "ses_a")
    seed(store, "ses_b")
    store_artifact(store, "ses_a", "evidence")
    expected = store.load_state("ses_a").state_hash()
    destination = tmp_path / "backup"
    manifest = create_backup(store, destination)
    store.close()

    report = restore_backup(destination, tmp_path / "restored")

    assert report.compatible
    assert report.files_written == len(manifest.files())
    assert report.index_restored
    assert {entry.session_id for entry in report.sessions} == {"ses_a", "ses_b"}
    restored_a = next(entry for entry in report.sessions if entry.session_id == "ses_a")
    assert restored_a.state_hash == expected
    assert restored_a.matches
    assert report.render()[-1] == "verdict: compatible"


def test_a_restored_directory_is_a_working_data_directory(
    store_factory: StoreFactory, seed: SessionSeeder, tmp_path: Path
) -> None:
    store = store_factory()
    seed(store, SESSION_ID)
    expected = store.load_state(SESSION_ID).state_hash()
    create_backup(store, tmp_path / "backup")
    store.close()
    target = tmp_path / "restored"

    restore_backup(tmp_path / "backup", target)

    with EventStore(target) as reopened:
        assert reopened.load_state(SESSION_ID).state_hash() == expected
        assert reopened.list_session_ids() == [SESSION_ID]


def test_a_damaged_backup_is_never_written_over_good_bytes(
    store_factory: StoreFactory, seed: SessionSeeder, tmp_path: Path
) -> None:
    """Verify first, write second -- the order is the whole point."""

    store = store_factory()
    seed(store, SESSION_ID)
    destination = tmp_path / "backup"
    create_backup(store, destination)
    store.close()
    copied = destination / "sessions" / SESSION_ID / "events.jsonl"
    with copied.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("junk\n")
    target = tmp_path / "restored"

    with pytest.raises(ConfigurationError):
        restore_backup(destination, target)

    assert not target.exists()


def test_restoring_into_a_populated_directory_is_refused_without_force(
    store_factory: StoreFactory, seed: SessionSeeder, tmp_path: Path
) -> None:
    """Two histories of the same session id must never interleave by accident."""

    store = store_factory()
    seed(store, SESSION_ID)
    create_backup(store, tmp_path / "backup")
    store.close()
    target = tmp_path / "restored"
    target.mkdir()
    (target / "sessions").mkdir()

    with pytest.raises(ConfigurationError):
        restore_backup(tmp_path / "backup", target)

    report = restore_backup(tmp_path / "backup", target, force=True)

    assert report.compatible


def test_a_named_subset_backs_up_only_those_sessions(
    store_factory: StoreFactory, seed: SessionSeeder, tmp_path: Path
) -> None:
    store = store_factory()
    seed(store, "ses_a")
    seed(store, "ses_b")

    manifest = create_backup(store, tmp_path / "backup", sessions=["ses_a"])

    assert [entry.session_id for entry in manifest.sessions] == ["ses_a"]
    assert not (tmp_path / "backup" / "sessions" / "ses_b").exists()


def test_a_session_with_no_log_cannot_be_backed_up(store: EventStore, tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        create_backup(store, tmp_path / "backup", sessions=["ses_absent"])


def test_a_backup_without_the_index_still_restores_and_says_to_rebuild(
    store_factory: StoreFactory, seed: SessionSeeder, tmp_path: Path
) -> None:
    """The index is derived, so a backup missing it is still complete."""

    store = store_factory()
    seed(store, SESSION_ID)
    create_backup(store, tmp_path / "backup", include_index=False)
    store.close()

    report = restore_backup(tmp_path / "backup", tmp_path / "restored")

    assert report.compatible
    assert not report.index_restored
    assert any("atlas reindex" in note for note in report.notes)


def test_the_index_is_copied_through_sqlite_and_is_readable(
    store_factory: StoreFactory, seed: SessionSeeder, tmp_path: Path
) -> None:
    """A WAL database's bytes on disk are only half the story."""

    store = store_factory()
    seed(store, SESSION_ID)
    create_backup(store, tmp_path / "backup")

    connection = sqlite3.connect(str(tmp_path / "backup" / INDEX_FILENAME))
    try:
        rows = connection.execute(
            "SELECT count(*) FROM events WHERE session_id = ?", (SESSION_ID,)
        ).fetchone()
    finally:
        connection.close()

    assert rows[0] == 7


def test_a_log_that_does_not_fold_is_still_backed_up(
    store_factory: StoreFactory, seed: SessionSeeder, tmp_path: Path
) -> None:
    """It is the only copy of the damage, so the backup records the fact instead."""

    store = store_factory()
    seed(store, SESSION_ID)
    log = store.log_path(SESSION_ID)
    lines = log.read_text(encoding="utf-8").splitlines()
    store.close()
    log.write_text(
        "".join(f"{line}\n" for line in lines[:3] + lines[4:]), encoding="utf-8", newline="\n"
    )

    manifest = create_backup(store_factory(), tmp_path / "backup", sessions=[SESSION_ID])
    entry = manifest.sessions[0]

    assert not entry.foldable
    assert entry.state_hash == ""
    assert verify_backup(tmp_path / "backup").ok
    assert "log did not fold" in "\n".join(manifest.render())


def test_a_restored_log_that_folds_differently_fails_the_restore(
    store_factory: StoreFactory, seed: SessionSeeder, tmp_path: Path
) -> None:
    """Simulates the break by editing the recorded hash: the bytes are fine, so a
    mismatch can only mean this build reads that log differently."""

    store = store_factory()
    seed(store, SESSION_ID)
    destination = tmp_path / "backup"
    create_backup(store, destination)
    store.close()

    manifest_path = destination / MANIFEST_FILENAME
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["sessions"][0]["state_hash"] = "f" * 64
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    report = restore_backup(destination, tmp_path / "restored")

    assert not report.compatible
    assert report.sessions[0].matches is False
    assert report.render()[-1] == "verdict: incompatible"


def test_the_reports_serialize_for_the_cli(
    store_factory: StoreFactory, seed: SessionSeeder, tmp_path: Path
) -> None:
    store = store_factory()
    seed(store, SESSION_ID)
    manifest = create_backup(store, tmp_path / "backup")
    store.close()
    report = restore_backup(tmp_path / "backup", tmp_path / "restored")

    manifest_json = json.loads(json.dumps(manifest.as_json(), ensure_ascii=False))
    restore_json = json.loads(json.dumps(report.as_json(), ensure_ascii=False))
    verify_json = json.loads(json.dumps(verify_backup(tmp_path / "backup").as_json()))

    assert manifest_json["files"] == len(manifest.files())
    assert manifest_json["bytes"] > 0
    assert restore_json["compatible"] is True
    assert verify_json["ok"] is True
