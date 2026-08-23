"""Skill files, the version state machine, and what stays traceable afterwards.

Two of the plan's four M6 conditions are decided here rather than in the selector:
a skill's version, source and evidence must be recoverable from the log, and an
unpermitted skill must be rejected rather than ranked low. The selector enforces
the second one, but it can only do so because the record carries its scopes.

The gate this file guards hardest is the missing ``draft -> active`` edge. That
absence *is* the evaluation requirement, so it is asserted from both sides: the
transition table refuses it, and a file on disk cannot declare its way around it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from atlas_harness.events import EventStore, EventType
from atlas_harness.kernel.clock import FrozenClock
from atlas_harness.kernel.errors import ConfigurationError, EventValidationError
from atlas_harness.skills.loader import (
    LOADABLE_STATUSES,
    load_directory,
    load_file,
    record_from_mapping,
)
from atlas_harness.skills.models import (
    ALLOWED_TRANSITIONS,
    SkillRecord,
    SkillStatus,
    check_transition,
    checksum_for,
    parse_status,
)
from atlas_harness.skills.repository import SkillRepository
from atlas_harness.tools.manifest import SCOPE_FS_READ, SCOPE_NETWORK

SESSION_ID = "ses_skills"


@pytest.fixture
def repository(store: EventStore, clock: FrozenClock) -> SkillRepository:
    store.append_new(
        EventType.SESSION_CREATED,
        session_id=SESSION_ID,
        payload={"title": "skills", "workspace_root": "/tmp/ws"},
    )
    return SkillRepository(store, clock=clock)


def skill(
    skill_id: str = "release-notes",
    *,
    version: str = "1.0.0",
    status: SkillStatus = SkillStatus.DRAFT,
    **fields: object,
) -> SkillRecord:
    return SkillRecord(skill_id=skill_id, version=version, status=status, **fields)  # type: ignore[arg-type]


def write_skill(directory: Path, name: str, payload: dict[str, object]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# the state machine
# --------------------------------------------------------------------------- #


def test_a_draft_cannot_become_active_in_one_step() -> None:
    """The missing edge is the evaluation gate. Adding it would remove the gate."""

    assert SkillStatus.ACTIVE not in ALLOWED_TRANSITIONS[SkillStatus.DRAFT]

    with pytest.raises(EventValidationError) as excinfo:
        check_transition(SkillStatus.DRAFT, SkillStatus.ACTIVE)

    assert excinfo.value.details["from_status"] == "draft"
    assert excinfo.value.details["to_status"] == "active"
    assert "candidate" in excinfo.value.details["allowed"]


def test_promotion_goes_through_candidate() -> None:
    check_transition(SkillStatus.DRAFT, SkillStatus.CANDIDATE)
    check_transition(SkillStatus.CANDIDATE, SkillStatus.ACTIVE)


def test_a_retired_version_stays_retired() -> None:
    """Retirement is the one terminal state; reviving it would mean a version's
    history could be reopened without a new version number."""

    assert ALLOWED_TRANSITIONS[SkillStatus.RETIRED] == frozenset()


@pytest.mark.parametrize(
    ("status", "injectable"),
    [
        (SkillStatus.DRAFT, False),
        (SkillStatus.CANDIDATE, False),
        (SkillStatus.ACTIVE, True),
        (SkillStatus.DEPRECATED, False),
        (SkillStatus.RETIRED, False),
    ],
)
def test_only_an_active_version_may_enter_a_prompt(status: SkillStatus, injectable: bool) -> None:
    assert status.is_injectable is injectable


def test_an_unknown_status_is_refused() -> None:
    with pytest.raises(EventValidationError):
        parse_status("published")


# --------------------------------------------------------------------------- #
# permission lives on the record
# --------------------------------------------------------------------------- #


def test_a_skill_reports_the_scopes_it_is_missing() -> None:
    """The selector needs the difference, not a boolean: the skip reason has to say
    which scope was absent or an operator cannot fix it."""

    record = skill(required_scopes=(SCOPE_FS_READ, SCOPE_NETWORK))

    assert record.is_permitted(frozenset({SCOPE_FS_READ})) is False
    assert record.missing_scopes(frozenset({SCOPE_FS_READ})) == (SCOPE_NETWORK,)


def test_a_skill_that_asks_for_nothing_is_always_permitted() -> None:
    assert skill().is_permitted(frozenset()) is True


def test_triggers_are_the_authors_own_statement_of_relevance() -> None:
    record = skill(triggers=("release notes", "changelog"))

    assert record.matches("help me write the RELEASE NOTES") is True
    assert record.matches("rotate the credentials") is False


def test_a_skill_without_triggers_never_matches_by_rule() -> None:
    """Absent triggers must not read as 'matches everything' — the prefilter scores
    above every text score, so a wildcard there would outrank real retrieval."""

    assert skill(triggers=()).matches("anything at all") is False


def test_the_rendered_text_names_the_version_and_its_evidence() -> None:
    text = skill(
        version="2.1.0",
        description="write release notes",
        required_scopes=(SCOPE_FS_READ,),
        evidence_refs=("eval-42",),
    ).as_context_text()

    assert "[skill:release-notes v2.1.0]" in text
    assert f"requires: {SCOPE_FS_READ}" in text
    assert "evidence: eval-42" in text


# --------------------------------------------------------------------------- #
# the loader
# --------------------------------------------------------------------------- #


def test_a_file_arrives_as_a_draft_by_default(tmp_path: Path) -> None:
    path = write_skill(tmp_path, "a.json", {"id": "release-notes", "body": "steps"})

    assert load_file(path).status is SkillStatus.DRAFT


def test_a_file_may_not_declare_itself_active(tmp_path: Path) -> None:
    """Dropping a file into the directory must not put it in the next request."""

    path = write_skill(tmp_path, "a.json", {"id": "release-notes", "status": "active"})

    with pytest.raises(ConfigurationError) as excinfo:
        load_file(path)

    assert excinfo.value.details["status"] == "active"
    assert excinfo.value.details["allowed"] == sorted(item.value for item in LOADABLE_STATUSES)


def test_a_file_may_declare_itself_a_candidate(tmp_path: Path) -> None:
    path = write_skill(tmp_path, "a.json", {"id": "release-notes", "status": "candidate"})

    assert load_file(path).status is SkillStatus.CANDIDATE


def test_the_loader_records_where_the_skill_came_from(tmp_path: Path) -> None:
    path = write_skill(
        tmp_path,
        "a.json",
        {"id": "release-notes", "body": "steps", "source_task": "op_7", "evidence": ["eval-42"]},
    )

    record = load_file(path)

    assert record.source_path == str(path)
    assert record.source_task == "op_7"
    assert record.evidence_refs == ("eval-42",)
    assert record.checksum == checksum_for("steps")


def test_a_bad_file_is_named_rather_than_failing_the_scan(tmp_path: Path) -> None:
    """An operator should see every broken skill at once, not one per restart."""

    write_skill(tmp_path, "good.json", {"id": "good"})
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    write_skill(tmp_path, "nameless.json", {"body": "no id here"})

    result = load_directory(tmp_path)

    assert [record.skill_id for record in result.records] == ["good"]
    assert {path.name for path in (error.path for error in result.errors)} == {
        "bad.json",
        "nameless.json",
    }
    assert result.ok is False


def test_the_same_version_defined_twice_is_an_error_not_a_race(tmp_path: Path) -> None:
    write_skill(tmp_path, "a.json", {"id": "dup", "version": "1.0.0"})
    write_skill(tmp_path, "b.json", {"id": "dup", "version": "1.0.0"})

    result = load_directory(tmp_path)

    assert len(result.records) == 1
    assert "duplicate skill" in result.errors[0].message


def test_a_missing_directory_is_not_an_error(tmp_path: Path) -> None:
    """A deployment with no skills yet is normal; refusing to start over it would
    make the whole feature mandatory."""

    result = load_directory(tmp_path / "absent")

    assert result.records == ()
    assert result.ok is True


def test_files_the_loader_does_not_understand_are_ignored(tmp_path: Path) -> None:
    write_skill(tmp_path, "a.json", {"id": "good"})
    (tmp_path / "README.md").write_text("not a skill", encoding="utf-8")

    result = load_directory(tmp_path)

    assert [record.skill_id for record in result.records] == ["good"]
    assert result.ok is True


def test_a_scan_is_ordered_so_registration_is_reproducible(tmp_path: Path) -> None:
    for name in ("c.json", "a.json", "b.json"):
        write_skill(tmp_path, name, {"id": name[0]})

    assert [record.skill_id for record in load_directory(tmp_path).records] == ["a", "b", "c"]


def test_an_oversized_body_is_clipped_not_refused() -> None:
    record = record_from_mapping({"id": "big", "body": "x" * 10_000})

    assert len(record.body) == 4_000


def test_scopes_and_triggers_accept_a_single_string() -> None:
    record = record_from_mapping({"id": "a", "scopes": SCOPE_FS_READ, "triggers": "release"})

    assert record.required_scopes == (SCOPE_FS_READ,)
    assert record.triggers == ("release",)


# --------------------------------------------------------------------------- #
# the repository: the event is the record
# --------------------------------------------------------------------------- #


def test_registering_writes_the_event_before_the_row(repository: SkillRepository) -> None:
    record = repository.register(
        skill(source_task="op_7", evidence_refs=("eval-42",)),
        session_id=SESSION_ID,
    )

    event = repository.store.read_events(SESSION_ID)[-1]
    assert event.event_type is EventType.SKILL_REGISTERED
    payload = event.payload.model_dump(mode="json")
    assert payload["skill_id"] == "release-notes"
    assert payload["version"] == "1.0.0"
    assert payload["source_task"] == "op_7"
    assert payload["evidence_refs"] == ["eval-42"]
    assert repository.get("release-notes", "1.0.0") == record


def test_registration_stamps_a_checksum_and_a_time(
    repository: SkillRepository, clock: FrozenClock
) -> None:
    """Version alone does not identify content: a file can be edited in place under
    the same version, and the checksum is what makes that visible."""

    record = repository.register(skill(body="steps"), session_id=SESSION_ID)

    assert record.checksum == checksum_for("steps")
    assert record.registered_at_ms == clock.now_ms()


def test_a_status_change_records_its_reason_and_evaluation(
    repository: SkillRepository,
) -> None:
    """The plan's third condition: a promotion must be explainable afterwards."""

    repository.register(skill(), session_id=SESSION_ID)
    repository.set_status("release-notes", "1.0.0", SkillStatus.CANDIDATE, session_id=SESSION_ID)

    promoted = repository.set_status(
        "release-notes",
        "1.0.0",
        SkillStatus.ACTIVE,
        session_id=SESSION_ID,
        reason="passed the release-notes eval",
        evaluation_ref="eval-42",
    )

    assert promoted.status is SkillStatus.ACTIVE
    payload = repository.store.read_events(SESSION_ID)[-1].payload.model_dump(mode="json")
    assert payload["from_status"] == "candidate"
    assert payload["to_status"] == "active"
    assert payload["reason"] == "passed the release-notes eval"
    assert payload["evaluation_ref"] == "eval-42"


