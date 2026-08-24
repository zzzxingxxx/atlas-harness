"""What each schema version added, and why a log written by an older build reads.

The log is the only source of truth in this runtime, which makes the schema a
public interface with no migration window: a session written six months ago must
fold today or the guarantee is empty. That obligation is easy to state and easy
to break silently, so it is written down here as data rather than prose.

Two properties are what actually make an old log readable, and both are
mechanically checkable:

* :class:`~atlas_harness.events.models.Payload` sets ``extra="allow"``, so a key
  a *newer* build writes does not fail an *older* reader, and a key an older
  build never wrote simply is not there.
* Every field added to a payload after the version that introduced its event
  type carries a default, so today's class accepts yesterday's smaller object.

The rule those two imply is the compatibility policy: **a new schema version may
add event types and may add defaulted fields; it may not rename a field, remove
one the reducer reads, or make an existing field required.** Anything else needs
a new event type, which is cheap here, rather than a redefinition of an old one.

:func:`check_policy` is the enforcement. It is run by the release checklist and
by the unit suite, so a version bump that forgets to describe itself, or a new
event type nobody attributed to a version, fails a gate rather than a customer's
replay.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from atlas_harness.events.models import (
    CURRENT_SCHEMA_VERSION,
    PAYLOAD_TYPES,
    SUPPORTED_SCHEMA_VERSIONS,
    EventType,
    Payload,
)
from atlas_harness.kernel.errors import EventValidationError


class SchemaVersionRecord(BaseModel):
    """One schema version, the milestone that cut it, and what it introduced."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    milestone: str
    summary: str
    added_events: tuple[EventType, ...]

    def as_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "milestone": self.milestone,
            "summary": self.summary,
            "added_events": [event.value for event in self.added_events],
        }


SCHEMA_HISTORY: tuple[SchemaVersionRecord, ...] = (
    SchemaVersionRecord(
        version=1,
        milestone="M1",
        summary="event-sourcing kernel: sessions, operations, tool calls, approvals, snapshots",
        added_events=(
            EventType.SESSION_CREATED,
            EventType.OPERATION_STARTED,
            EventType.MODEL_REQUESTED,
            EventType.ASSISTANT_MESSAGE,
            EventType.APPROVAL_REQUESTED,
            EventType.APPROVAL_RESOLVED,
            EventType.TOOL_STARTED,
            EventType.TOOL_RESULT,
            EventType.OPERATION_FINISHED,
            EventType.OPERATION_FAILED,
            EventType.OPERATION_ABORTED,
            EventType.SNAPSHOT_CREATED,
        ),
    ),
    SchemaVersionRecord(
        version=2,
        milestone="M3",
        summary="model adapter layer and the agent loop's steer and interrupt queues",
        added_events=(
            EventType.MODEL_STREAM_COMPLETED,
            EventType.PROVIDER_ERROR,
            EventType.QUEUE_MESSAGE_ENQUEUED,
            EventType.QUEUE_MESSAGE_CONSUMED,
        ),
    ),
    SchemaVersionRecord(
        version=3,
        milestone="M4",
        summary="recovery of a crashed operation, plus lanes and branches",
        added_events=(
            EventType.OPERATION_SUSPENDED,
            EventType.OPERATION_RESUMED,
            EventType.LANE_CREATED,
            EventType.BRANCH_CREATED,
            EventType.BRANCH_SWITCHED,
        ),
    ),
    SchemaVersionRecord(
        version=4,
        milestone="M5",
        summary="structured compaction and externalized tool output",
        added_events=(
            EventType.CONTEXT_COMPACT_PENDING,
            EventType.CONTEXT_COMPACTED,
            EventType.ARTIFACT_STORED,
        ),
    ),
    SchemaVersionRecord(
        version=5,
        milestone="M6",
        summary="memory, skills and the capability injection that cites them",
        added_events=(
            EventType.MEMORY_STORED,
            EventType.MEMORY_EXPIRED,
            EventType.SKILL_REGISTERED,
            EventType.SKILL_STATUS_CHANGED,
            EventType.CAPABILITY_INJECTED,
        ),
    ),
    SchemaVersionRecord(
        version=6,
        milestone="M7",
        summary="the pending window: candidate skills, evaluation, promotion and rollback",
        added_events=(
            EventType.FEEDBACK_RECORDED,
            EventType.SKILL_CANDIDATE_PROPOSED,
            EventType.SKILL_CANDIDATE_REJECTED,
            EventType.CANDIDATE_EVALUATED,
            EventType.CHAMPION_PROMOTED,
            EventType.CHAMPION_ROLLED_BACK,
        ),
    ),
    SchemaVersionRecord(
        version=7,
        milestone="M8",
        summary="MCP servers as translated tools, and delegated sub-agent tasks",
        added_events=(
            EventType.MCP_SERVER_CONNECTED,
            EventType.MCP_SERVER_DISCONNECTED,
            EventType.MCP_TOOLS_REGISTERED,
            EventType.SUBAGENT_TASK_STARTED,
            EventType.SUBAGENT_TASK_FINISHED,
        ),
    ),
)
"""Frozen, append-only. An entry is never edited after its milestone ships --
that is the whole point of writing the history down instead of deriving it."""


