"""Every checklist item is computed, so every item has to be falsifiable.

M9's completion condition is 发布检查清单全部通过，关键风险有明确的监控、暂停和回滚
动作. A checklist that always passes satisfies that sentence and protects nothing,
so each test here pairs a passing case with the damage that must make the same
check fail. The register gets the same treatment: its evidence paths are resolved
against the repository, because a control that cites a file which no longer exists
is exactly the rot the register was written to prevent.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from atlas_harness.events import Event, EventStore, EventType
from atlas_harness.events.models import CURRENT_SCHEMA_VERSION, SUPPORTED_SCHEMA_VERSIONS
from atlas_harness.events.store import LOG_FILENAME, SESSIONS_DIRNAME
from atlas_harness.kernel.errors import ConfigurationError
from atlas_harness.ops.checklist import (
    CHECK_NAMES,
    DEMO_SESSION_ID,
    RISK_REGISTER,
    SAMPLE_EXPECTATIONS_FILENAME,
    RiskControl,
    check_backup_round_trip,
    check_data_dir,
    check_demo_session,
    check_no_secrets,
    check_policy_probes,
    check_risk_register,
    check_sample_replay,
    check_schema_coverage,
    check_schema_policy,
    check_side_effect_tools,
    run_release_checks,
)

StoreFactory = Callable[..., EventStore]
SessionSeeder = Callable[..., list[Event]]

SESSION_ID = "ses_release"
FAKE_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
"""The example key from AWS's own documentation: shaped like a credential, and
not one. A real-looking secret in a test file is a secret in a test file."""


def write_samples(directory: Path, expectations: dict[str, object]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / SAMPLE_EXPECTATIONS_FILENAME).write_text(
        json.dumps({"sessions": expectations}, indent=2), encoding="utf-8", newline="\n"
    )
    return directory


def copy_log(
    store: EventStore, session_id: str, samples_dir: Path, as_id: str | None = None
) -> None:
    target = samples_dir / SESSIONS_DIRNAME / (as_id or session_id) / LOG_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(store.log_path(session_id).read_bytes())


def append_demo_events(store: EventStore, session_id: str) -> None:
    """The three things section 13 asks a demo to show, in the order it shows them."""

    store.append_new(
        EventType.APPROVAL_RESOLVED,
        session_id=session_id,
        payload={"approval_id": "apr_demo", "approved": True},
    )
    store.append_new(
        EventType.ARTIFACT_STORED,
        session_id=session_id,
        payload={"artifact_id": "art_demo", "path": "art_demo.txt", "kind": "tool_output"},
    )
    store.append_new(
        EventType.TOOL_RESULT,
        session_id=session_id,
        payload={"tool_name": "run_tests", "success": False},
    )
    store.append_new(
        EventType.TOOL_RESULT,
        session_id=session_id,
        payload={"tool_name": "run_tests", "success": True},
    )


def test_the_checklist_runs_exactly_the_closed_list_of_checks(
    store: EventStore, seed: SessionSeeder, tmp_path: Path
) -> None:
    """A caller may gate on a name, so the names and their order are the contract."""

    seed(store, SESSION_ID)

    report = run_release_checks(store, samples_dir=tmp_path / "samples", workspace_root=tmp_path)

    assert tuple(check.name for check in report.checks) == CHECK_NAMES
    assert report.schema_version == CURRENT_SCHEMA_VERSION
    assert report.data_dir == str(store.data_dir)
    assert report.risks == RISK_REGISTER


def test_a_missing_sample_set_makes_the_release_not_ready(
    store: EventStore, seed: SessionSeeder, tmp_path: Path
) -> None:
    """The failure mode worth pinning: no fixtures must never read as a pass."""

    seed(store, SESSION_ID)

    report = run_release_checks(store, samples_dir=tmp_path / "absent", workspace_root=tmp_path)

    assert not report.ok
    assert {check.name for check in report.failed} >= {"sample_replay", "schema_coverage"}
    assert report.render()[-1].startswith("verdict: not ready")


def test_the_checklist_writes_nothing_into_the_directory_it_checks(
    store: EventStore, seed: SessionSeeder, tmp_path: Path
) -> None:
    """Otherwise it is a tool nobody runs against production, which is where it counts."""

    seed(store, SESSION_ID)
    before = store.log_path(SESSION_ID).read_bytes()

    run_release_checks(store, samples_dir=tmp_path / "samples", workspace_root=tmp_path)

    assert store.log_path(SESSION_ID).read_bytes() == before
    assert not (store.data_dir / "backups").exists()
    assert store.list_session_ids() == [SESSION_ID]


def test_the_schema_policy_check_passes_on_this_build() -> None:
    result = check_schema_policy()

    assert result.passed
    assert result.evidence["current_schema_version"] == CURRENT_SCHEMA_VERSION
    assert result.render().startswith("[PASS]")


def test_the_data_dir_check_follows_verify(
    store_factory: StoreFactory, seed: SessionSeeder
) -> None:
    store = store_factory()
    seed(store, SESSION_ID)

    assert check_data_dir(store).passed

    log = store.log_path(SESSION_ID)
    lines = log.read_text(encoding="utf-8").splitlines()
    store.close()
    log.write_text(
        "".join(f"{line}\n" for line in lines[:3] + lines[4:]), encoding="utf-8", newline="\n"
    )

    result = check_data_dir(store_factory())

    assert not result.passed
    assert result.detail
    assert result.render().startswith("[FAIL]")


def test_the_backup_round_trip_check_backs_up_and_restores_this_directory(
    store: EventStore, seed: SessionSeeder
) -> None:
    seed(store, "ses_a")
    seed(store, "ses_b")

    result = check_backup_round_trip(store)

    assert result.passed
    assert result.evidence["sessions"] == 2
    assert result.evidence["bytes"] > 0


def test_a_session_directory_with_no_log_is_not_a_session(
    store: EventStore, seed: SessionSeeder
) -> None:
    """The backup and the store have to agree on what counts, or the round trip
    fails on a directory some interrupted command left behind."""

    seed(store, SESSION_ID)
    (store.data_dir / SESSIONS_DIRNAME / "ses_hollow").mkdir(parents=True)

    result = check_backup_round_trip(store)

    assert result.passed
    assert result.evidence["sessions"] == 1


def test_the_backup_round_trip_check_reports_a_refusal_instead_of_raising(
    store: EventStore, seed: SessionSeeder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check has to survive its own tools failing, or a release aborts with a
    traceback instead of a verdict naming the item that went wrong."""

    seed(store, SESSION_ID)

    def refuse(*args: object, **kwargs: object) -> None:
        raise ConfigurationError("no room on the backup volume")

    monkeypatch.setattr("atlas_harness.ops.checklist.create_backup", refuse)

    result = check_backup_round_trip(store)

    assert not result.passed
    assert "no room on the backup volume" in result.detail


