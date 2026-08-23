"""Choosing which memories and skills reach the model, and recording why.

This is the pipeline the plan asks for, in order: rule prefilter, retrieval,
permission filter, token ordering, then a small injection. Each stage can only
reject; nothing that a later stage sees was hidden from an earlier one. That makes
the outcome explainable, which is the actual completion condition — a trace has to
say why a skill the operator expected is absent.

Rejections are first-class here. :class:`CapabilityPlan` carries ``skipped``
alongside ``selected``, each skip naming one of the closed
:data:`~atlas_harness.events.models.SKIP_REASONS`. A selector that returned only
its winners would be impossible to audit: an empty capability slot would look
identical whether nothing matched, everything was unpermitted, or the budget was
zero.

Two rules are enforced regardless of score:

*Permission is not a ranking signal.* A skill whose ``required_scopes`` exceed the
grant is rejected as ``not_permitted``, never merely down-weighted. Injecting it
would invite a call the policy refuses, spending an iteration to discover what the
harness already knew.

*Expiry is not a ranking signal either.* An expired episodic memory is ineligible,
not unlucky. Retrieval filters it before ranking so it cannot displace an eligible
record from a limit-N result.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from atlas_harness.context.compiler import ContextItem, Slot
from atlas_harness.context.tokens import EstimatingCounter, TokenCounter
from atlas_harness.memory.models import MemoryRecord
from atlas_harness.memory.retrieval import DEFAULT_LIMIT, MemoryRetriever
from atlas_harness.model.protocol import ModelMessage, Role, TokenInput
from atlas_harness.skills.models import SkillRecord
from atlas_harness.skills.repository import SkillRepository
from atlas_harness.tools.manifest import DEFAULT_SCOPES

MAX_MEMORIES = 5
MAX_SKILLS = 3
"""Injection stays small on purpose. The plan says 少量注入: a capability slot
holding a dozen items is a second conversation competing with the first."""

DEFAULT_CAPABILITY_TOKENS = 1_500
"""Token ceiling for the whole slot. Separate from the prompt budget so a large
memory store cannot quietly consume the room reserved for the transcript."""

MIN_SCORE = 0.0
"""Below this a match is noise. Recorded as ``below_threshold`` rather than
dropped silently, so a near-miss is visible to whoever tunes the threshold."""

REASON_NOT_PERMITTED = "not_permitted"
REASON_BUDGET = "budget"
REASON_OVER_LIMIT = "over_limit"
REASON_BELOW_THRESHOLD = "below_threshold"
REASON_EXPIRED = "expired"
REASON_NOT_ACTIVE = "not_active"
REASON_DUPLICATE = "duplicate"


@dataclass(frozen=True)
class CapabilityChoice:
    """One item that made it into the slot, with the score that put it there."""

    kind: str
    ref_id: str
    content: str
    tokens: int
    score: float
    version: str | None = None
    layer: str | None = None
    source_task: str | None = None
    source_path: str | None = None
    evidence_refs: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "ref_id": self.ref_id,
            "version": self.version,
            "layer": self.layer,
            "score": round(self.score, 6),
            "tokens": self.tokens,
            "source_task": self.source_task,
            "source_path": self.source_path,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class CapabilitySkipped:
    """One candidate that did not make it, and the named reason."""

    kind: str
    ref_id: str
    reason: str
    detail: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "ref_id": self.ref_id,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class CapabilityPlan:
    """What the capability slot will hold, and everything it will not."""

    query: str = ""
    token_budget: int = DEFAULT_CAPABILITY_TOKENS
    granted_scopes: tuple[str, ...] = ()
    selected: tuple[CapabilityChoice, ...] = ()
    skipped: tuple[CapabilitySkipped, ...] = field(default=())

    @property
    def tokens_used(self) -> int:
        return sum(choice.tokens for choice in self.selected)

    @property
    def memory_ids(self) -> tuple[str, ...]:
        return tuple(choice.ref_id for choice in self.selected if choice.kind == "memory")

    @property
    def skill_labels(self) -> tuple[str, ...]:
        return tuple(
            f"{choice.ref_id}@{choice.version}"
            for choice in self.selected
            if choice.kind == "skill"
        )

    def items(self) -> list[ContextItem]:
        """Render the selection as capability-slot items.

        Relevance carries the selector's score through, so if the prompt budget is
        tighter than the capability budget the compiler sheds the same item this
        selector would have shed next.
        """

        return [
            ContextItem(
                slot=Slot.CAPABILITY,
                content=choice.content,
                role=Role.SYSTEM,
                key=f"{choice.kind}:{choice.ref_id}",
                relevance=choice.score,
            )
            for choice in self.selected
        ]

    def to_payload(self, *, iteration: int | None = None) -> dict[str, object]:
        return {
            "query": self.query,
            "iteration": iteration,
            "token_budget": self.token_budget,
            "tokens_used": self.tokens_used,
            "granted_scopes": list(self.granted_scopes),
            "selected": [choice.to_payload() for choice in self.selected],
            "skipped": [skip.to_payload() for skip in self.skipped],
        }

    def explain(self) -> dict[str, object]:
        """Human-readable summary for ``atlas capabilities`` and the trace."""

        return {
            "query": self.query,
            "selected": [
                {
                    "kind": choice.kind,
                    "ref": _reference(choice),
                    "score": round(choice.score, 4),
                    "tokens": choice.tokens,
                }
                for choice in self.selected
            ],
            "skipped": [
                {"kind": skip.kind, "ref": skip.ref_id, "reason": skip.reason}
                for skip in self.skipped
            ],
            "tokens_used": self.tokens_used,
            "token_budget": self.token_budget,
        }


class CapabilitySelector:
    """Run the plan's selection pipeline over one query."""

    def __init__(
        self,
        *,
        retriever: MemoryRetriever | None = None,
        skills: SkillRepository | None = None,
        counter: TokenCounter | None = None,
        granted_scopes: frozenset[str] = DEFAULT_SCOPES,
        token_budget: int = DEFAULT_CAPABILITY_TOKENS,
        max_memories: int = MAX_MEMORIES,
        max_skills: int = MAX_SKILLS,
        min_score: float = MIN_SCORE,
    ) -> None:
        self.retriever = retriever
        self.skills = skills
        self.counter: TokenCounter = counter or EstimatingCounter()
        self.granted_scopes = granted_scopes
        self.token_budget = token_budget
        self.max_memories = max_memories
        self.max_skills = max_skills
        self.min_score = min_score

    def select(self, query: str, *, now_ms: int | None = None) -> CapabilityPlan:
        """Choose memories and skills for one request.

        Skills are considered before memories when the budget is spent: a skill is
        an instruction about how to do the task, a memory is a fact about it, and a
        prompt that fits only one is better off with the instruction.
        """

        skipped: list[CapabilitySkipped] = []
        candidates = self._skill_candidates(query, skipped)
        candidates.extend(self._memory_candidates(query, skipped, now_ms=now_ms))
        selected = self._fit(candidates, skipped)
        return CapabilityPlan(
            query=query,
            token_budget=self.token_budget,
            granted_scopes=tuple(sorted(self.granted_scopes)),
            selected=tuple(selected),
            skipped=tuple(skipped),
        )

    # -------------------------------------------------------------- candidates

    def _skill_candidates(
        self, query: str, skipped: list[CapabilitySkipped]
    ) -> list[CapabilityChoice]:
        """Prefilter by declared trigger, then fall back to keyword retrieval.

        The trigger match is checked first and scored above any text match, because
        a trigger is the author's own statement of when the skill applies and should
        not depend on BM25 having indexed the right words.
        """

        if self.skills is None:
            return []
        scored: dict[tuple[str, str], tuple[SkillRecord, float]] = {}
        for record in self.skills.active():
            if record.matches(query):
                scored[record.key] = (record, 10.0)
        for record, text_score in self.skills.search(query, limit=self.max_skills * 4):
            if record.key not in scored:
                scored[record.key] = (record, text_score)

        eligible: list[CapabilityChoice] = []
        for record, score in sorted(
            scored.values(), key=lambda pair: (-pair[1], pair[0].skill_id, pair[0].version)
        ):
            if not record.status.is_injectable:
                skipped.append(
                    CapabilitySkipped(
                        kind="skill",
                        ref_id=record.label,
                        reason=REASON_NOT_ACTIVE,
                        detail=record.status.value,
                    )
                )
                continue
            missing = record.missing_scopes(self.granted_scopes)
            if missing:
                skipped.append(
                    CapabilitySkipped(
                        kind="skill",
                        ref_id=record.label,
                        reason=REASON_NOT_PERMITTED,
                        detail="missing scopes: " + ", ".join(missing),
                    )
                )
                continue
            if score < self.min_score:
                skipped.append(
                    CapabilitySkipped(
                        kind="skill",
                        ref_id=record.label,
                        reason=REASON_BELOW_THRESHOLD,
                        detail=f"score {score:.4f}",
                    )
                )
                continue
            if len(eligible) >= self.max_skills:
                skipped.append(
                    CapabilitySkipped(
                        kind="skill",
                        ref_id=record.label,
                        reason=REASON_OVER_LIMIT,
                        detail=f"max_skills={self.max_skills}",
                    )
                )
                continue
            eligible.append(self._skill_choice(record, score))
        return eligible

    def _memory_candidates(
        self,
        query: str,
        skipped: list[CapabilitySkipped],
        *,
        now_ms: int | None = None,
    ) -> list[CapabilityChoice]:
        if self.retriever is None:
            return []
        hits = self.retriever.search(
            query, limit=max(self.max_memories, DEFAULT_LIMIT), now_ms=now_ms
        )
        eligible: list[CapabilityChoice] = []
        for hit in hits:
            if hit.score < self.min_score:
                skipped.append(
                    CapabilitySkipped(
                        kind="memory",
                        ref_id=hit.record.memory_id,
                        reason=REASON_BELOW_THRESHOLD,
                        detail=f"score {hit.score:.4f}",
                    )
                )
                continue
            if len(eligible) >= self.max_memories:
                skipped.append(
                    CapabilitySkipped(
                        kind="memory",
                        ref_id=hit.record.memory_id,
                        reason=REASON_OVER_LIMIT,
                        detail=f"max_memories={self.max_memories}",
                    )
                )
                continue
            eligible.append(self._memory_choice(hit.record, hit.score))
        for record in self.retriever.expired(now_ms=now_ms):
            skipped.append(
                CapabilitySkipped(
                    kind="memory",
                    ref_id=record.memory_id,
                    reason=REASON_EXPIRED,
                    detail=record.layer.value,
                )
            )
        return eligible

    # ------------------------------------------------------------------ fitting

    def _fit(
        self, candidates: Sequence[CapabilityChoice], skipped: list[CapabilitySkipped]
    ) -> list[CapabilityChoice]:
        """Take candidates in order until the token budget is spent.

        Deliberately not a knapsack. Order here is relevance order, and skipping a
        highly relevant item to fit two marginal ones would make the slot's contents
        depend on unrelated items' sizes — the same query would then produce
        different context as the store grows.
        """

        selected: list[CapabilityChoice] = []
        seen: set[str] = set()
        used = 0
        for candidate in candidates:
            key = f"{candidate.kind}:{candidate.ref_id}"
            if key in seen:
                skipped.append(
                    CapabilitySkipped(
                        kind=candidate.kind, ref_id=candidate.ref_id, reason=REASON_DUPLICATE
                    )
                )
                continue
            if used + candidate.tokens > self.token_budget:
                skipped.append(
                    CapabilitySkipped(
                        kind=candidate.kind,
                        ref_id=candidate.ref_id,
                        reason=REASON_BUDGET,
                        detail=f"{candidate.tokens} tokens, {self.token_budget - used} left",
                    )
                )
                continue
            seen.add(key)
            used += candidate.tokens
            selected.append(candidate)
        return selected

    def _skill_choice(self, record: SkillRecord, score: float) -> CapabilityChoice:
        content = record.as_context_text()
        return CapabilityChoice(
            kind="skill",
            ref_id=record.skill_id,
            content=content,
            tokens=self._tokens(content),
            score=score,
            version=record.version,
            source_task=record.source_task,
            source_path=record.source_path,
            evidence_refs=record.evidence_refs,
        )

    def _memory_choice(self, record: MemoryRecord, score: float) -> CapabilityChoice:
        content = record.as_context_text()
        return CapabilityChoice(
            kind="memory",
            ref_id=record.memory_id,
            content=content,
            tokens=self._tokens(content),
            score=score,
            layer=record.layer.value,
            source_task=record.source_task,
            evidence_refs=record.evidence_refs,
        )

    def _tokens(self, content: str) -> int:
        return self.counter.count(
            TokenInput(messages=(ModelMessage(role=Role.SYSTEM, content=content),))
        )


def _reference(choice: CapabilityChoice) -> str:
    """How a choice is named in an explanation: skills carry their version, memories do not."""

    return choice.ref_id if choice.version is None else f"{choice.ref_id}@{choice.version}"


def capability_items(plan: CapabilityPlan) -> list[ContextItem]:
    """Free function mirroring :func:`~atlas_harness.context.compiler.fixed_items`."""

    return plan.items()
