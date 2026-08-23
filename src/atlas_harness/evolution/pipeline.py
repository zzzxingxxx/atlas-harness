"""The controlled self-evolution loop, end to end.

Feedback arrives, a candidate is extracted from it, the candidate is screened against
the existing library, and only then is anything written down. Evaluation is a separate
call, and promotion a third, because each step is a decision an operator may want to
stop at -- a pipeline that ran all three on one call would make "propose" mean
"deploy", which is precisely what the pending window exists to prevent.

Nothing here decides on its own that a candidate should become effective. It reports
what each stage concluded and leaves the promotion to a caller who has read it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from pydantic import BaseModel, ConfigDict, Field

from atlas_harness.evolution.champion import ChampionRegistry, current_champion
from atlas_harness.evolution.evaluator import Evaluator
from atlas_harness.evolution.extractor import extract, group_by_task
from atlas_harness.evolution.merger import decide, explain
from atlas_harness.evolution.models import (
    CandidateDecision,
    EvaluationRecord,
    FeedbackItem,
    SkillCandidate,
)
from atlas_harness.evolution.repository import EvolutionRepository
from atlas_harness.kernel.errors import EventValidationError
from atlas_harness.skills.models import SkillRecord


class ProposalOutcome(BaseModel):
    """What happened to one proposal attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    candidate: SkillCandidate | None = None
    decision: CandidateDecision = CandidateDecision.REJECT
    reason: str | None = None
    detail: str | None = None
    notes: tuple[str, ...] = Field(default_factory=tuple)

    @property
    def accepted(self) -> bool:
        return self.candidate is not None and self.decision is not CandidateDecision.REJECT


class EvolutionPipeline:
    """Runs feedback through extraction, screening, evaluation and promotion."""

    def __init__(
        self,
        repository: EvolutionRepository,
        *,
        evaluator: Evaluator | None = None,
    ) -> None:
        self.repository = repository
        self.evaluator = evaluator
        self.champions = ChampionRegistry(repository.skills)

    # ------------------------------------------------------------------ propose

    def record_feedback(
        self,
        items: Iterable[FeedbackItem],
        *,
        session_id: str,
        operation_id: str | None = None,
    ) -> list[FeedbackItem]:
        return [
            self.repository.record_feedback(item, session_id=session_id, operation_id=operation_id)
            for item in items
        ]

    def propose_from(
        self,
        items: Sequence[FeedbackItem],
        *,
        session_id: str,
        operation_id: str | None = None,
        skill_id: str | None = None,
    ) -> ProposalOutcome:
        """Extract one candidate from feedback and store it if it survives screening.

        A refusal at either stage is written to the log as a rejection, so the audit
        trail distinguishes "nothing was proposed" from "something was proposed and
        refused" -- the second is a signal about the feedback, the first is silence.
        """

        result = extract(items, skill_id=skill_id)
        if result.candidate is None:
            refusal = result.rejected
            assert refusal is not None  # ExtractionResult is one or the other
            self.repository.reject(
                refusal.feedback_id or "",
                refusal.reason,
                session_id=session_id,
                operation_id=operation_id,
                detail=refusal.detail,
                skill_id=refusal.skill_id,
            )
            return ProposalOutcome(reason=refusal.reason, detail=refusal.detail)

        outcome = decide(result.candidate, self.repository.skills.all())
        if outcome.rejected:
            self.repository.reject(
                outcome.candidate.candidate_id,
                outcome.reason or "duplicate",
                session_id=session_id,
                operation_id=operation_id,
                detail=outcome.detail,
                skill_id=outcome.candidate.skill_id,
            )
            return ProposalOutcome(
                candidate=outcome.candidate,
                reason=outcome.reason,
                detail=outcome.detail,
                notes=(explain(outcome),),
            )

        stored = self.repository.propose(
            outcome.candidate,
            session_id=session_id,
            operation_id=operation_id,
        )
        return ProposalOutcome(
            candidate=stored,
            decision=outcome.decision,
            notes=(explain(outcome),),
        )

    def propose_all(
        self,
        items: Iterable[FeedbackItem],
        *,
        session_id: str,
        operation_id: str | None = None,
    ) -> list[ProposalOutcome]:
        """One proposal attempt per source task."""

        grouped = group_by_task(items)
        return [
            self.propose_from(group, session_id=session_id, operation_id=operation_id)
            for group in grouped.values()
        ]

    # ----------------------------------------------------------------- evaluate

    def evaluate(
        self,
        candidate_id: str,
        *,
        session_id: str,
        operation_id: str | None = None,
    ) -> EvaluationRecord:
        """Measure a stored candidate against the fixed task sets."""

        if self.evaluator is None:
            raise EventValidationError(
                "no evaluator configured; a candidate cannot be measured",
                details={"candidate_id": candidate_id},
            )
        candidate = self.repository.require_candidate(candidate_id)
        champion = current_champion(self.repository.skills, candidate.skill_id)
        record = self.evaluator.evaluate(candidate, champion=champion)
        return self.repository.record_evaluation(
            record, session_id=session_id, operation_id=operation_id
        )

    # ------------------------------------------------------------------ promote

    def promote(
        self,
        candidate_id: str,
        *,
        session_id: str,
        operation_id: str | None = None,
        reason: str | None = None,
    ) -> SkillRecord:
        """Promote a candidate using its most recent evaluation.

        The newest verdict is used rather than the best one. A candidate measured
        again after a model change has been re-measured for a reason, and letting a
        promotion reach back to an older passing run would make the gate depend on
        which verdict a caller chose to cite.
        """

        evaluation = self.repository.latest_evaluation(candidate_id)
        if evaluation is None:
            raise EventValidationError(
                "cannot promote a candidate that was never evaluated",
                details={"candidate_id": candidate_id},
            )
        promoted = self.champions.promote(
            evaluation,
            session_id=session_id,
            operation_id=operation_id,
            reason=reason,
        )
        self.repository.mark_promoted(candidate_id)
        return promoted

    def rollback(
        self,
        skill_id: str,
        to_version: str,
        *,
        session_id: str,
        operation_id: str | None = None,
        reason: str | None = None,
    ) -> SkillRecord:
        return self.champions.rollback(
            skill_id,
            to_version,
            session_id=session_id,
            operation_id=operation_id,
            reason=reason,
        )
