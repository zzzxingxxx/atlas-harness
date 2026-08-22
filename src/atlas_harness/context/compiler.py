"""Slot ordering, deduplication, redaction and budget trimming.

The context compiler is not a concatenator. It sorts candidate content into the
plan's five fixed slots, drops duplicates, redacts secrets, and then trims to a
token budget by discarding the *least* important content rather than the newest
or the oldest.

The slot order is the priority order, and that is the whole design:

===========  ====================================  ==================================
slot         content                               trimming rule
===========  ====================================  ==================================
``fixed``    system prompt, project rules, policy  never trimmed, never overwritten
``task``     objective, constraints, blockers      preserved ahead of everything else
``capability`` memory, skills, tool notes          filtered by relevance and permission
``short_term`` recent messages and tool results    newest kept, oldest dropped first
``evidence`` paths, diffs, logs, external refs     references only, bodies live in artifacts
===========  ====================================  ==================================

A tool result cannot displace the system prompt no matter how large it is, which
is what "不允许被工具结果覆盖" means in practice: the fixed slot is trimmed last
and, if the budget cannot even hold it, the compiler raises rather than silently
shipping a prompt with no instructions in it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from atlas_harness.context.tokens import ContextBudget, EstimatingCounter, TokenCounter
from atlas_harness.kernel.errors import BudgetExceededError
from atlas_harness.model.protocol import ModelMessage, Role, TokenInput
from atlas_harness.tools.redaction import redact


class Slot(StrEnum):
    """The five fixed slots, declared in priority order."""

    FIXED = "fixed"
    TASK = "task"
    CAPABILITY = "capability"
    SHORT_TERM = "short_term"
    EVIDENCE = "evidence"


Indexed = tuple[int, "ContextItem"]
"""An item paired with its arrival position, so the two orderings the compiler
cares about — survival priority and reading order — stay separable."""


SLOT_ORDER: tuple[Slot, ...] = (
    Slot.FIXED,
    Slot.TASK,
    Slot.CAPABILITY,
    Slot.SHORT_TERM,
    Slot.EVIDENCE,
)
"""Priority order. Also the order the compiled messages appear in the prompt."""

TRIM_ORDER: tuple[Slot, ...] = (
    Slot.EVIDENCE,
    Slot.SHORT_TERM,
    Slot.CAPABILITY,
    Slot.TASK,
    Slot.FIXED,
)
"""Reverse priority: what gets dropped first when the budget is tight."""


class ContextItem(BaseModel):
    """One candidate piece of context, before any trimming decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slot: Slot
    content: str
    role: Role = Role.USER
    key: str | None = None
    """Dedup identity. Two items with the same key are the same content."""

    relevance: float = 0.0
    """Higher survives longer inside its own slot. Ties keep insertion order."""

    pinned: bool = False
    """Never trimmed. Everything in ``fixed`` behaves this way implicitly."""

    message: ModelMessage | None = None
    """Set when the item *is* an existing transcript message, so the compiled
    prompt can reuse it verbatim instead of flattening it to text and losing
    tool-call bindings."""

    @property
    def dedup_key(self) -> tuple[str, str]:
        return (self.slot.value, self.key if self.key is not None else self.content)

    @property
    def is_pinned(self) -> bool:
        return self.pinned or self.slot is Slot.FIXED


class DroppedItem(BaseModel):
    """One item the budget did not fit, and why."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slot: Slot
    key: str
    tokens: int
    reason: str


class CompiledContext(BaseModel):
    """The prompt the compiler produced, plus what it had to leave out."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    messages: tuple[ModelMessage, ...] = ()
    used_tokens: int = 0
    limit_tokens: int = 0
    dropped: tuple[DroppedItem, ...] = ()
    duplicates_removed: int = 0
    slot_tokens: dict[str, int] = Field(default_factory=dict)

    @property
    def ratio(self) -> float:
        return 0.0 if self.limit_tokens == 0 else self.used_tokens / self.limit_tokens

    @property
    def dropped_keys(self) -> tuple[str, ...]:
        return tuple(item.key for item in self.dropped)

    def summary(self) -> dict[str, object]:
        return {
            "messages": len(self.messages),
            "used_tokens": self.used_tokens,
            "limit_tokens": self.limit_tokens,
            "ratio": round(self.ratio, 4),
            "dropped": len(self.dropped),
            "duplicates_removed": self.duplicates_removed,
            "slot_tokens": dict(self.slot_tokens),
        }