def test_an_illegal_promotion_writes_no_event(repository: SkillRepository) -> None:
    """The transition is checked before the append, so a refused promotion leaves
    the log exactly as it was rather than recording an effect that did not happen."""

    repository.register(skill(), session_id=SESSION_ID)
    before = len(repository.store.read_events(SESSION_ID))

    with pytest.raises(EventValidationError):
        repository.set_status("release-notes", "1.0.0", SkillStatus.ACTIVE, session_id=SESSION_ID)

    assert len(repository.store.read_events(SESSION_ID)) == before
    assert repository.get("release-notes", "1.0.0").status is SkillStatus.DRAFT  # type: ignore[union-attr]


def test_promoting_a_version_that_was_never_registered_is_refused(
    repository: SkillRepository,
) -> None:
    with pytest.raises(EventValidationError) as excinfo:
        repository.set_status("ghost", "9.9.9", SkillStatus.CANDIDATE, session_id=SESSION_ID)

    assert excinfo.value.details["skill_id"] == "ghost"
    assert excinfo.value.details["version"] == "9.9.9"


# --------------------------------------------------------------------------- #
# which version is effective
# --------------------------------------------------------------------------- #


def activate(repository: SkillRepository, skill_id: str, version: str) -> SkillRecord:
    repository.set_status(skill_id, version, SkillStatus.CANDIDATE, session_id=SESSION_ID)
    return repository.set_status(
        skill_id, version, SkillStatus.ACTIVE, session_id=SESSION_ID, evaluation_ref="eval-1"
    )


