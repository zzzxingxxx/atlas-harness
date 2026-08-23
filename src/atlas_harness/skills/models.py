"""Skill metadata, lifecycle status and the scopes a skill needs to be usable.

A skill is an instruction that reaches the model, so two properties decide its
shape: which permissions its instructions assume, and whether this version has
earned the right to be read.

Permissions come first. A skill that tells the model to fetch a URL is useless in
a session with no network grant, and worse than useless — it invites a call that
the policy will refuse, spending an iteration to learn something the harness knew
before the request went out. :meth:`SkillRecord.is_permitted` compares
``required_scopes`` against the granted set, and the selector treats a shortfall as
``not_permitted`` rather than as a low score.

Status comes second. The plan is explicit that a candidate must not affect the
effective version before it passes evaluation, so only ``active`` is injectable.
Registration and activation are separate events for the same reason: knowing about
a skill and trusting it are different facts, and collapsing them would make a
newly-loaded draft immediately readable by the model.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from atlas_harness.events.models import INJECTABLE_SKILL_STATUSES, SKILL_STATUSES
from atlas_harness.kernel.errors import EventValidationError

MAX_BODY_CHARS = 4_000
"""Cap on the instruction text rendered into a prompt. A skill long enough to
crowd out the conversation is a document, not a skill."""


class SkillStatus(StrEnum):
    DRAFT = "draft"
    CANDIDATE = "candidate"
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    RETIRED = "retired"

    @property
    def is_injectable(self) -> bool:
        """Whether this status may reach the model.

        Reads :data:`INJECTABLE_SKILL_STATUSES` rather than testing ``is ACTIVE``
        so the set stays the single place that decides.
        """

        return self.value in INJECTABLE_SKILL_STATUSES


ALLOWED_TRANSITIONS: dict[SkillStatus, frozenset[SkillStatus]] = {
    SkillStatus.DRAFT: frozenset({SkillStatus.CANDIDATE, SkillStatus.RETIRED}),
    SkillStatus.CANDIDATE: frozenset({SkillStatus.ACTIVE, SkillStatus.DRAFT, SkillStatus.RETIRED}),
    SkillStatus.ACTIVE: frozenset({SkillStatus.DEPRECATED, SkillStatus.RETIRED}),
    SkillStatus.DEPRECATED: frozenset({SkillStatus.ACTIVE, SkillStatus.RETIRED}),
    SkillStatus.RETIRED: frozenset(),
}
"""The lifecycle as a graph. ``draft`` cannot jump straight to ``active``: the
evaluation gate the plan asks for is exactly the edge that is missing here."""


def parse_status(value: str) -> SkillStatus:
    if value not in SKILL_STATUSES:
        raise EventValidationError(
            "unknown skill status",
            details={"status": value, "supported": sorted(SKILL_STATUSES)},
        )
    return SkillStatus(value)


def checksum_for(body: str) -> str:
    """Content hash of a skill body, used to tell two versions apart."""

    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class SkillRecord(BaseModel):
    """One version of one skill.

    Identity is ``(skill_id, version)``, not ``skill_id``. Two versions coexist on
    purpose: promoting a candidate must not overwrite the active version that is
    still serving requests, and a rollback needs somewhere to roll back to.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    skill_id: str = Field(min_length=1)
    version: str = Field(default="0.1.0", min_length=1)
    status: SkillStatus = SkillStatus.DRAFT
    name: str | None = None
    description: str = ""
    body: str = ""
    source_path: str | None = None
    checksum: str | None = None
    required_scopes: tuple[str, ...] = ()
    triggers: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    source_task: str | None = None
    registered_at_ms: int = 0

    @property
    def key(self) -> tuple[str, str]:
        return (self.skill_id, self.version)

    @property
    def label(self) -> str:
        return f"{self.skill_id}@{self.version}"

    def is_permitted(self, granted_scopes: frozenset[str]) -> bool:
        """Whether every scope this skill assumes has actually been granted."""

        return set(self.required_scopes).issubset(granted_scopes)

    def missing_scopes(self, granted_scopes: frozenset[str]) -> tuple[str, ...]:
        return tuple(sorted(set(self.required_scopes) - granted_scopes))

    def is_injectable(self, granted_scopes: frozenset[str]) -> bool:
        return self.status.is_injectable and self.is_permitted(granted_scopes)

    def matches(self, query: str) -> bool:
        """Cheap rule prefilter: does any declared trigger appear in the query?

        This runs before retrieval so an obvious match never depends on BM25 having
        indexed the right words. A skill with no triggers declines to answer here
        and is left to the text search.
        """

        if not self.triggers:
            return False
        lowered = query.lower()
        return any(trigger.lower() in lowered for trigger in self.triggers if trigger)

    def as_context_text(self) -> str:
        """Render for the capability slot, version and provenance included.

        The version travels with the body so a trace reader can tell which revision
        the model actually read, which is what makes a behaviour change traceable to
        a skill change.
        """

        head = f"[skill:{self.skill_id} v{self.version}]"
        parts = [f"{head} {self.name or self.skill_id}"]
        if self.description:
            parts.append(self.description)
        if self.body:
            parts.append(self.body[:MAX_BODY_CHARS])
        if self.required_scopes:
            parts.append("requires: " + ", ".join(self.required_scopes))
        if self.evidence_refs:
            parts.append("evidence: " + ", ".join(self.evidence_refs))
        return "\n".join(parts)

    def to_payload(self) -> dict[str, object]:
        return {
            "skill_id": self.skill_id,
            "version": self.version,
            "status": self.status.value,
            "name": self.name,
            "description": self.description,
            "body": self.body,
            "source_path": self.source_path,
            "checksum": self.checksum or checksum_for(self.body),
            "required_scopes": list(self.required_scopes),
            "triggers": list(self.triggers),
            "evidence_refs": list(self.evidence_refs),
            "source_task": self.source_task,
            "registered_at_ms": self.registered_at_ms,
        }


def can_transition(from_status: SkillStatus, to_status: SkillStatus) -> bool:
    return to_status in ALLOWED_TRANSITIONS[from_status]


def check_transition(from_status: SkillStatus, to_status: SkillStatus) -> None:
    """Refuse a lifecycle jump the graph does not allow.

    Raised rather than silently corrected: a promotion that skipped evaluation is a
    governance failure, and quietly rewriting it to a legal edge would hide it.
    """

    if not can_transition(from_status, to_status):
        raise EventValidationError(
            "illegal skill status transition",
            details={
                "from_status": from_status.value,
                "to_status": to_status.value,
                "allowed": sorted(status.value for status in ALLOWED_TRANSITIONS[from_status]),
            },
        )
