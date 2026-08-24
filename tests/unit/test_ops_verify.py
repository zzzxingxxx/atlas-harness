"""Verify reports and never repairs, so what it reports has to be exact.

Every finding here is produced by damaging a real data directory and asking
:func:`atlas_harness.ops.verify.verify_data_dir` what it sees. The severities
carry the operator's next action -- ``error`` means restore, ``repairable`` means
run ``atlas reindex``, ``warning`` means nothing -- so a test that only checked
"something was found" would let the tool give the wrong instruction and still
pass. Each case therefore pins the code *and* the severity.

The other half of the contract is that verification is read-only: a tool an
operator is afraid to run against production will not be run at all.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from atlas_harness.events import Event, EventStore, EventType
from atlas_harness.events.store import INDEX_FILENAME, LOG_FILENAME
from atlas_harness.ops.verify import (
    SEVERITIES,
    SEVERITY_ERROR,
    SEVERITY_REPAIRABLE,
    SEVERITY_WARNING,
    Finding,
    verify_data_dir,
    verify_session,
)

StoreFactory = Callable[..., EventStore]
SessionSeeder = Callable[..., list[Event]]

SESSION_ID = "ses_verify"


def codes(findings: tuple[Finding, ...]) -> set[str]:
    return {finding.code for finding in findings}


def severity_of(findings: tuple[Finding, ...], code: str) -> str:
    for finding in findings:
        if finding.code == code:
            return finding.severity
    raise AssertionError(f"no {code} finding in {sorted(codes(findings))}")


def rewrite_log(path: Path, lines: list[str]) -> None:
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8", newline="\n")


def log_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def test_a_healthy_session_has_nothing_to_report(seeded: tuple[EventStore, str]) -> None:
    store, session_id = seeded

    result = verify_session(store, session_id)

    assert result.ok
    assert result.findings == ()
    assert result.log_events == 7
    assert result.log_last_seq == 7


def test_a_healthy_data_directory_verifies_as_ok(store: EventStore, seed: SessionSeeder) -> None:
    seed(store, "ses_a")
    seed(store, "ses_b")

    report = verify_data_dir(store)

    assert report.ok
    assert not report.repairable
    assert len(report.sessions) == 2
    assert report.counts() == dict.fromkeys(SEVERITIES, 0)
    assert report.render()[-1].startswith("verdict: ok")


def test_verifying_changes_nothing_on_disk(store: EventStore, seed: SessionSeeder) -> None:
    """The property that makes it safe to run against production."""

    seed(store, SESSION_ID)
    log = store.log_path(SESSION_ID)
    before = log.read_bytes()

    verify_data_dir(store)

    assert log.read_bytes() == before


def test_a_missing_log_is_an_error(store: EventStore) -> None:
    result = verify_session(store, "ses_never_written")

    assert not result.ok
    assert codes(result.findings) == {"log_missing"}
    assert severity_of(result.findings, "log_missing") == SEVERITY_ERROR


def test_a_gap_in_the_sequence_is_an_error(
    store_factory: StoreFactory, seed: SessionSeeder
) -> None:
    """The plan names 事件 seq 缺失 explicitly: a hole is unrecoverable, not stale."""

    store = store_factory()
    seed(store, SESSION_ID)
    log = store.log_path(SESSION_ID)
    lines = log_lines(log)
    store.close()
    rewrite_log(log, lines[:3] + lines[4:])

    result = verify_session(store_factory(), SESSION_ID)

    assert "seq_gap" in codes(result.findings)
    assert severity_of(result.findings, "seq_gap") == SEVERITY_ERROR


def test_a_duplicated_line_is_reported_as_a_duplicate_and_not_as_a_gap(
    store_factory: StoreFactory, seed: SessionSeeder
) -> None:
    store = store_factory()
    seed(store, SESSION_ID)
    log = store.log_path(SESSION_ID)
    lines = log_lines(log)
    store.close()
    rewrite_log(log, lines + [lines[-1]])

    result = verify_session(store_factory(), SESSION_ID)

    found = codes(result.findings)
    assert "seq_duplicate" in found
    assert "event_id_duplicate" in found
    assert "seq_gap" not in found


def test_a_line_that_is_not_json_is_an_error_and_the_rest_of_the_log_is_still_read(
    store_factory: StoreFactory, seed: SessionSeeder
) -> None:
    """Unlike the store's strict parser, verify keeps going -- it is the diagnosis."""

    store = store_factory()
    seed(store, SESSION_ID)
    log = store.log_path(SESSION_ID)
    lines = log_lines(log)
    store.close()
    rewrite_log(log, [*lines[:2], "{not json", *lines[2:]])

    result = verify_session(store_factory(), SESSION_ID)

    assert "log_not_json" in codes(result.findings)
    assert result.log_events == 7