def test_only_the_highest_active_version_is_effective(repository: SkillRepository) -> None:
    """Two active versions of one skill would put contradictory instructions in the
    same prompt, so the repository picks rather than injecting both."""

    for version in ("1.0.0", "1.2.0"):
        repository.register(skill(version=version), session_id=SESSION_ID)
        activate(repository, "release-notes", version)

    assert [record.version for record in repository.active()] == ["1.2.0"]


def test_a_candidate_version_is_not_effective(repository: SkillRepository) -> None:
    repository.register(skill(version="1.0.0"), session_id=SESSION_ID)
    activate(repository, "release-notes", "1.0.0")
    repository.register(skill(version="2.0.0"), session_id=SESSION_ID)
    repository.set_status("release-notes", "2.0.0", SkillStatus.CANDIDATE, session_id=SESSION_ID)

    assert [record.version for record in repository.active()] == ["1.0.0"]


def test_deprecating_the_active_version_leaves_none_effective(
    repository: SkillRepository,
) -> None:
    repository.register(skill(), session_id=SESSION_ID)
    activate(repository, "release-notes", "1.0.0")
    repository.set_status("release-notes", "1.0.0", SkillStatus.DEPRECATED, session_id=SESSION_ID)

    assert repository.active() == []
    # The version is still queryable; it just stopped being effective.
    assert repository.get("release-notes", "1.0.0") is not None


