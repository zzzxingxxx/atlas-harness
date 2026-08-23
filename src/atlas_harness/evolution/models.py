"""Feedback, candidates and evaluation records.

Three records, one direction: feedback produces a candidate, a candidate produces an
evaluation, and only an evaluation can produce a promotion. Nothing in this module
can make a candidate effective -- that is deliberately the skill lifecycle's job,
because the ``candidate -> active`` edge is the gate the plan asks for and there
must be exactly one of it.

Each record keeps its own provenance rather than relying on the reader to join
tables. A candidate that names its feedback, and an evaluation that names its
candidate and the champion it was measured against, can be audited from the log
alone -- which matters because the log is the only artefact that survives a
projection rebuild.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from atlas_harness.events.models import (
    CANDIDATE_DECISIONS,
    CANDIDATE_REJECTIONS,
    EVALUATION_STAGES,
    EVALUATION_VERDICTS,
    FEEDBACK_KINDS,
    EvaluationMetrics,
)
from atlas_harness.kernel.errors import EventValidationError
from atlas_harness.skills.models import SkillRecord, SkillStatus


class FeedbackKind(StrEnum):
    CORRECTION = "correction"
    FAILURE = "failure"
    SUCCESS = "success"


class CandidateDecision(StrEnum):
    ADD = "add"
    MERGE = "merge"
    REJECT = "reject"


class CandidateStatus(StrEnum):
    """Where a candidate sits in the pending window.

    ``proposed`` is the pending state the milestone is named after: known, bound to
    its evidence, and not injectable. Everything else is an exit from that window.
    """

    PROPOSED = "proposed"
    REJECTED = "rejected"
    EVALUATED = "evaluated"
    PROMOTED = "promoted"


class EvaluationVerdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


def _checked(value: str, allowed: frozenset[str], label: str) -> str:
    if value not in allowed:
        raise EventValidationError(
            f"unknown {label}",
            details={label: value, "supported": sorted(allowed)},
        )
    return value


def parse_feedback_kind(value: str) -> FeedbackKind:
    return FeedbackKind(_checked(value, FEEDBACK_KINDS, "feedback kind"))


def parse_decision(value: str) -> CandidateDecision:
    return CandidateDecision(_checked(value, CANDIDATE_DECISIONS, "candidate decision"))


def parse_rejection(value: str) -> str:
    return _checked(value, CANDIDATE_REJECTIONS, "rejection reason")


def parse_stage(value: str) -> str:
    return _checked(value, EVALUATION_STAGES, "evaluation stage")


def parse_verdict(value: str) -> EvaluationVerdict:
    return EvaluationVerdict(_checked(value, EVALUATION_VERDICTS, "evaluation verdict"))


class FeedbackItem(BaseModel):
    """One correction, failure or success, with the evidence that supports it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    feedback_id: str = Field(min_length=1)
    kind: FeedbackKind = FeedbackKind.CORRECTION
    content: str = ""
    source_task: str | None = None
    source_session_id: str | None = None
    tool_name: str | None = None
    evidence_refs: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    created_at_ms: int = 0

    @property
    def has_evidence(self) -> bool:
        """Whether this item can support a candidate at all.

        The plan requires a candidate to bind to its source. An item with no
        evidence and no originating task cannot supply that binding, so it is
        recorded for the audit trail but never promoted into a proposal.
        """

        return bool(self.evidence_refs) or bool(self.source_task)

    def to_payload(self) -> dict[str, Any]:
        return {
            "feedback_id": self.feedback_id,
            "kind": self.kind.value,
            "content": self.content,
            "source_task": self.source_task,
            "source_session_id": self.source_session_id,
            "tool_name": self.tool_name,
            "evidence_refs": list(self.evidence_refs),
            "tags": list(self.tags),
            "created_at_ms": self.created_at_ms,
        }


