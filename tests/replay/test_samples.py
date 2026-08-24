"""The committed samples are the baseline, and this suite is what makes them one.

``samples/expected.json`` records a state hash per session, frozen at the release
that generated it. These tests fold the logs on today's build and compare. A
failure here is not a flaky test: it means a change to the reducer, the payload
models or the schema altered how an existing log is read, which is the
backward-compatibility break the plan's 固定数据集基线报告 exists to catch. The fix
is to explain the change, never to regenerate the hashes.

The samples directory is read and never written. A test that dropped an index or
a scratch file into it would dirty the working tree on every run, and a fixture
that changes when you look at it is not a fixture.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from atlas_harness.events import Event, EventStore, EventType, replay
from atlas_harness.events.compat import unreadable
from atlas_harness.events.models import SUPPORTED_SCHEMA_VERSIONS
from atlas_harness.events.store import LOG_FILENAME, SESSIONS_DIRNAME
from atlas_harness.ops.checklist import DEMO_SESSION_ID, check_demo_session, check_sample_replay
from atlas_harness.ops.migrate import rebuild_index_at
from atlas_harness.ops.verify import verify_data_dir
from atlas_harness.tools.redaction import redact

SAMPLES_DIR = Path(__file__).resolve().parents[2] / "samples"
EXPECTATIONS: dict[str, dict[str, Any]] = json.loads(
    (SAMPLES_DIR / "expected.json").read_text(encoding="utf-8")
)["sessions"]
SESSION_IDS = sorted(EXPECTATIONS)


def sample_log(session_id: str) -> Path:
    return SAMPLES_DIR / SESSIONS_DIRNAME / session_id / LOG_FILENAME


def read_sample(session_id: str) -> list[Event]:
    """Parse a sample log without opening a store next to it."""

    return [
        Event.model_validate(json.loads(line))
        for line in sample_log(session_id).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def copy_samples(destination: Path) -> Path:
    """A writable copy, so a store may be opened against it."""

    shutil.copytree(SAMPLES_DIR / SESSIONS_DIRNAME, destination / SESSIONS_DIRNAME)
    return destination


def test_there_is_a_sample_for_every_schema_version_this_build_reads() -> None:
    """Seven claimed versions and one fixture would be an untested claim."""

    covered = {expected["schema_version"] for expected in EXPECTATIONS.values()}

    assert covered == set(SUPPORTED_SCHEMA_VERSIONS)


@pytest.mark.parametrize("session_id", SESSION_IDS)
def test_a_sample_folds_to_the_hash_committed_with_it(session_id: str) -> None:
    expected = EXPECTATIONS[session_id]
    events = read_sample(session_id)

    state = replay(events, session_id=session_id)

    assert len(events) == expected["events"]
    assert events[-1].seq == expected["last_seq"]
    assert state.state_hash() == expected["state_hash"]


@pytest.mark.parametrize("session_id", SESSION_IDS)
def test_a_sample_folds_the_same_way_twice(session_id: str) -> None:
    """Determinism, asserted on the frozen data rather than on data just written."""

    events = read_sample(session_id)

    first = replay(events, session_id=session_id)
    second = replay(events, session_id=session_id)

    assert first.state_hash() == second.state_hash()
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


@pytest.mark.parametrize("session_id", SESSION_IDS)
def test_a_sample_is_written_at_the_version_it_claims(session_id: str) -> None:
    versions = {event.schema_version for event in read_sample(session_id)}

    assert versions == {EXPECTATIONS[session_id]["schema_version"]}
    assert unreadable(versions) == ()


@pytest.mark.parametrize("session_id", SESSION_IDS)
def test_a_sample_has_a_contiguous_sequence_starting_at_one(session_id: str) -> None:
    events = read_sample(session_id)

    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    assert {event.session_id for event in events} == {session_id}


@pytest.mark.parametrize("session_id", SESSION_IDS)
def test_no_sample_line_looks_like_a_credential(session_id: str) -> None:
    """These logs ship with the repository, so they are the worst place for one."""

    for number, line in enumerate(
        sample_log(session_id).read_text(encoding="utf-8").splitlines(), start=1
    ):
        assert redact(line) == line, f"{session_id}:{number}"


def test_an_old_sample_folds_through_a_store_and_not_only_through_replay(
    tmp_path: Path,
) -> None:
    """The store's reader is stricter than ``replay``, and the v1 log predates it."""

    data_dir = copy_samples(tmp_path / "runtime")

    with EventStore(data_dir) as store:
        events = store.read_events("ses_schema_v1")
        state = store.load_state("ses_schema_v1")

    assert len(events) == EXPECTATIONS["ses_schema_v1"]["events"]
    assert state.state_hash() == EXPECTATIONS["ses_schema_v1"]["state_hash"]


def test_the_samples_rebuild_an_index_from_nothing_and_still_fold_the_same(
    tmp_path: Path,
) -> None:
    """A restore lands logs with no index, so this is the restore path end to end."""

    data_dir = copy_samples(tmp_path / "runtime")

    report = rebuild_index_at(data_dir)

    assert report.ok
    assert len(report.changed) == len(SESSION_IDS)
    with EventStore(data_dir) as store:
        assert sorted(store.list_session_ids()) == SESSION_IDS
        for session_id in SESSION_IDS:
            assert (
                store.load_state(session_id).state_hash() == EXPECTATIONS[session_id]["state_hash"]
            )


def test_the_demo_sample_shows_the_three_things_the_plan_asks_a_demo_to_show() -> None:
    """Section 13: one approval, one externalized result, one failure then a fix."""

    result = check_demo_session(SAMPLES_DIR)

    assert result.passed, result.detail
    assert result.evidence["session_id"] == DEMO_SESSION_ID


def test_the_demo_sample_is_a_single_operation_that_finished() -> None:
    events = read_sample(DEMO_SESSION_ID)
    types = [event.event_type for event in events]

    assert types[0] is EventType.SESSION_CREATED
    assert EventType.OPERATION_STARTED in types
    assert EventType.OPERATION_FINISHED in types
    assert types.index(EventType.OPERATION_STARTED) < types.index(EventType.OPERATION_FINISHED)


def test_the_release_check_agrees_with_this_suite() -> None:
    """Both read the same file, and a release must not be able to pass while this fails."""

    result = check_sample_replay(SAMPLES_DIR)

    assert result.passed, result.detail
    assert result.evidence == {"sessions": len(SESSION_IDS), "replayed": len(SESSION_IDS)}


def test_a_restored_copy_of_the_samples_verifies_including_its_artifacts(
    tmp_path: Path,
) -> None:
    """The demo externalizes a tool result, so its artifact ships with the log and
    has to still hash to what the log recorded."""

    data_dir = copy_samples(tmp_path / "runtime")
    rebuild_index_at(data_dir)

    with EventStore(data_dir) as store:
        report = verify_data_dir(store)

    assert report.ok, "\n".join(report.render())


def test_the_fixture_holds_logs_artifacts_and_nothing_derived() -> None:
    """The whole suite has run by now, and a derived file in here would be committed
    once and then silently disagree with the logs forever."""

    stray = sorted(
        path.relative_to(SAMPLES_DIR).as_posix()
        for path in SAMPLES_DIR.rglob("*")
        if path.is_file()
        and path.name != "expected.json"
        and path.name != LOG_FILENAME
        and path.parent.name != "artifacts"
    )

    assert stray == []