COMPATIBILITY_RULES: tuple[str, ...] = (
    "a new version may add event types; existing types keep their meaning",
    "a new field on an existing payload must carry a default",
    "a field the reducer reads is never renamed or removed",
    "unknown keys are preserved rather than rejected, so a newer log reads too",
    "an unsupported schema_version is refused at parse time, not silently folded",
)
"""The policy in five lines, so the README, the release checklist and the tests
quote one source instead of three drifting paraphrases."""


def readable(version: int) -> bool:
    """Whether this build can fold a log written at ``version``."""

    return version in SUPPORTED_SCHEMA_VERSIONS


def describe(version: int) -> SchemaVersionRecord:
    """The history entry for one version.

    Raises rather than returning ``None`` because every readable version has an
    entry by construction -- :func:`check_policy` is what keeps that true -- so a
    miss here is a build defect and not a caller's bad input.
    """

    for record in SCHEMA_HISTORY:
        if record.version == version:
            return record
    raise EventValidationError(
        "no schema history entry for this version",
        details={"schema_version": version, "known": [item.version for item in SCHEMA_HISTORY]},
    )


def version_of(event_type: EventType) -> int:
    """The schema version that introduced one event type."""

    for record in SCHEMA_HISTORY:
        if event_type in record.added_events:
            return record.version
    raise EventValidationError(
        "event type is not attributed to any schema version",
        details={"event_type": event_type.value},
    )


def events_up_to(version: int) -> tuple[EventType, ...]:
    """Every event type a build at ``version`` could have written.

    Used to check a log against the version it claims: a v3 log carrying a
    ``memory_stored`` was not written by a v3 build, and whatever produced it is
    worth an operator's attention even though the event itself folds fine.
    """

    return tuple(
        event
        for record in SCHEMA_HISTORY
        if record.version <= version
        for event in record.added_events
    )


def required_fields(event_type: EventType) -> tuple[str, ...]:
    """Fields today's build refuses an event of this type without.

    This is the surface backward compatibility actually turns on, so it is
    exposed rather than left to be read off the payload classes: every name here
    is a field an older writer must already have written.
    """

    payload_type = PAYLOAD_TYPES[event_type]
    return tuple(
        sorted(name for name, field in payload_type.model_fields.items() if field.is_required())
    )


def unreadable(versions: object) -> tuple[int, ...]:
    """The versions in an observed set this build cannot read, sorted.

    Takes ``object`` and filters to integers because the caller is usually
    reading versions off disk, where a hand-edited log can carry a string.
    """

    if not isinstance(versions, list | tuple | set | frozenset):
        return ()
    return tuple(
        sorted(
            version
            for version in versions
            if isinstance(version, int) and not isinstance(version, bool) and not readable(version)
        )
    )


def check_policy() -> tuple[str, ...]:
    """Findings that would make the compatibility claim false. Empty is good.

    Every check here is a way the history and the code can drift apart while all
    the other tests still pass: a bumped version with no entry, a new event type
    nobody attributed, an entry naming a type that was later renamed, or a
    payload base that stopped keeping unknown keys.
    """

    findings: list[str] = []
    versions = [record.version for record in SCHEMA_HISTORY]

    if versions != sorted(set(versions)):
        findings.append(f"schema history is not strictly increasing: {versions}")
    if versions and versions != list(range(1, versions[-1] + 1)):
        findings.append(f"schema history skips a version: {versions}")
    if versions and versions[-1] != CURRENT_SCHEMA_VERSION:
        findings.append(
            f"history ends at v{versions[-1]} but the build writes v{CURRENT_SCHEMA_VERSION}"
        )
    if set(versions) != set(SUPPORTED_SCHEMA_VERSIONS):
        findings.append(
            "history and SUPPORTED_SCHEMA_VERSIONS disagree: "
            f"{sorted(set(versions) ^ set(SUPPORTED_SCHEMA_VERSIONS))}"
        )

    attributed = [event for record in SCHEMA_HISTORY for event in record.added_events]
    duplicates = sorted({event.value for event in attributed if attributed.count(event) > 1})
    if duplicates:
        findings.append(f"event types attributed to more than one version: {duplicates}")
    missing = sorted(event.value for event in EventType if event not in set(attributed))
    if missing:
        findings.append(f"event types with no schema version: {missing}")

    if Payload.model_config.get("extra") != "allow":
        findings.append("Payload no longer keeps unknown keys, so newer logs would be refused")

    return tuple(findings)


def history_report() -> dict[str, Any]:
    """The compatibility statement as one JSON object, for ``atlas verify``."""

    return {
        "current_schema_version": CURRENT_SCHEMA_VERSION,
        "supported_schema_versions": sorted(SUPPORTED_SCHEMA_VERSIONS),
        "event_types": len(EventType),
        "rules": list(COMPATIBILITY_RULES),
        "versions": [record.as_json() for record in SCHEMA_HISTORY],
        "policy_findings": list(check_policy()),
    }