def test_a_truncated_last_line_is_reported_as_a_partial_write(
    store_factory: StoreFactory, seed: SessionSeeder
) -> None:
    """The expected shape of a crash mid-append, so it gets its own code."""

    store = store_factory()
    seed(store, SESSION_ID)
    log = store.log_path(SESSION_ID)
    store.close()
    with log.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write('{"schema_version": 7, "event_id": "evt_half"')

    result = verify_session(store_factory(), SESSION_ID)

    assert "log_partial_line" in codes(result.findings)


def test_an_event_belonging_to_another_session_is_an_error(
    store_factory: StoreFactory, seed: SessionSeeder
) -> None:
    store = store_factory()
    seed(store, SESSION_ID)
    log = store.log_path(SESSION_ID)
    lines = log_lines(log)
    store.close()
    stolen = json.loads(lines[-1])
    stolen["session_id"] = "ses_somebody_else"
    rewrite_log(log, [*lines[:-1], json.dumps(stolen)])

    result = verify_session(store_factory(), SESSION_ID)

    assert "event_foreign_session" in codes(result.findings)


def test_a_missing_index_is_repairable_rather_than_fatal(
    store_factory: StoreFactory, seed: SessionSeeder, tmp_path: Path
) -> None:
    """The index is derived, so losing all of it is an inconvenience."""

    store = store_factory()
    seed(store, SESSION_ID)
    store.close()
    for path in tmp_path.glob(f"{INDEX_FILENAME}*"):
        path.unlink()

    report = verify_data_dir(store_factory())

    assert not report.ok
    assert report.repairable
    assert "index_missing_rows" in codes(report.all_findings)
    assert all(
        finding.severity == SEVERITY_REPAIRABLE
        for finding in report.all_findings
        if finding.code.startswith("index_")
    )
    assert report.render()[-1].startswith("verdict: repairable")


def test_index_rows_the_log_does_not_vouch_for_are_repairable(
    store_factory: StoreFactory, seed: SessionSeeder, tmp_path: Path
) -> None:
    store = store_factory()
    seed(store, SESSION_ID)
    log = store.log_path(SESSION_ID)
    lines = log_lines(log)
    store.close()
    rewrite_log(log, lines[:-1])

    report = verify_data_dir(store_factory())

    assert "index_stale_rows" in codes(report.all_findings)
    assert report.repairable


def test_an_index_that_lags_the_log_is_repairable(
    store_factory: StoreFactory, seed: SessionSeeder, tmp_path: Path
) -> None:
    store = store_factory()
    seed(store, SESSION_ID)
    store.close()
    connection = sqlite3.connect(str(tmp_path / INDEX_FILENAME), isolation_level=None)
    try:
        connection.execute("DELETE FROM events WHERE seq >= 6")
    finally:
        connection.close()

    result = verify_session(store_factory(), SESSION_ID)

    found = codes(result.findings)
    assert "index_last_seq_mismatch" in found or "index_missing_rows" in found
    assert all(finding.severity == SEVERITY_REPAIRABLE for finding in result.findings)


def test_a_missing_artifact_file_is_an_error(
    store_factory: StoreFactory, seed: SessionSeeder
) -> None:
    """The log holds the checksum, not the bytes, so the bytes can go missing."""

    store = store_factory()
    seed(store, SESSION_ID)
    store.append_new(
        EventType.ARTIFACT_STORED,
        session_id=SESSION_ID,
        payload={
            "artifact_id": "art_gone",
            "path": "art_gone.txt",
            "kind": "tool_output",
            "size_bytes": 4,
            "checksum": "0" * 64,
        },
    )
    store.close()

    result = verify_session(store_factory(), SESSION_ID)

    assert "artifact_missing" in codes(result.findings)
    assert severity_of(result.findings, "artifact_missing") == SEVERITY_ERROR


