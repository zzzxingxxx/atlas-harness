"""The six operator commands, exercised the way an operator reaches them.

Each one is a report, so what is asserted here is mostly the exit code. That is
deliberate: ``atlas verify`` returning 1 means run ``atlas reindex``, and 8 means
the log itself is damaged and only a restore will help. An operator following a
runbook, or a CI job gating on the release check, reads the number rather than
the prose -- so a command that printed the right words and exited 0 anyway would
be worse than one that crashed.

The whole flow is here in order as well, because verify, reindex, backup and
restore are individually plausible and only useful if a damaged directory can be
walked back to a healthy one using nothing but them.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from atlas_harness.events import Event, EventStore
from atlas_harness.events.store import INDEX_FILENAME
from atlas_harness.ops.backup import MANIFEST_FILENAME
from atlas_harness.transport.cli import app, main

runner = CliRunner()
StoreFactory = Callable[..., EventStore]
SessionSeeder = Callable[..., list[Event]]

SESSION_ID = "ses_ops"


@pytest.fixture
def data_dir(
    store_factory: StoreFactory,
    seed: SessionSeeder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    store = store_factory(tmp_path / "runtime")
    seed(store, SESSION_ID)
    seed(store, "ses_other")
    store.close()
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(tmp_path / "ws"))
    return tmp_path / "runtime"


def drop_index(data_dir: Path) -> None:
    for path in data_dir.glob(f"{INDEX_FILENAME}*"):
        path.unlink()


def break_log(data_dir: Path, session_id: str) -> None:
    """Remove one line from the middle, which is the one damage nothing repairs."""

    log = data_dir / "sessions" / session_id / "events.jsonl"
    lines = log.read_text(encoding="utf-8").splitlines()
    log.write_text(
        "".join(f"{line}\n" for line in lines[:3] + lines[4:]), encoding="utf-8", newline="\n"
    )


def test_verify_exits_zero_on_a_healthy_directory(data_dir: Path) -> None:
    result = runner.invoke(app, ["verify"])

    assert result.exit_code == 0
    assert "verdict: ok (0 error, 0 repairable, 0 warning)" in result.stdout


def test_verify_json_is_machine_readable(data_dir: Path) -> None:
    result = runner.invoke(app, ["verify", "--json"])

    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload["ok"] is True
    assert len(payload["sessions"]) == 2


def test_verify_exits_one_when_only_derived_state_drifted(data_dir: Path) -> None:
    """Exit 1 is the instruction to reindex, and the index is always rebuildable."""

    drop_index(data_dir)

    result = runner.invoke(app, ["verify"])

    assert result.exit_code == 1
    assert "verdict: repairable" in result.stdout


def test_verify_exits_eight_when_the_log_itself_is_damaged(data_dir: Path) -> None:
    """Exit 8 is the instruction to restore: no tool here can invent a lost event."""

    break_log(data_dir, SESSION_ID)

    result = runner.invoke(app, ["verify"])

    assert result.exit_code == 8
    assert "seq_gap" in result.stdout


def test_verify_can_be_pointed_at_one_session(data_dir: Path) -> None:
    break_log(data_dir, "ses_other")

    result = runner.invoke(app, ["verify", "--session", SESSION_ID, "--json"])

    assert result.exit_code == 0
    assert [entry["session_id"] for entry in json.loads(result.stdout)["sessions"]] == [SESSION_ID]


def test_reindex_rebuilds_a_deleted_index_and_verify_then_passes(data_dir: Path) -> None:
    drop_index(data_dir)

    rebuilt = runner.invoke(app, ["reindex"])

    assert rebuilt.exit_code == 0
    assert "reindexed" in rebuilt.stdout
    assert runner.invoke(app, ["verify"]).exit_code == 0


def test_reindex_is_safe_to_run_when_nothing_is_wrong(data_dir: Path) -> None:
    result = runner.invoke(app, ["reindex", "--json"])

    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload["changed"] == 0


def test_reindex_exits_one_when_a_log_cannot_be_read(data_dir: Path) -> None:
    """A partial rebuild must not report success to whatever gates on this."""

    break_log(data_dir, SESSION_ID)

    result = runner.invoke(app, ["reindex"])

    assert result.exit_code == 1
    assert "skipped" in result.stdout


def test_reindex_repairs_an_index_that_lags_the_log(data_dir: Path) -> None:
    connection = sqlite3.connect(str(data_dir / INDEX_FILENAME), isolation_level=None)
    try:
        connection.execute("DELETE FROM events WHERE seq >= 6")
    finally:
        connection.close()

    assert runner.invoke(app, ["verify"]).exit_code == 1
    assert runner.invoke(app, ["reindex"]).exit_code == 0
    assert runner.invoke(app, ["verify"]).exit_code == 0


def test_backup_writes_a_manifest_and_verifies_its_own_copy(data_dir: Path, tmp_path: Path) -> None:
    out = tmp_path / "backup"

    result = runner.invoke(app, ["backup", "--out", str(out)])

    assert result.exit_code == 0
    assert (out / MANIFEST_FILENAME).exists()
    assert result.stdout.strip().endswith("verdict: ok")


def test_backup_without_an_out_directory_lands_under_the_data_dir(data_dir: Path) -> None:
    result = runner.invoke(app, ["backup", "--json"])

    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert Path(payload["directory"]).parent == data_dir / "backups"
    assert payload["verification"]["ok"] is True


def test_backup_can_skip_the_index_because_the_index_is_derived(
    data_dir: Path, tmp_path: Path
) -> None:
    result = runner.invoke(
        app, ["backup", "--out", str(tmp_path / "backup"), "--no-index", "--json"]
    )

    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload["manifest"]["index"] is None


def test_backup_of_one_session_only(data_dir: Path, tmp_path: Path) -> None:
    out = tmp_path / "backup"

    result = runner.invoke(app, ["backup", "--out", str(out), "--session", SESSION_ID])

    assert result.exit_code == 0
    assert not (out / "sessions" / "ses_other").exists()


def test_backup_check_re_hashes_a_stored_backup(data_dir: Path, tmp_path: Path) -> None:
    """So a backup can be checked on a schedule, long before anybody needs it."""

    out = tmp_path / "backup"
    runner.invoke(app, ["backup", "--out", str(out)])

    assert runner.invoke(app, ["backup-check", str(out)]).exit_code == 0


def test_backup_check_exits_one_on_a_damaged_backup(data_dir: Path, tmp_path: Path) -> None:
    out = tmp_path / "backup"
    runner.invoke(app, ["backup", "--out", str(out)])
    with (out / "sessions" / SESSION_ID / "events.jsonl").open(
        "a", encoding="utf-8", newline="\n"
    ) as handle:
        handle.write("junk\n")

    result = runner.invoke(app, ["backup-check", str(out)])

    assert result.exit_code == 1
    assert "corrupt" in result.stdout


def test_restore_proves_the_restored_logs_still_fold_the_same(
    data_dir: Path, tmp_path: Path
) -> None:
    out = tmp_path / "backup"
    runner.invoke(app, ["backup", "--out", str(out)])
    target = tmp_path / "restored"

    result = runner.invoke(app, ["restore", str(out), "--target", str(target)])

    assert result.exit_code == 0
    assert "verdict: compatible" in result.stdout
    with EventStore(target) as store:
        assert sorted(store.list_session_ids()) == [SESSION_ID, "ses_other"]


def test_restore_rebuilds_the_index_when_the_backup_had_none(
    data_dir: Path, tmp_path: Path
) -> None:
    """Otherwise the restore leaves a directory nothing can serve traffic from."""

    out = tmp_path / "backup"
    runner.invoke(app, ["backup", "--out", str(out), "--no-index"])
    target = tmp_path / "restored"

    result = runner.invoke(app, ["restore", str(out), "--target", str(target), "--json"])

    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload["reindex"]["ok"] is True
    assert (target / INDEX_FILENAME).exists()


def test_restore_refuses_a_populated_target_until_forced(
    data_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Two histories of one session id must never interleave by accident."""

    out = tmp_path / "backup"
    runner.invoke(app, ["backup", "--out", str(out)])
    monkeypatch.setattr(sys, "argv", ["atlas", "restore", str(out), "--target", str(data_dir)])

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 2
    assert json.loads(capsys.readouterr().err)["error"] == "configuration_error"
    assert (
        runner.invoke(app, ["restore", str(out), "--target", str(data_dir), "--force"]).exit_code
        == 0
    )