def test_the_sample_replay_check_folds_the_frozen_logs(
    store: EventStore, seed: SessionSeeder, tmp_path: Path
) -> None:
    seed(store, SESSION_ID)
    samples = tmp_path / "samples"
    copy_log(store, SESSION_ID, samples)
    write_samples(
        samples,
        {
            SESSION_ID: {
                "state_hash": store.load_state(SESSION_ID).state_hash(),
                "events": 7,
                "schema_version": CURRENT_SCHEMA_VERSION,
            }
        },
    )

    result = check_sample_replay(samples)

    assert result.passed
    assert result.evidence == {"sessions": 1, "replayed": 1}


def test_a_sample_that_folds_to_a_different_hash_fails_the_release(
    store: EventStore, seed: SessionSeeder, tmp_path: Path
) -> None:
    """The one check that fails on a deliberate change, which is the whole point."""

    seed(store, SESSION_ID)
    samples = tmp_path / "samples"
    copy_log(store, SESSION_ID, samples)
    write_samples(
        samples,
        {SESSION_ID: {"state_hash": "f" * 64, "events": 7, "schema_version": 1}},
    )

    result = check_sample_replay(samples)

    assert not result.passed
    assert SESSION_ID in result.detail


def test_a_sample_with_the_wrong_event_count_fails_even_when_the_hash_matches(
    store: EventStore, seed: SessionSeeder, tmp_path: Path
) -> None:
    seed(store, SESSION_ID)
    samples = tmp_path / "samples"
    copy_log(store, SESSION_ID, samples)
    write_samples(
        samples,
        {
            SESSION_ID: {
                "state_hash": store.load_state(SESSION_ID).state_hash(),
                "events": 99,
                "schema_version": 1,
            }
        },
    )

    assert not check_sample_replay(samples).passed