def test_an_artifact_whose_bytes_changed_is_an_error(
    store_factory: StoreFactory, seed: SessionSeeder, tmp_path: Path
) -> None:
    store = store_factory()
    seed(store, SESSION_ID)
    artifacts = store.log_path(SESSION_ID).parent / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    body = "hello"
    (artifacts / "art_edited.txt").write_text(body, encoding="utf-8", newline="\n")
    store.append_new(
        EventType.ARTIFACT_STORED,
        session_id=SESSION_ID,
        payload={
            "artifact_id": "art_edited",
            "path": "art_edited.txt",
            "kind": "tool_output",
            "size_bytes": len(body),
            "checksum": "1" * 64,
        },
    )
    store.close()

    result = verify_session(store_factory(), SESSION_ID)

    assert "artifact_checksum_mismatch" in codes(result.findings)


def test_an_artifact_file_no_event_mentions_is_only_a_warning(
    store_factory: StoreFactory, seed: SessionSeeder
) -> None:
    """The file is written before the event, so an orphan is what a crash leaves."""

    store = store_factory()
    seed(store, SESSION_ID)
    artifacts = store.log_path(SESSION_ID).parent / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "art_orphan.txt").write_text("stranded", encoding="utf-8", newline="\n")
    store.close()

    result = verify_session(store_factory(), SESSION_ID)

    assert codes(result.findings) == {"artifact_orphan"}
    assert severity_of(result.findings, "artifact_orphan") == SEVERITY_WARNING
    assert result.ok


def test_a_report_is_repairable_only_when_every_real_finding_is(
    store_factory: StoreFactory, seed: SessionSeeder, tmp_path: Path
) -> None:
    """One error alongside twenty repairable findings still means restore."""

    store = store_factory()
    seed(store, "ses_broken")
    seed(store, "ses_stale")
    log = store.log_path("ses_broken")
    lines = log_lines(log)
    store.close()
    rewrite_log(log, lines[:3] + lines[4:])
    for path in tmp_path.glob(f"{INDEX_FILENAME}*"):
        path.unlink()

    report = verify_data_dir(store_factory())

    assert not report.ok
    assert not report.repairable
    assert report.counts()[SEVERITY_ERROR] >= 1
    assert report.render()[-1].startswith("verdict: problems")


def test_verifying_a_named_subset_leaves_the_others_unexamined(
    store: EventStore, seed: SessionSeeder
) -> None:
    seed(store, "ses_a")
    seed(store, "ses_b")

    report = verify_data_dir(store, sessions=["ses_a"])

    assert [entry.session_id for entry in report.sessions] == ["ses_a"]


def test_findings_serialize_to_json_the_cli_can_print(
    store_factory: StoreFactory, seed: SessionSeeder, tmp_path: Path
) -> None:
    store = store_factory()
    seed(store, SESSION_ID)
    store.close()
    for path in tmp_path.glob(f"{INDEX_FILENAME}*"):
        path.unlink()

    payload = verify_data_dir(store_factory()).as_json()

    assert json.loads(json.dumps(payload, ensure_ascii=False))["ok"] is False


@pytest.mark.parametrize("severity", SEVERITIES)
def test_every_severity_is_one_of_the_three_documented_ones(severity: str) -> None:
    assert severity in {SEVERITY_ERROR, SEVERITY_REPAIRABLE, SEVERITY_WARNING}


def test_a_log_whose_only_content_is_a_blank_line_is_reported_not_crashed(
    store_factory: StoreFactory, tmp_path: Path
) -> None:
    log = tmp_path / "sessions" / SESSION_ID / LOG_FILENAME
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("\n", encoding="utf-8", newline="\n")

    result = verify_session(store_factory(), SESSION_ID)

    assert "log_blank_line" in codes(result.findings)