def test_release_check_is_not_ready_without_the_frozen_samples(
    data_dir: Path, tmp_path: Path
) -> None:
    result = runner.invoke(app, ["release-check", "--samples", str(tmp_path / "absent")])

    assert result.exit_code == 1
    assert "verdict: not ready" in result.stdout


def test_release_check_passes_against_the_committed_samples(data_dir: Path) -> None:
    """The gate a release actually runs, against the samples in the repository."""

    samples = Path(__file__).resolve().parents[2] / "samples"

    result = runner.invoke(app, ["release-check", "--samples", str(samples), "--json"])

    payload = json.loads(result.stdout)
    assert result.exit_code == 0, [check for check in payload["checks"] if not check["passed"]]
    assert payload["passed"] == payload["total"]
    assert all(risk["complete"] for risk in payload["risks"])


def test_release_check_prints_the_risk_register_for_a_reviewer(data_dir: Path) -> None:
    samples = Path(__file__).resolve().parents[2] / "samples"

    result = runner.invoke(app, ["release-check", "--samples", str(samples)])

    assert "risk register:" in result.stdout
    assert "rollback:" in result.stdout
    assert "prompt injection" in result.stdout


def test_a_damaged_directory_is_walked_back_to_healthy_with_these_commands_alone(
    data_dir: Path, tmp_path: Path
) -> None:
    """The runbook, executed: back up while healthy, lose a log, restore, verify."""

    out = tmp_path / "backup"
    assert runner.invoke(app, ["backup", "--out", str(out)]).exit_code == 0

    break_log(data_dir, SESSION_ID)
    assert runner.invoke(app, ["verify"]).exit_code == 8
    assert runner.invoke(app, ["reindex"]).exit_code == 1

    assert runner.invoke(app, ["restore", str(out), "--force"]).exit_code == 0
    assert runner.invoke(app, ["verify"]).exit_code == 0
