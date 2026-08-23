"""Turning feedback into a candidate, and refusing the feedback that cannot support one.

Extraction is rule-based rather than model-driven. A model asked to write a skill from
one correction will produce something plausible for every correction, including the
ones that were about a typo, and the pending window would then fill with proposals
nobody wants to evaluate. So the rules here are deliberately strict about what counts
as a signal, and the checks below run before anything reaches the log.

Four things are checked, in this order, because a later check is more expensive and
would be wasted on input the earlier one already rejects:

``no_evidence``
    The feedback does not name a task or an artefact. The plan requires a candidate
    to bind to its source, and there is nothing here to bind to.

``low_signal``
    The text is too short, or the feedback is a bare success with nothing corrective
    in it. "Worked fine" is worth recording and not worth turning into an instruction.

``schema``
    The proposal does not satisfy the candidate contract -- no skill id, no body.

``security`` / ``lint``
    The body asks for a scope nobody granted, tries to smuggle a secret into a
    durable record, or is long enough to crowd out the conversation it joins.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from pydantic import BaseModel, ConfigDict

from atlas_harness.evolution.models import (
    CandidateDecision,
    FeedbackItem,
    FeedbackKind,
    SkillCandidate,
)
from atlas_harness.kernel.ids import new_id
from atlas_harness.skills.models import MAX_BODY_CHARS
from atlas_harness.tools.redaction import REDACTED, redact

MIN_CONTENT_CHARS = 20
"""Below this a correction is a reaction, not an instruction. Picked so "no, wrong"
does not become a skill while a one-sentence rule still can."""

MAX_TRIGGERS = 8

KNOWN_SCOPES: frozenset[str] = frozenset({"fs:read", "fs:write", "process:run", "net:request"})
"""Scopes a candidate is allowed to name. An unknown scope is a lint failure rather
than a silent grant: the policy would refuse the call anyway, and a skill that
assumes a permission the harness has never heard of is a typo at best."""

_STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "this",
        "that",
        "with",
        "from",
        "into",
        "when",
        "then",
        "than",
        "have",
        "should",
        "always",
        "never",
        "please",
        "instead",
        "because",
        "about",
        "would",
        "could",
        "there",
        "these",
        "those",
        "before",
        "after",
        "again",
        "just",
        "make",
        "made",
        "does",
        "done",
        "your",
        "yours",
    }
)

_WORD = re.compile(r"[a-z0-9_\-]{4,}")

_DANGEROUS_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\brm\s+-rf\b"), "body instructs a recursive delete"),
    (re.compile(r"(?i)\bcurl\b[^\n]*\|\s*(?:ba)?sh\b"), "body pipes a download into a shell"),
    (
        re.compile(r"(?i)\bignore\s+(?:all\s+)?previous\s+instructions\b"),
        "body overrides the prompt",
    ),
    (
        re.compile(r"(?i)\bdisable\s+(?:the\s+)?(?:policy|approval|sandbox)"),
        "body disables a control",
    ),
    (re.compile(r"(?i)--no-verify\b"), "body skips a verification hook"),
)
"""Text that must never reach a prompt as an instruction. This is a small, explicit
list rather than a classifier because a candidate refused here is refused in the
audit trail, and an operator has to be able to read why."""


class ExtractionRejected(BaseModel):
    """A refusal, carrying the reason code the log will record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: str
    detail: str
    feedback_id: str | None = None
    skill_id: str | None = None


class ExtractionResult(BaseModel):
    """Either a candidate or a refusal, never both and never neither."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate: SkillCandidate | None = None
    rejected: ExtractionRejected | None = None

    @property
    def accepted(self) -> bool:
        return self.candidate is not None


def keywords_of(text: str, *, limit: int = MAX_TRIGGERS) -> tuple[str, ...]:
    """Content words, deduplicated, in first-appearance order.

    Order is first-appearance rather than by frequency: a correction states its
    subject early, and frequency on a two-sentence input mostly ranks filler.
    """

    seen: list[str] = []
    for match in _WORD.finditer(text.lower()):
        word = match.group(0)
        if word in _STOPWORDS or word in seen:
            continue
        seen.append(word)
        if len(seen) >= limit:
            break
    return tuple(seen)


def slug_for(text: str, *, fallback: str = "learned-rule") -> str:
    """A stable, readable skill id derived from the feedback's own words."""

    words = keywords_of(text, limit=4)
    slug = "-".join(words)
    return slug or fallback


def scopes_in(text: str) -> tuple[str, ...]:
    """Scopes the text explicitly names, in the canonical ``area:action`` form."""

    found = [scope for scope in sorted(KNOWN_SCOPES) if scope in text]
    return tuple(found)


def _unknown_scopes(text: str) -> tuple[str, ...]:
    """Scope-shaped tokens that are not in the known set."""

    candidates = set(re.findall(r"\b(?:fs|net|process|system|admin|root):[a-z_]+", text))
    return tuple(sorted(candidates - KNOWN_SCOPES))


