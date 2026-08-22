"""Token counting and the threshold policy that decides when to compact.

Two things live here. :class:`TokenCounter` is the seam between "how many tokens
is this?" and whoever can answer: an estimator that needs nothing, or a provider
that counts exactly. :class:`ContextBudget` turns a count into a decision, using
the plan's three marks — prepare at 70%, compact at 85%, force at 95%.

The estimator is deliberately the default. A provider round-trip per iteration
costs latency and money to answer a question whose only consumer is a threshold
comparison, and a count that is 10% off moves the compaction one iteration, which
is not a correctness problem. Exact counting is available when a provider offers
it cheaply.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from atlas_harness.model.protocol import ModelAdapter, TokenInput
from atlas_harness.model.tokens import count_tokens_estimate

PREPARE_RATIO = 0.70
"""Mark a compaction as pending. Nothing is replaced yet."""

COMPACT_RATIO = 0.85
"""Compact automatically on the next iteration boundary."""

FORCE_RATIO = 0.95
"""Compact even if that means dropping evidence to references, or stop."""


class ContextPressure(StrEnum):
    """How close the transcript is to the model's context window."""

    OK = "ok"
    PENDING = "pending"
    COMPACT = "compact"
    FORCE = "force"

    @property
    def should_compact(self) -> bool:
        """True from the automatic mark upward. ``PENDING`` only announces."""

        return self in {ContextPressure.COMPACT, ContextPressure.FORCE}


class TokenCounter(Protocol):
    """Anything that can size a prompt. Sync so the compiler stays sync."""

    def count(self, value: TokenInput) -> int: ...


class EstimatingCounter:
    """Character-based estimate. Deterministic, offline, never used for billing."""

    def count(self, value: TokenInput) -> int:
        return count_tokens_estimate(value)


class AdapterCounter:
    """Exact counts from a provider, falling back to the estimate on failure.

    A counting endpoint that is down must not stop a run: an estimate is good
    enough for a threshold, so a provider fault degrades the precision of the
    decision rather than the availability of it.
    """

    def __init__(self, adapter: ModelAdapter, *, cache: dict[int, int] | None = None) -> None:
        self._adapter = adapter
        self._cache: dict[int, int] = {} if cache is None else cache

    def count(self, value: TokenInput) -> int:
        return count_tokens_estimate(value)

    async def count_async(self, value: TokenInput) -> int:
        key = hash(value.model_dump_json())
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        try:
            counted = await self._adapter.count_tokens(value)
        except Exception:
            return count_tokens_estimate(value)
        self._cache[key] = counted
        return counted


class ContextBudget(BaseModel):
    """The token ceiling for one run and the three marks along the way.

    ``limit_tokens`` is the *usable* window, not the model's raw context: the
    reserve for the reply is subtracted before the ratios are applied, because a
    prompt that exactly fills the window leaves nowhere for an answer.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    limit_tokens: int = Field(default=128_000, gt=0)
    prepare_ratio: float = Field(default=PREPARE_RATIO, gt=0, le=1)
    compact_ratio: float = Field(default=COMPACT_RATIO, gt=0, le=1)
    force_ratio: float = Field(default=FORCE_RATIO, gt=0, le=1)

    @classmethod
    def for_model(
        cls,
        *,
        max_context_tokens: int,
        reserve_output_tokens: int = 0,
        **overrides: float,
    ) -> ContextBudget:
        """Derive a budget from a model's window, holding back room for the reply."""

        usable = max(1, max_context_tokens - max(0, reserve_output_tokens))
        return cls(limit_tokens=usable, **overrides)

    def ratio(self, used_tokens: int) -> float:
        return used_tokens / self.limit_tokens

    def pressure(self, used_tokens: int) -> ContextPressure:
        """Classify a usage count against the three marks.

        Checked highest first: at 96% every mark is crossed, and the answer that
        matters is the most urgent one.
        """

        ratio = self.ratio(used_tokens)
        if ratio >= self.force_ratio:
            return ContextPressure.FORCE
        if ratio >= self.compact_ratio:
            return ContextPressure.COMPACT
        if ratio >= self.prepare_ratio:
            return ContextPressure.PENDING
        return ContextPressure.OK

    def tokens_at(self, ratio: float) -> int:
        return int(self.limit_tokens * ratio)

    def headroom(self, used_tokens: int) -> int:
        """Tokens left before the automatic mark. Zero once it is reached."""

        return max(0, self.tokens_at(self.compact_ratio) - used_tokens)