def test_listing_by_status_sees_every_registered_version(repository: SkillRepository) -> None:
    repository.register(skill(version="1.0.0"), session_id=SESSION_ID)
    repository.register(skill(version="2.0.0"), session_id=SESSION_ID)
    activate(repository, "release-notes", "2.0.0")

    assert len(repository.all()) == 2
    assert [record.version for record in repository.all(status=SkillStatus.DRAFT)] == ["1.0.0"]


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #


def test_search_only_returns_effective_versions(repository: SkillRepository) -> None:
    repository.register(
        skill("drafted", description="write the release notes"), session_id=SESSION_ID
    )
    repository.register(skill("live", description="write the release notes"), session_id=SESSION_ID)
    activate(repository, "live", "1.0.0")

    found = [record.skill_id for record, _ in repository.search("release notes")]

    assert found == ["live"]


def test_search_orders_the_same_way_every_time(repository: SkillRepository) -> None:
    for skill_id in ("c", "a", "b"):
        repository.register(
            skill(skill_id, description="write the release notes for the changelog"),
            session_id=SESSION_ID,
        )
        activate(repository, skill_id, "1.0.0")

    runs = [[record.skill_id for record, _ in repository.search("release notes")] for _ in range(5)]

    assert len(runs[0]) == 3
    assert all(run == runs[0] for run in runs)
    assert runs[0] == sorted(runs[0])


def test_the_score_reads_higher_is_better(repository: SkillRepository) -> None:
    """SQLite bm25() is negative and more-negative is better; the flip happens in
    the repository so no caller has to remember which direction it is reading."""

    repository.register(skill("a", triggers=("changelog",)), session_id=SESSION_ID)
    activate(repository, "a", "1.0.0")

    hits = repository.search("changelog")

    assert hits and hits[0][1] > 0


# --------------------------------------------------------------------------- #
# the table is a projection
# --------------------------------------------------------------------------- #


def test_the_rows_can_be_thrown_away_and_rebuilt_from_the_log(
    repository: SkillRepository,
) -> None:
    repository.register(skill(version="1.0.0", description="notes"), session_id=SESSION_ID)
    activate(repository, "release-notes", "1.0.0")
    repository.register(skill(version="2.0.0", description="notes"), session_id=SESSION_ID)

    count = repository.rebuild(SESSION_ID)

    assert count == 2
    # Status changes are replayed too, so the effective version survives the rebuild.
    assert [record.version for record in repository.active()] == ["1.0.0"]
    assert repository.get("release-notes", "2.0.0").status is SkillStatus.DRAFT  # type: ignore[union-attr]
    assert [record.skill_id for record, _ in repository.search("notes")] == ["release-notes"]
