"""Promoting an evaluated candidate, and putting it back when it was a mistake.

This is the only module that can make a candidate effective, and it refuses to do so
without a passing evaluation. The check is not a convenience: the plan's invariant is
that a candidate must not affect the effective version before it passes, and a code
path that could promote without a verdict would be a second way in that none of the
evaluation machinery guards.

Promotion is two status changes, not one. The incoming version becomes ``active`` and
the outgoing version becomes ``deprecated`` -- deprecated rather than retired, because
``deprecated -> active`` is a legal edge and ``retired -> anything`` is not. Retiring
the displaced version would make the promotion irreversible, which is the opposite of
what a champion mechanism is for.

The order is activate-then-deprecate. For an instant both versions read as active, and
:meth:`~atlas_harness.skills.repository.SkillRepository.active` resolves that to the
higher version deterministically. The alternative order leaves an instant with *no*
active version, and a request arriving in that window would silently run without the
skill -- a worse failure than one resolved by a rule.
"""

from __future__ import annotations

from atlas_harness.events.models import EventType
from atlas_harness.evolution.models import EvaluationRecord
from atlas_harness.kernel.errors import EventValidationError
from atlas_harness.skills.models import SkillRecord, SkillStatus
from atlas_harness.skills.repository import SkillRepository


def current_champion(skills: SkillRepository, skill_id: str) -> SkillRecord | None:
    """The version currently serving requests for one skill, if any."""

    return next((record for record in skills.active() if record.skill_id == skill_id), None)


def rollback_targets(skills: SkillRepository, skill_id: str) -> list[SkillRecord]:
    """Versions a rollback could return to, newest first.

    Only ``deprecated`` versions qualify. A ``retired`` version was deliberately taken
    out of service and the lifecycle graph has no edge back; a ``candidate`` was never
    in service, so returning to it would be a promotion wearing a rollback's name.
    """

    found = [
        record
        for record in skills.all(status=SkillStatus.DEPRECATED)
        if record.skill_id == skill_id
    ]
    return sorted(found, key=lambda record: record.version, reverse=True)


class ChampionRegistry:
    """Applies and reverses promotions over one skill repository."""

    def __init__(self, skills: SkillRepository) -> None:
        self.skills = skills

    def promote(
        self,
        evaluation: EvaluationRecord,
        *,
        session_id: str,
        operation_id: str | None = None,
        reason: str | None = None,
    ) -> SkillRecord:
        """Make the evaluated version effective, deprecating the one it displaces.

        Raises when the evaluation did not pass. The candidate stays at ``candidate``
        status in that case, which is the same state it was already in -- a refused
        promotion leaves nothing half-applied.
        """

        if not evaluation.passed:
            raise EventValidationError(
                "cannot promote a candidate that did not pass evaluation",
                details={
                    "candidate_id": evaluation.candidate_id,
                    "skill_id": evaluation.skill_id,
                    "version": evaluation.version,
                    "verdict": evaluation.verdict.value,
                    "failed_stages": list(evaluation.failed_stages),
                },
            )

        incoming = self.skills.get(evaluation.skill_id, evaluation.version)
        if incoming is None:
            raise EventValidationError(
                "cannot promote a version that was never registered",
                details={"skill_id": evaluation.skill_id, "version": evaluation.version},
            )

        outgoing = current_champion(self.skills, evaluation.skill_id)
        if outgoing is not None and outgoing.version == evaluation.version:
            return outgoing

        promoted = self.skills.set_status(
            evaluation.skill_id,
            evaluation.version,
            SkillStatus.ACTIVE,
            session_id=session_id,
            operation_id=operation_id,
            reason=reason or "promoted after evaluation",
            evaluation_ref=evaluation.evaluation_id,
        )
        if outgoing is not None:
            self.skills.set_status(
                outgoing.skill_id,
                outgoing.version,
                SkillStatus.DEPRECATED,
                session_id=session_id,
                operation_id=operation_id,
                reason=f"displaced by {promoted.label}",
                evaluation_ref=evaluation.evaluation_id,
            )

        self.skills.store.append_new(
            EventType.CHAMPION_PROMOTED,
            session_id=session_id,
            operation_id=operation_id,
            payload={
                "skill_id": evaluation.skill_id,
                "to_version": evaluation.version,
                "from_version": None if outgoing is None else outgoing.version,
                "candidate_id": evaluation.candidate_id,
                "evaluation_id": evaluation.evaluation_id,
                "reason": reason,
            },
        )
        return promoted

    def rollback(
        self,
        skill_id: str,
        to_version: str,
        *,
        session_id: str,
        operation_id: str | None = None,
        reason: str | None = None,
        evaluation_id: str | None = None,
    ) -> SkillRecord:
        """Return the effective version to a named earlier one.

        The version is named by the caller rather than inferred as "the previous one".
        Inferring it would make the destination depend on how many promotions happened
        since, and the one operation that must land somewhere known is this one.
        """

        target = self.skills.get(skill_id, to_version)
        if target is None:
            raise EventValidationError(
                "cannot roll back to a version that does not exist",
                details={
                    "skill_id": skill_id,
                    "to_version": to_version,
                    "available": [
                        record.version for record in rollback_targets(self.skills, skill_id)
                    ],
                },
            )

        outgoing = current_champion(self.skills, skill_id)
        if outgoing is not None and outgoing.version == to_version:
            return outgoing

        # Deprecate first here, unlike promotion: the rollback target is already
        # deprecated, and activating it while the current champion is still active
        # would leave the higher version -- the one being rolled back -- still winning.
        if outgoing is not None:
            self.skills.set_status(
                outgoing.skill_id,
                outgoing.version,
                SkillStatus.DEPRECATED,
                session_id=session_id,
                operation_id=operation_id,
                reason=reason or f"rolled back to {to_version}",
                evaluation_ref=evaluation_id,
            )
        restored = self.skills.set_status(
            skill_id,
            to_version,
            SkillStatus.ACTIVE,
            session_id=session_id,
            operation_id=operation_id,
            reason=reason or "rollback",
            evaluation_ref=evaluation_id,
        )
        self.skills.store.append_new(
            EventType.CHAMPION_ROLLED_BACK,
            session_id=session_id,
            operation_id=operation_id,
            payload={
                "skill_id": skill_id,
                "to_version": to_version,
                "from_version": None if outgoing is None else outgoing.version,
                "reason": reason,
                "evaluation_id": evaluation_id,
            },
        )
        return restored