def test_a_sample_expectation_with_no_log_on_disk_fails(tmp_path: Path) -> None:
    samples = write_samples(tmp_path / "samples", {"ses_ghost": {"state_hash": "0" * 64}})

    result = check_sample_replay(samples)

    assert not result.passed
    assert "no log" in result.detail


def test_an_empty_sample_set_is_not_a_pass(tmp_path: Path) -> None:
    """Zero mismatches out of zero samples is the emptiest kind of green."""

    samples = write_samples(tmp_path / "samples", {})

    assert not check_sample_replay(samples).passed


def test_unreadable_sample_expectations_are_reported(tmp_path: Path) -> None:
    samples = tmp_path / "samples"
    samples.mkdir()
    (samples / SAMPLE_EXPECTATIONS_FILENAME).write_text("[not an object]", encoding="utf-8")

    result = check_sample_replay(samples)

    assert not result.passed


def test_schema_coverage_wants_a_sample_for_every_version_this_build_reads(
    tmp_path: Path,
) -> None:
    samples = write_samples(
        tmp_path / "samples",
        {
            f"ses_v{version}": {"state_hash": "0" * 64, "schema_version": version}
            for version in sorted(SUPPORTED_SCHEMA_VERSIONS)
        },
    )

    result = check_schema_coverage(samples)

    assert result.passed
    assert result.evidence["covered"] == sorted(SUPPORTED_SCHEMA_VERSIONS)


def test_covering_only_the_current_version_is_a_compatibility_claim_never_exercised(
    tmp_path: Path,
) -> None:
    samples = write_samples(
        tmp_path / "samples",
        {"ses_now": {"state_hash": "0" * 64, "schema_version": CURRENT_SCHEMA_VERSION}},
    )

    result = check_schema_coverage(samples)

    assert not result.passed
    assert "schema v" in result.detail


def test_the_demo_check_reads_the_demo_rather_than_a_description_of_it(
    store: EventStore, seed: SessionSeeder, tmp_path: Path
) -> None:
    seed(store, DEMO_SESSION_ID)
    append_demo_events(store, DEMO_SESSION_ID)
    samples = tmp_path / "samples"
    copy_log(store, DEMO_SESSION_ID, samples)

    result = check_demo_session(samples)

    assert result.passed
    assert result.evidence["approvals"] == 1
    assert result.evidence["artifacts"] == 1


def test_a_demo_without_a_failure_and_a_fix_does_not_count(
    store: EventStore, seed: SessionSeeder, tmp_path: Path
) -> None:
    seed(store, DEMO_SESSION_ID)
    store.append_new(
        EventType.APPROVAL_RESOLVED,
        session_id=DEMO_SESSION_ID,
        payload={"approval_id": "apr_demo", "approved": True},
    )
    store.append_new(
        EventType.ARTIFACT_STORED,
        session_id=DEMO_SESSION_ID,
        payload={"artifact_id": "art_demo", "path": "art_demo.txt", "kind": "tool_output"},
    )
    samples = tmp_path / "samples"
    copy_log(store, DEMO_SESSION_ID, samples)

    result = check_demo_session(samples)

    assert not result.passed
    assert "no failure was followed by a passing run" in result.detail


def test_a_missing_demo_log_names_the_path_it_looked_for(tmp_path: Path) -> None:
    result = check_demo_session(tmp_path / "samples")

    assert not result.passed
    assert DEMO_SESSION_ID in result.detail


