"""Deciding whether a candidate is new, a revision of something, or noise.

Every proposal is checked against the skills that already exist before it is written
down. Skipping that check would let the library grow a second, slightly different
copy of an existing skill every time the same feedback arrived twice, and two skills
that both claim the same trigger make retrieval pick one arbitrarily -- which reads
to an operator as the harness ignoring the skill they just added.

Three outcomes, matching the plan's vocabulary:

``add``
    Nothing similar exists. The candidate stands on its own.

``merge``
    Something similar is already active. The candidate becomes the *next version of
    that skill* rather than a sibling, so promoting it displaces the old version and
    a rollback has a named place to return to.

``reject``
    Either it duplicates an existing version outright, or it fails one of the checks
    below. A rejection is recorded with its reason: a candidate refused by the
    security check was never measured, and an audit that could not tell that apart
    from a failing score would read a refusal as a bad benchmark result.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from atlas_harness.evolution.models import (
    CandidateDecision,
    SkillCandidate,
)
from atlas_harness.skills.models import MAX_BODY_CHARS, SkillRecord, checksum_for

MERGE_SIMILARITY = 0.45
"""Above this, a candidate is treated as a revision of an existing skill instead of a
new one. Tuned low on purpose: a duplicate that slips through as ``add`` splits
retrieval between two near-identical skills, while a false merge only means the
operator sees one version chain instead of two, which is visible and reversible."""

DUPLICATE_SIMILARITY = 0.92
"""Near-identical text. Merging at this point would produce a new version whose
body says what the old one already said, so the candidate is rejected instead."""

MIN_BODY_CHARS = 24
"""Shorter than this is not an instruction. A one-word body passes every structural
check while telling the model nothing, and it would still occupy a capability slot."""

MAX_TRIGGERS = 12

_UNSAFE_BODY_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(?i)\bignore\s+(?:all\s+|any\s+)?(?:previous|prior|earlier)\b"),
        "prompt override",
    ),
    (
        re.compile(r"(?i)\bdisregard\s+(?:your\s+)?(?:instructions|system\s+prompt|rules)"),
        "prompt override",
    ),
    (re.compile(r"(?i)\byou\s+are\s+now\s+(?:a|an)\b"), "identity override"),
    (
        re.compile(r"(?i)\bskip\s+(?:the\s+)?(?:approval|confirmation|permission)"),
        "approval bypass",
    ),
    (re.compile(r"(?i)\b(?:without|no)\s+(?:asking|confirming|approval)\b"), "approval bypass"),
    (
        re.compile(r"(?i)\bdisable\s+(?:the\s+)?(?:policy|sandbox|safety|guardrail)"),
        "policy bypass",
    ),
    (re.compile(r"(?i)--no-verify\b"), "hook bypass"),
    (re.compile(r"(?i)\brm\s+-rf\s+/"), "destructive command"),
    (re.compile(r"(?i)\bcurl\b[^\n]*\|\s*(?:ba)?sh\b"), "remote execution"),
)
"""Text a skill body must not contain. These are checked because a skill body is
instruction text that reaches the model on every matching request, so a body that
tells the model to bypass approval is a durable policy hole rather than a one-off
bad request. Pattern matching cannot catch a determined author -- the evaluation
stages and the active-only injection filter are the real defences -- but it catches
the accidental case cheaply, before anything is written down."""

_SCOPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*:[a-z][a-z0-9_]*$")

_WORD_PATTERN = re.compile(r"[\w']+", re.UNICODE)

_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "for",
        "from",
        "if",
        "in",
        "into",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "then",
        "there",
        "these",
        "this",
        "to",
        "was",
        "were",
        "when",
        "which",
        "with",
        "you",
        "your",
    }
)


def tokenize(text: str) -> frozenset[str]:
    """Content words of a text, lowercased.

    Stopwords are dropped because they appear in every skill: leaving them in would
    put a floor under every similarity score and make two unrelated skills look
    related just for being written in English.
    """

    return frozenset(
        word
        for word in (match.group().lower() for match in _WORD_PATTERN.finditer(text))
        if word not in _STOPWORDS and len(word) > 1
    )


def similarity(left: str, right: str) -> float:
    """Jaccard overlap of two texts' content words.

    Jaccard rather than a substring ratio: it does not care about word order, so a
    reworded version of the same instruction still reads as similar, and reordering
    is the most common thing a revision does.
    """

    first = tokenize(left)
    second = tokenize(right)
    if not first or not second:
        return 0.0
    return len(first & second) / len(first | second)


def candidate_text(candidate: SkillCandidate) -> str:
    return " ".join(
        part for part in (candidate.name or "", candidate.description, candidate.body) if part
    )


def skill_text(record: SkillRecord) -> str:
    return " ".join(part for part in (record.name or "", record.description, record.body) if part)


def check_schema(candidate: SkillCandidate) -> str | None:
    """Structural problems that make a candidate unusable. Returns a detail or None."""

    if not candidate.skill_id.strip():
        return "skill_id is empty"
    if len(candidate.body.strip()) < MIN_BODY_CHARS:
        return f"body is shorter than {MIN_BODY_CHARS} characters"
    if len(candidate.body) > MAX_BODY_CHARS:
        return f"body exceeds {MAX_BODY_CHARS} characters"
    if not candidate.description.strip():
        return "description is empty"
    return None


def check_lint(candidate: SkillCandidate) -> str | None:
    """Problems that would make the skill behave badly rather than fail outright."""

    if len(candidate.triggers) > MAX_TRIGGERS:
        return f"more than {MAX_TRIGGERS} triggers"
    if any(not trigger.strip() for trigger in candidate.triggers):
        return "a trigger is blank"
    if len(set(candidate.triggers)) != len(candidate.triggers):
        return "triggers repeat"
    for scope in candidate.required_scopes:
        if not _SCOPE_PATTERN.match(scope):
            return f"malformed scope: {scope}"
    return None


def check_security(candidate: SkillCandidate) -> str | None:
    """Instruction text that tries to widen its own authority."""

    haystack = f"{candidate.description}\n{candidate.body}"
    for pattern, label in _UNSAFE_BODY_PATTERNS:
        if pattern.search(haystack):
            return label
    return None


def check_evidence(candidate: SkillCandidate) -> str | None:
    """Whether the candidate is bound to anything.

    The plan's acceptance condition is that a candidate must bind to its source and
    evidence. A candidate with neither cannot be reviewed -- an operator asked to
    approve it has no way to see what it came from -- so it never reaches evaluation.
    """

    if not candidate.feedback_refs:
        return "no feedback bound to this candidate"
    if not candidate.evidence_refs:
        return "no evidence bound to this candidate"
    return None


_CHECKS: tuple[tuple[str, Callable[[SkillCandidate], str | None]], ...] = (
    ("schema", check_schema),
    ("lint", check_lint),
    ("security", check_security),
    ("no_evidence", check_evidence),
)
"""Every check that runs before evaluation, paired with the rejection reason it
produces. The reasons are drawn from ``CANDIDATE_REJECTIONS`` so a rejection event
never carries a string the contract does not know."""


def screen(candidate: SkillCandidate) -> tuple[str, str] | None:
    """Run every pre-evaluation check, returning ``(reason, detail)`` on the first
    failure.

    Order is cheapest-first, but only up to a point: the security check runs before
    the evidence check even though evidence is cheaper to test, because a body trying
    to bypass approval should be reported as a security rejection rather than as a
    paperwork problem.
    """

    for reason, check in _CHECKS:
        detail = check(candidate)
        if detail is not None:
            return (reason, detail)
    return None


class MergeOutcome:
    """What retrieval decided, and the candidate rewritten to match that decision."""

    __slots__ = ("candidate", "decision", "detail", "reason", "similar_to", "score")

    def __init__(
        self,
        *,
        candidate: SkillCandidate,
        decision: CandidateDecision,
        similar_to: SkillRecord | None = None,
        score: float = 0.0,
        reason: str | None = None,
        detail: str | None = None,
    ) -> None:
        self.candidate = candidate
        self.decision = decision
        self.similar_to = similar_to
        self.score = score
        self.reason = reason
        self.detail = detail

    @property
    def rejected(self) -> bool:
        return self.decision is CandidateDecision.REJECT


def next_version(version: str) -> str:
    """Bump the minor component, so a merge produces a new version rather than
    overwriting the one currently serving requests."""

    parts = version.split(".")
    numbers: list[int] = []
    for chunk in parts:
        try:
            numbers.append(int(chunk))
        except ValueError:
            numbers.append(0)
    while len(numbers) < 3:
        numbers.append(0)
    numbers[1] += 1
    numbers[2] = 0
    return ".".join(str(number) for number in numbers[:3])


def decide(candidate: SkillCandidate, existing: list[SkillRecord]) -> MergeOutcome:
    """Compare a candidate against the current library and choose add, merge or reject.

    ``existing`` is every known version, not only the active ones. A candidate that
    duplicates a version an operator already retired should not come back as new: the
    decision to retire it was a decision, and re-adding the same text under a fresh id
    would quietly undo it.
    """

    failure = screen(candidate)
    if failure is not None:
        reason, detail = failure
        return MergeOutcome(
            candidate=candidate.model_copy(update={"decision": CandidateDecision.REJECT}),
            decision=CandidateDecision.REJECT,
            reason=reason,
            detail=detail,
        )

    text = candidate_text(candidate)
    body_checksum = checksum_for(candidate.body)

    best: SkillRecord | None = None
    best_score = 0.0
    for record in existing:
        if record.checksum == body_checksum:
            return MergeOutcome(
                candidate=candidate.model_copy(update={"decision": CandidateDecision.REJECT}),
                decision=CandidateDecision.REJECT,
                similar_to=record,
                score=1.0,
                reason="duplicate",
                detail=f"identical body to {record.label}",
            )
        score = similarity(text, skill_text(record))
        if score > best_score or (score == best_score and best is None):
            best = record
            best_score = score

    if best is None or best_score < MERGE_SIMILARITY:
        return MergeOutcome(
            candidate=candidate.model_copy(update={"decision": CandidateDecision.ADD}),
            decision=CandidateDecision.ADD,
            similar_to=best,
            score=best_score,
        )

    if best_score >= DUPLICATE_SIMILARITY:
        return MergeOutcome(
            candidate=candidate.model_copy(update={"decision": CandidateDecision.REJECT}),
            decision=CandidateDecision.REJECT,
            similar_to=best,
            score=best_score,
            reason="duplicate",
            detail=f"{best_score:.2f} similar to {best.label}",
        )

    merged = candidate.model_copy(
        update={
            "decision": CandidateDecision.MERGE,
            "skill_id": best.skill_id,
            "version": next_version(best.version),
            "merged_from": best.label,
            # The existing skill's triggers are kept: they are how requests reach it
            # today, and a merge that dropped them would silently stop answering
            # queries the old version handled.
            "triggers": tuple(dict.fromkeys((*best.triggers, *candidate.triggers))),
            "required_scopes": tuple(
                sorted(set(best.required_scopes) | set(candidate.required_scopes))
            ),
        }
    )
    return MergeOutcome(
        candidate=merged,
        decision=CandidateDecision.MERGE,
        similar_to=best,
        score=best_score,
    )


def explain(outcome: MergeOutcome) -> str:
    """One line an operator can read, naming the skill that drove the decision."""

    target = outcome.similar_to.label if outcome.similar_to is not None else "-"
    if outcome.rejected:
        return f"reject ({outcome.reason}): {outcome.detail or ''} [closest={target}]"
    if outcome.decision is CandidateDecision.MERGE:
        return (
            f"merge into {target} as {outcome.candidate.version} (similarity={outcome.score:.2f})"
        )
    return f"add {outcome.candidate.label} (closest={target}, similarity={outcome.score:.2f})"
