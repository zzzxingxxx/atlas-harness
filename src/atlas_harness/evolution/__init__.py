"""Controlled self-evolution: feedback in, evaluated skill versions out.

The package is split by decision rather than by data type. :mod:`extractor` decides
whether feedback describes a rule at all, :mod:`merger` decides whether that rule is
new, :mod:`evaluator` decides whether it works, and :mod:`champion` decides what is
effective. Only the last one can change what a request sees.
"""

from __future__ import annotations

from atlas_harness.evolution.champion import (
    ChampionRegistry,
    current_champion,
    rollback_targets,
)
from atlas_harness.evolution.evaluator import (
    COST_PER_1K_TOKENS,
    STAGE_JUDGE,
    STAGE_RULES,
    STAGE_SHADOW,
    Evaluator,
    Judge,
    TaskRunner,
    cost_of,
    run_datasets,
)
from atlas_harness.evolution.extractor import (
    KNOWN_SCOPES,
    MIN_CONTENT_CHARS,
    ExtractionRejected,
    ExtractionResult,
    extract,
    group_by_task,
    keywords_of,
    lint,
    security_problems,
    slug_for,
)
from atlas_harness.evolution.merger import (
    DUPLICATE_SIMILARITY,
    MERGE_SIMILARITY,
    MIN_BODY_CHARS,
    MergeOutcome,
    check_evidence,
    check_lint,
    check_schema,
    check_security,
    decide,
    explain,
    next_version,
    screen,
    similarity,
)
from atlas_harness.evolution.models import (
    CandidateDecision,
    CandidateStatus,
    EvaluationRecord,
    EvaluationVerdict,
    FeedbackItem,
    FeedbackKind,
    SkillCandidate,
    parse_decision,
    parse_feedback_kind,
    parse_rejection,
    parse_stage,
    parse_verdict,
)
from atlas_harness.evolution.pipeline import EvolutionPipeline, ProposalOutcome
from atlas_harness.evolution.repository import EvolutionRepository

__all__ = [
    "COST_PER_1K_TOKENS",
    "DUPLICATE_SIMILARITY",
    "KNOWN_SCOPES",
    "MERGE_SIMILARITY",
    "MIN_BODY_CHARS",
    "MIN_CONTENT_CHARS",
    "STAGE_JUDGE",
    "STAGE_RULES",
    "STAGE_SHADOW",
    "CandidateDecision",
    "CandidateStatus",
    "ChampionRegistry",
    "EvaluationRecord",
    "EvaluationVerdict",
    "Evaluator",
    "EvolutionPipeline",
    "EvolutionRepository",
    "ExtractionRejected",
    "ExtractionResult",
    "FeedbackItem",
    "FeedbackKind",
    "Judge",
    "MergeOutcome",
    "ProposalOutcome",
    "SkillCandidate",
    "TaskRunner",
    "check_evidence",
    "check_lint",
    "check_schema",
    "check_security",
    "cost_of",
    "current_champion",
    "decide",
    "explain",
    "extract",
    "group_by_task",
    "keywords_of",
    "lint",
    "next_version",
    "parse_decision",
    "parse_feedback_kind",
    "parse_rejection",
    "parse_stage",
    "parse_verdict",
    "rollback_targets",
    "run_datasets",
    "screen",
    "security_problems",
    "similarity",
    "slug_for",
]