def test_the_secrets_check_passes_on_a_clean_data_directory(
    store: EventStore, seed: SessionSeeder
) -> None:
    seed(store, "ses_a")
    seed(store, "ses_b")

    result = check_no_secrets(store)

    assert result.passed
    assert result.evidence["files_scanned"] > 2
    assert result.evidence["hits"] == 0


def test_a_credential_shaped_string_in_a_log_fails_the_release(
    store: EventStore, seed: SessionSeeder
) -> None:
    seed(store, SESSION_ID)
    store.append_new(
        EventType.TOOL_RESULT,
        session_id=SESSION_ID,
        payload={"tool_name": "read_file", "output_excerpt": f"aws_key={FAKE_AWS_KEY}"},
    )

    result = check_no_secrets(store)

    assert not result.passed
    assert result.evidence["hits"] >= 1


def test_the_secrets_check_never_prints_what_it_found(
    store: EventStore, seed: SessionSeeder
) -> None:
    """It reports locations, because its own output is printed and archived."""

    seed(store, SESSION_ID)
    store.append_new(
        EventType.TOOL_RESULT,
        session_id=SESSION_ID,
        payload={"tool_name": "read_file", "output_excerpt": f"aws_key={FAKE_AWS_KEY}"},
    )

    result = check_no_secrets(store)
    printed = json.dumps(result.as_json(), ensure_ascii=False) + result.render()

    assert FAKE_AWS_KEY not in printed
    assert SESSION_ID in result.detail


def test_side_effecting_tools_in_the_shipping_registry_need_approval() -> None:
    result = check_side_effect_tools()

    assert result.passed
    assert result.evidence["side_effecting"] >= 1


def test_the_policy_probes_are_all_refused(tmp_path: Path) -> None:
    result = check_policy_probes(tmp_path)

    assert result.passed
    assert result.evidence["probes"] >= 6


def test_every_risk_names_a_monitor_a_pause_and_a_rollback() -> None:
    result = check_risk_register()

    assert result.passed
    assert result.evidence["risks"] == len(RISK_REGISTER)
    assert all(risk.complete for risk in RISK_REGISTER)


def test_a_risk_missing_one_of_the_three_actions_fails_the_release() -> None:
    incomplete = RiskControl(
        risk="something nobody can undo",
        signal="it happens",
        monitor="a dashboard",
        pause="",
        rollback="",
        evidence=("nowhere",),
    )

    result = check_risk_register([incomplete])

    assert not result.passed
    assert "something nobody can undo" in result.detail


def test_an_empty_register_is_not_a_complete_one() -> None:
    assert not check_risk_register([]).passed


def test_every_risk_cites_a_file_that_exists() -> None:
    """A control that points at a deleted module is an intention, not a control."""

    root = Path(__file__).resolve().parents[2]
    missing = [
        citation
        for risk in RISK_REGISTER
        for citation in risk.evidence
        if not (root / "src" / "atlas_harness" / citation.split(":")[0]).exists()
        and not (root / citation.split(":")[0]).exists()
    ]

    assert missing == []


def test_the_register_renders_the_three_actions_for_a_reviewer() -> None:
    lines = RISK_REGISTER[0].render()

    assert [line.strip().split(":")[0] for line in lines[1:]] == [
        "signal",
        "monitor",
        "pause",
        "rollback",
        "where",
    ]


def test_the_report_serializes_for_the_cli(
    store: EventStore, seed: SessionSeeder, tmp_path: Path
) -> None:
    seed(store, SESSION_ID)

    report = run_release_checks(store, samples_dir=tmp_path / "samples", workspace_root=tmp_path)
    payload = json.loads(json.dumps(report.as_json(), ensure_ascii=False))

    assert payload["total"] == len(CHECK_NAMES)
    assert payload["passed"] == len(CHECK_NAMES) - len(report.failed)
    assert len(payload["risks"]) == len(RISK_REGISTER)
    assert all(risk["complete"] for risk in payload["risks"])