class ContextCompiler:
    """Sort, dedup, redact and trim context items into a prompt.

    Stateless between calls: :meth:`compile` takes everything it needs, so the
    same compiler can serve concurrent lanes without sharing anything mutable.
    """

    def __init__(
        self,
        *,
        budget: ContextBudget | None = None,
        counter: TokenCounter | None = None,
        tools: Sequence[dict[str, object]] = (),
    ) -> None:
        self.budget = budget or ContextBudget()
        self.counter: TokenCounter = counter or EstimatingCounter()
        self._tools = tuple(tools)

    def compile(self, items: Iterable[ContextItem]) -> CompiledContext:
        """Sort, dedup, redact, trim, then emit in reading order.

        Two different orderings are at work and conflating them is a bug worth
        naming: *relevance* decides which items survive a tight budget, while
        *insertion order* decides where the survivors appear in the prompt. A
        transcript emitted in relevance order would hand the model its turns
        newest-first, so the final sequence is rebuilt from the original positions
        once trimming has made its choices.
        """

        indexed = list(enumerate(items))
        ranked, duplicates = self._dedup(self._rank(indexed))
        redacted = [(index, self._redact(item)) for index, item in ranked]
        kept, dropped = self._trim(redacted)
        messages = tuple(self._to_message(item) for item in self._reading_order(kept))
        slot_tokens = {
            slot.value: sum(self._tokens(item) for _, item in kept if item.slot is slot)
            for slot in SLOT_ORDER
        }
        return CompiledContext(
            messages=messages,
            used_tokens=self._prompt_tokens(messages),
            limit_tokens=self.budget.limit_tokens,
            dropped=tuple(dropped),
            duplicates_removed=duplicates,
            slot_tokens={slot: count for slot, count in slot_tokens.items() if count},
        )

    # ------------------------------------------------------------------ stages

    def _rank(self, indexed: Sequence[Indexed]) -> list[Indexed]:
        """Order by survival priority: slot, then relevance, then arrival.

        The original index is the third key, so equal-relevance items never
        reorder between two compilations of the same input.
        """

        return sorted(
            indexed,
            key=lambda pair: (SLOT_ORDER.index(pair[1].slot), -pair[1].relevance, pair[0]),
        )

    def _reading_order(self, indexed: Sequence[Indexed]) -> list[ContextItem]:
        """Order for the prompt: slot priority, then the order items arrived."""

        ordered = sorted(indexed, key=lambda pair: (SLOT_ORDER.index(pair[1].slot), pair[0]))
        return [item for _, item in ordered]

    def _dedup(self, items: Sequence[Indexed]) -> tuple[list[Indexed], int]:
        """Keep the first occurrence of each key. Ranking ran first, so the
        survivor is the most relevant copy rather than an arbitrary one."""

        seen: set[tuple[str, str]] = set()
        kept: list[Indexed] = []
        for index, item in items:
            key = item.dedup_key
            if key in seen:
                continue
            seen.add(key)
            kept.append((index, item))
        return kept, len(items) - len(kept)

    def _redact(self, item: ContextItem) -> ContextItem:
        """Redact on the way into the prompt, not only on the way into the log.

        A secret that reaches the model is disclosed even if the event log is
        clean, so this runs on every slot regardless of where the content came
        from.
        """

        content = redact(item.content)
        message = item.message
        if message is not None and message.content:
            message = message.model_copy(update={"content": redact(message.content)})
        if content == item.content and message is item.message:
            return item
        return item.model_copy(update={"content": content, "message": message})

    def _trim(self, items: Sequence[Indexed]) -> tuple[list[Indexed], list[DroppedItem]]:
        """Drop least-important items until the prompt fits.

        Slots are sacrificed in reverse priority order, and within a slot the
        lowest relevance goes first. ``short_term`` needs no special case: recency
        is encoded as relevance, so its oldest turn is already its lowest-scoring
        one.
        """

        kept = list(items)
        dropped: list[DroppedItem] = []
        if self._fits(kept):
            return kept, dropped

        for slot in TRIM_ORDER:
            for candidate in self._trim_candidates(kept, slot):
                if self._fits(kept):
                    return kept, dropped
                kept.remove(candidate)
                item = candidate[1]
                dropped.append(
                    DroppedItem(
                        slot=item.slot,
                        key=item.key or item.content[:60],
                        tokens=self._tokens(item),
                        reason=f"{slot.value} slot trimmed to fit the token budget",
                    )
                )
        if self._fits(kept):
            return kept, dropped
        raise BudgetExceededError(
            "context budget cannot hold the pinned slots",
            details={
                "limit_tokens": self.budget.limit_tokens,
                "pinned_tokens": self._prompt_tokens(
                    tuple(self._to_message(item) for _, item in kept)
                ),
                "hint": "raise the context budget or shorten the system prompt",
            },
        )

    def _trim_candidates(self, kept: Sequence[Indexed], slot: Slot) -> list[Indexed]:
        """Items in one slot, in the order they should be sacrificed.

        Ascending relevance, ties broken by arrival order, so two compilations of
        the same input always shed the same item first.
        """

        candidates = [pair for pair in kept if pair[1].slot is slot and not pair[1].is_pinned]
        candidates.sort(key=lambda pair: (pair[1].relevance, pair[0]))
        return candidates

    # ------------------------------------------------------------------ tokens

    def _fits(self, items: Sequence[Indexed]) -> bool:
        messages = tuple(self._to_message(item) for _, item in items)
        return self._prompt_tokens(messages) <= self.budget.limit_tokens

    def _prompt_tokens(self, messages: tuple[ModelMessage, ...]) -> int:
        """Count the whole prompt, tool declarations included.

        The declarations are part of what the provider charges against the
        window, so leaving them out would make the budget optimistic by exactly
        the amount most likely to overflow it.
        """

        return self.counter.count(TokenInput(messages=messages, tools=self._tools))

    def _tokens(self, item: ContextItem) -> int:
        return self.counter.count(TokenInput(messages=(self._to_message(item),)))

    def _to_message(self, item: ContextItem) -> ModelMessage:
        if item.message is not None:
            return item.message
        return ModelMessage(role=item.role, content=item.content)