class SkillCandidate(BaseModel):
    """A proposed skill version that is not yet a capability.

    It carries the same fields a :class:`~atlas_harness.skills.models.SkillRecord`
    needs, but converting one into the other lands at ``candidate`` status and
    nowhere else -- see :meth:`to_skill_record`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str = Field(min_length=1)
    skill_id: str = Field(min_length=1)
    version: str = Field(default="0.1.0", min_length=1)
    status: CandidateStatus = CandidateStatus.PROPOSED
    decision: CandidateDecision = CandidateDecision.ADD
    name: str | None = None
    description: str = ""
    body: str = ""
    triggers: tuple[str, ...] = ()
    required_scopes: tuple[str, ...] = ()
    feedback_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    merged_from: str | None = None
    reject_reason: str | None = None
    created_at_ms: int = 0

    @property
    def label(self) -> str:
        return f"{self.skill_id}@{self.version}"

    @property
    def is_pending(self) -> bool:
        return self.status is CandidateStatus.PROPOSED

    def to_skill_record(self) -> SkillRecord:
        """Render as a registrable skill version, always at ``candidate`` status.

        The status is hard-coded rather than taken from a parameter. A caller able
        to ask for ``active`` here would be a second promotion path with none of
        the evaluation checks, which is precisely what the pending window exists to
        prevent.
        """

        return SkillRecord(
            skill_id=self.skill_id,
            version=self.version,
            status=SkillStatus.CANDIDATE,
            name=self.name,
            description=self.description,
            body=self.body,
            required_scopes=self.required_scopes,
            triggers=self.triggers,
            evidence_refs=self.evidence_refs,
            source_task=None,
            registered_at_ms=self.created_at_ms,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "skill_id": self.skill_id,
            "version": self.version,
            "decision": self.decision.value,
            "name": self.name,
            "description": self.description,
            "body": self.body,
            "triggers": list(self.triggers),
            "required_scopes": list(self.required_scopes),
            "feedback_refs": list(self.feedback_refs),
            "evidence_refs": list(self.evidence_refs),
            "merged_from": self.merged_from,
            "created_at_ms": self.created_at_ms,
        }


class EvaluationRecord(BaseModel):
    """The result of measuring one candidate against the fixed task sets.

    ``baseline_metrics`` and ``champion_version`` travel with the verdict because a
    promotion decision is a comparison, and a stored verdict that cannot say what it
    was compared against is not reviewable after the fact.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    evaluation_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    skill_id: str = Field(min_length=1)
    version: str = Field(default="0.1.0", min_length=1)
    dataset: str = ""
    verdict: EvaluationVerdict = EvaluationVerdict.FAIL
    stages: tuple[str, ...] = ()
    failed_stages: tuple[str, ...] = ()
    metrics: EvaluationMetrics = Field(default_factory=EvaluationMetrics)
    baseline_metrics: EvaluationMetrics | None = None
    champion_version: str | None = None
    task_count: int = 0
    failures: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    evaluated_at_ms: int = 0

    @property
    def passed(self) -> bool:
        """Only an explicit pass counts.

        ``inconclusive`` is not a pass: an unreachable judge or an empty task set
        leaves the candidate unmeasured, and promoting the unmeasured is the failure
        mode the fixed benchmark exists to close.
        """

        return self.verdict is EvaluationVerdict.PASS

    def to_payload(self) -> dict[str, Any]:
        return {
            "evaluation_id": self.evaluation_id,
            "candidate_id": self.candidate_id,
            "skill_id": self.skill_id,
            "version": self.version,
            "dataset": self.dataset,
            "verdict": self.verdict.value,
            "stages": list(self.stages),
            "failed_stages": list(self.failed_stages),
            "metrics": self.metrics.model_dump(mode="python"),
            "baseline_metrics": (
                None if self.baseline_metrics is None else self.baseline_metrics.model_dump()
            ),
            "champion_version": self.champion_version,
            "task_count": self.task_count,
            "failures": list(self.failures),
            "notes": list(self.notes),
            "evaluated_at_ms": self.evaluated_at_ms,
        }
