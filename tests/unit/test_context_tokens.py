"""Token counting and the three threshold marks.

The marks are the plan's contract (70/85/95), so these tests pin the exact
boundaries rather than approximate behaviour: an off-by-one in ``pressure``
either compacts a conversation that fits or lets one overflow.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from atlas_harness.context.tokens import (
    COMPACT_RATIO,
    FORCE_RATIO,
    PREPARE_RATIO,
    AdapterCounter,
    ContextBudget,
    ContextPressure,
    EstimatingCounter,
)
from atlas_harness.model.protocol import ModelMessage, TokenInput

# --------------------------------------------------------------------------- #
# the estimator
# --------------------------------------------------------------------------- #


def test_the_estimator_is_deterministic() -> None:
    """The same input must always produce the same count.

    A threshold that moved between two identical prompts would make compaction
    unreproducible, which is worse than being imprecise.
    """

    counter = EstimatingCounter()
    value = TokenInput(messages=(ModelMessage.user("hello world"),))

    assert counter.count(value) == counter.count(value)


def test_the_estimator_grows_with_content() -> None:
    counter = EstimatingCounter()

    short = counter.count(TokenInput(messages=(ModelMessage.user("hi"),)))
    long = counter.count(TokenInput(messages=(ModelMessage.user("hi" * 500),)))

    assert long > short


def test_the_estimator_counts_tool_declarations() -> None:
    """Declarations occupy the window too, so leaving them out would make the
    budget optimistic by exactly the amount most likely to overflow it."""

    counter = EstimatingCounter()
    messages = (ModelMessage.user("hi"),)
    tools = ({"type": "function", "function": {"name": "read_file", "description": "x" * 200}},)

    assert counter.count(TokenInput(messages=messages, tools=tools)) > counter.count(
        TokenInput(messages=messages)
    )


def test_the_estimator_handles_an_empty_input() -> None:
    assert EstimatingCounter().count(TokenInput()) == 0


# --------------------------------------------------------------------------- #
# the three marks
# --------------------------------------------------------------------------- #


def test_the_default_marks_match_the_plan() -> None:
    assert (PREPARE_RATIO, COMPACT_RATIO, FORCE_RATIO) == (0.70, 0.85, 0.95)


@pytest.mark.parametrize(
    ("used", "expected"),
    [
        (0, ContextPressure.OK),
        (699, ContextPressure.OK),
        (700, ContextPressure.PENDING),
        (849, ContextPressure.PENDING),
        (850, ContextPressure.COMPACT),
        (949, ContextPressure.COMPACT),
        (950, ContextPressure.FORCE),
        (10_000, ContextPressure.FORCE),
    ],
)
def test_pressure_boundaries_are_inclusive(used: int, expected: ContextPressure) -> None:
    """Each mark takes effect *at* its ratio, not one token past it."""

    assert ContextBudget(limit_tokens=1_000).pressure(used) is expected


def test_pressure_reports_the_most_urgent_mark() -> None:
    """At 96% every mark is crossed; the answer that matters is the highest."""

    assert ContextBudget(limit_tokens=1_000).pressure(960) is ContextPressure.FORCE


def test_only_the_upper_two_marks_compact() -> None:
    """Crossing 70% is information. Discarding context needs a stronger signal."""

    assert ContextPressure.OK.should_compact is False
    assert ContextPressure.PENDING.should_compact is False
    assert ContextPressure.COMPACT.should_compact is True
    assert ContextPressure.FORCE.should_compact is True


def test_ratio_and_tokens_at_are_inverses() -> None:
    budget = ContextBudget(limit_tokens=1_000)

    assert budget.ratio(850) == pytest.approx(0.85)
    assert budget.tokens_at(0.85) == 850


def test_headroom_reaches_zero_at_the_automatic_mark() -> None:
    budget = ContextBudget(limit_tokens=1_000)

    assert budget.headroom(0) == 850
    assert budget.headroom(850) == 0
    assert budget.headroom(999) == 0


# --------------------------------------------------------------------------- #
# deriving a budget from a model
# --------------------------------------------------------------------------- #


def test_for_model_reserves_room_for_the_reply() -> None:
    """A prompt that exactly fills the window leaves nowhere for an answer."""

    budget = ContextBudget.for_model(max_context_tokens=10_000, reserve_output_tokens=2_000)

    assert budget.limit_tokens == 8_000


def test_for_model_never_produces_a_zero_limit() -> None:
    """A reserve larger than the window is a misconfiguration, but a limit of
    zero would make every ratio a division by zero, so it clamps instead."""

    budget = ContextBudget.for_model(max_context_tokens=100, reserve_output_tokens=500)

    assert budget.limit_tokens == 1


def test_for_model_carries_overridden_ratios() -> None:
    budget = ContextBudget.for_model(max_context_tokens=1_000, compact_ratio=0.5)

    assert budget.compact_ratio == 0.5
    assert budget.pressure(500) is ContextPressure.COMPACT


def test_a_budget_is_frozen() -> None:
    budget = ContextBudget(limit_tokens=1_000)

    with pytest.raises(ValidationError):
        budget.limit_tokens = 2_000  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# the adapter counter
# --------------------------------------------------------------------------- #


class _CountingAdapter:
    """Minimal stand-in: only ``count_tokens`` is exercised here."""

    def __init__(self, *, answer: int | None = 42, fail: bool = False) -> None:
        self.answer = answer
        self.fail = fail
        self.calls = 0

    async def count_tokens(self, value: TokenInput) -> int:
        self.calls += 1
        if self.fail:
            raise RuntimeError("counting endpoint is down")
        assert self.answer is not None
        return self.answer


async def test_the_adapter_counter_uses_the_provider() -> None:
    adapter = _CountingAdapter(answer=99)
    counter = AdapterCounter(adapter)  # type: ignore[arg-type]

    assert await counter.count_async(TokenInput(messages=(ModelMessage.user("hi"),))) == 99


async def test_the_adapter_counter_caches_by_input() -> None:
    adapter = _CountingAdapter(answer=99)
    counter = AdapterCounter(adapter)  # type: ignore[arg-type]
    value = TokenInput(messages=(ModelMessage.user("hi"),))

    await counter.count_async(value)
    await counter.count_async(value)

    assert adapter.calls == 1


async def test_a_failing_counting_endpoint_falls_back_to_the_estimate() -> None:
    """A provider fault must degrade precision, not availability: the only
    consumer of this number is a threshold comparison."""

    adapter = _CountingAdapter(fail=True)
    counter = AdapterCounter(adapter)  # type: ignore[arg-type]
    value = TokenInput(messages=(ModelMessage.user("hello world"),))

    counted = await counter.count_async(value)

    assert counted == EstimatingCounter().count(value)


def test_the_adapter_counter_is_synchronously_an_estimator() -> None:
    """The sync path cannot await, so it estimates. This keeps the compiler sync."""

    adapter = _CountingAdapter(answer=99)
    counter = AdapterCounter(adapter)  # type: ignore[arg-type]
    value = TokenInput(messages=(ModelMessage.user("hi"),))

    assert counter.count(value) == EstimatingCounter().count(value)
    assert adapter.calls == 0