def lint(body: str, *, scopes: Sequence[str] = ()) -> tuple[str, ...]:
    """Problems that make a body unfit to inject, as readable sentences."""

    problems: list[str] = []
    if len(body) > MAX_BODY_CHARS:
        problems.append(f"body is {len(body)} chars, over the {MAX_BODY_CHARS} limit")
    unknown = tuple(scope for scope in scopes if scope not in KNOWN_SCOPES)
    if unknown:
        problems.append("body requires unknown scopes: " + ", ".join(unknown))
    unknown_in_text = _unknown_scopes(body)
    if unknown_in_text:
        problems.append("body names unknown scopes: " + ", ".join(unknown_in_text))
    return tuple(problems)


def security_problems(body: str) -> tuple[str, ...]:
    """Instructions a candidate must never carry into a prompt."""

    problems = [note for pattern, note in _DANGEROUS_PATTERNS if pattern.search(body)]
    if redact(body) != body:
        # The body held something credential-shaped. Redaction would blank it, but a
        # candidate whose instruction is a secret has no instruction left.
        problems.append("body contained a credential")
    return tuple(problems)


def extract(
    feedback: Iterable[FeedbackItem],
    *,
    skill_id: str | None = None,
    now_ms: int = 0,
) -> ExtractionResult:
    """Propose at most one candidate from a group of related feedback items.

    One candidate per group, not per item. Three corrections about the same mistake
    describe one rule, and emitting three near-identical proposals would make the
    merge step do work the extractor could have avoided.
    """

    items = tuple(feedback)
    if not items:
        return ExtractionResult(
            rejected=ExtractionRejected(reason="no_evidence", detail="no feedback supplied")
        )

    bound = tuple(item for item in items if item.has_evidence)
    if not bound:
        return ExtractionResult(
            rejected=ExtractionRejected(
                reason="no_evidence",
                detail="no feedback item names a source task or evidence",
                feedback_id=items[0].feedback_id,
            )
        )

    corrective = tuple(item for item in bound if item.kind is not FeedbackKind.SUCCESS)
    signal = corrective or bound
    content = "\n".join(item.content for item in signal if item.content).strip()

    if len(content) < MIN_CONTENT_CHARS:
        return ExtractionResult(
            rejected=ExtractionRejected(
                reason="low_signal",
                detail=f"combined feedback is {len(content)} chars, under {MIN_CONTENT_CHARS}",
                feedback_id=signal[0].feedback_id,
            )
        )
    if not corrective:
        return ExtractionResult(
            rejected=ExtractionRejected(
                reason="low_signal",
                detail="success feedback alone does not describe a rule to follow",
                feedback_id=bound[0].feedback_id,
            )
        )

    resolved_id = skill_id or slug_for(content)
    body = redact(content)
    if REDACTED in body:
        return ExtractionResult(
            rejected=ExtractionRejected(
                reason="security",
                detail="feedback contained a credential",
                feedback_id=signal[0].feedback_id,
                skill_id=resolved_id,
            )
        )

    scopes = scopes_in(body)
    problems = security_problems(body)
    if problems:
        return ExtractionResult(
            rejected=ExtractionRejected(
                reason="security",
                detail="; ".join(problems),
                feedback_id=signal[0].feedback_id,
                skill_id=resolved_id,
            )
        )

    lint_problems = lint(body, scopes=scopes)
    if lint_problems:
        return ExtractionResult(
            rejected=ExtractionRejected(
                reason="lint",
                detail="; ".join(lint_problems),
                feedback_id=signal[0].feedback_id,
                skill_id=resolved_id,
            )
        )

    evidence = _unique(ref for item in signal for ref in item.evidence_refs)
    tasks = _unique(item.source_task for item in signal if item.source_task)
    if not evidence:
        # A task name is weaker evidence than an artefact reference, but it is still
        # a source an auditor can go look at, so it is recorded as one.
        evidence = tuple(f"task:{task}" for task in tasks)

    candidate = SkillCandidate(
        candidate_id=new_id("cand"),
        skill_id=resolved_id,
        version="0.1.0",
        decision=CandidateDecision.ADD,
        name=resolved_id.replace("-", " "),
        description=_first_sentence(body),
        body=body,
        triggers=keywords_of(body),
        required_scopes=scopes,
        feedback_refs=tuple(item.feedback_id for item in signal),
        evidence_refs=evidence,
        created_at_ms=now_ms,
    )
    if not candidate.body.strip():
        return ExtractionResult(
            rejected=ExtractionRejected(
                reason="schema",
                detail="candidate body is empty",
                skill_id=resolved_id,
            )
        )
    return ExtractionResult(candidate=candidate)


def group_by_task(feedback: Iterable[FeedbackItem]) -> dict[str, tuple[FeedbackItem, ...]]:
    """Bucket feedback by the task it came from, keeping arrival order.

    Grouping by task rather than by kind is what lets one rule be extracted from a
    failure and the correction that followed it, which is the pair that actually
    describes what should have happened.
    """

    grouped: dict[str, list[FeedbackItem]] = {}
    for item in feedback:
        grouped.setdefault(item.source_task or "", []).append(item)
    return {key: tuple(value) for key, value in grouped.items()}


def _unique(values: Iterable[str | None]) -> tuple[str, ...]:
    seen: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
    return tuple(seen)


def _first_sentence(text: str, *, limit: int = 160) -> str:
    flat = " ".join(text.split())
    for stop in (". ", "\n"):
        head, _, _ = flat.partition(stop)
        if head != flat:
            flat = head
            break
    return flat[:limit]