def fixed_items(
    system_prompt: str,
    *,
    project_rules: Sequence[str] = (),
    safety_rules: Sequence[str] = (),
) -> list[ContextItem]:
    """Build the fixed slot. Pinned, so no tool result can push it out."""

    items = [
        ContextItem(
            slot=Slot.FIXED,
            content=system_prompt,
            role=Role.SYSTEM,
            key="system_prompt",
            pinned=True,
        )
    ]
    for index, rule in enumerate(project_rules):
        items.append(
            ContextItem(
                slot=Slot.FIXED,
                content=rule,
                role=Role.SYSTEM,
                key=f"project_rule:{index}",
                pinned=True,
            )
        )
    for index, rule in enumerate(safety_rules):
        items.append(
            ContextItem(
                slot=Slot.FIXED,
                content=rule,
                role=Role.SYSTEM,
                key=f"safety_rule:{index}",
                pinned=True,
            )
        )
    return items


def short_term_items(
    messages: Sequence[ModelMessage], *, base_relevance: float = 0.0
) -> list[ContextItem]:
    """Wrap transcript messages, newest most relevant.

    The original :class:`ModelMessage` is carried through rather than flattened,
    so assistant tool calls keep their ids and the transcript stays valid for
    providers that require every call to have a matching result.
    """

    total = len(messages)
    items: list[ContextItem] = []
    for index, message in enumerate(messages):
        items.append(
            ContextItem(
                slot=Slot.SHORT_TERM,
                content=message.content,
                role=message.role,
                key=f"message:{index}",
                relevance=base_relevance + index / max(1, total),
                message=message,
            )
        )
    return items
