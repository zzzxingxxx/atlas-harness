"""Slot ordering, deduplication, redaction and budget trimming.

The guarantee these tests exist to pin down is the one the plan states as
"不允许被工具结果覆盖": no amount of tool output can push the system prompt out of
the context. Everything else here is the machinery that makes that true.
"""

from __future__ import annotations

import pytest

from atlas_harness.context.compiler import (
    SLOT_ORDER,
    TRIM_ORDER,
    CompiledContext,
    ContextCompiler,
    ContextItem,
    Slot,
    fixed_items,
    short_term_items,
)
from atlas_harness.context.tokens import ContextBudget
from atlas_harness.kernel.errors import BudgetExceededError
from atlas_harness.model.protocol import ModelMessage, ModelToolCall, Role

SYSTEM_PROMPT = "You are AtlasHarness, a tool-using agent."


def compiler(limit: int = 4_000) -> ContextCompiler:
    return ContextCompiler(budget=ContextBudget(limit_tokens=limit))


def item(slot: Slot, content: str, **kwargs: object) -> ContextItem:
    return ContextItem(slot=slot, content=content, **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# slot order
# --------------------------------------------------------------------------- #


def test_the_five_slots_are_the_ones_the_plan_names() -> None:
    assert [slot.value for slot in SLOT_ORDER] == [
        "fixed",
        "task",
        "capability",
        "short_term",
        "evidence",
    ]


def test_trim_order_is_the_reverse_of_priority() -> None:
    """Least important first. Otherwise trimming would sacrifice the objective."""

    assert TRIM_ORDER == tuple(reversed(SLOT_ORDER))


def test_messages_come_out_in_slot_order_regardless_of_input_order() -> None:
    compiled = compiler().compile(
        [
            item(Slot.EVIDENCE, "src/a.py:12"),
            item(Slot.FIXED, SYSTEM_PROMPT, role=Role.SYSTEM),
            item(Slot.SHORT_TERM, "most recent turn"),
            item(Slot.TASK, "add pagination to /users"),
            item(Slot.CAPABILITY, "skill: pagination"),
        ]
    )

    assert [message.content for message in compiled.messages] == [
        SYSTEM_PROMPT,
        "add pagination to /users",
        "skill: pagination",
        "most recent turn",
        "src/a.py:12",
    ]


def test_relevance_does_not_reorder_a_prompt_that_fits() -> None:
    """Relevance decides what survives, not where it appears.

    Emitting in relevance order would hand a model its transcript newest-first.
    So when everything fits, the prompt keeps the order the items arrived in, and
    relevance has no visible effect at all -- it only speaks up under a budget
    too small to hold everything.
    """

    compiled = compiler().compile(
        [
            item(Slot.CAPABILITY, "low", relevance=0.1),
            item(Slot.CAPABILITY, "high", relevance=0.9),
            item(Slot.CAPABILITY, "mid", relevance=0.5),
        ]
    )

    assert [message.content for message in compiled.messages] == ["low", "high", "mid"]


def test_relevance_decides_which_item_survives_a_tight_budget() -> None:
    """The other half of the contract: under pressure, the low score goes."""

    compiled = compiler(limit=40).compile(
        [
            item(Slot.CAPABILITY, "DROP " * 20, relevance=0.1),
            item(Slot.CAPABILITY, "KEEP " * 20, relevance=0.9),
        ]
    )

    kept = " ".join(message.content for message in compiled.messages)
    assert "KEEP" in kept
    assert "DROP" not in kept


def test_equal_relevance_keeps_insertion_order() -> None:
    """Two compilations of the same input must produce the same prompt."""

    items = [item(Slot.TASK, f"step {index}") for index in range(5)]

    first = compiler().compile(items)
    second = compiler().compile(items)

    assert [m.content for m in first.messages] == [f"step {index}" for index in range(5)]
    assert [m.content for m in first.messages] == [m.content for m in second.messages]


# --------------------------------------------------------------------------- #
# deduplication
# --------------------------------------------------------------------------- #


def test_identical_content_is_deduplicated() -> None:
    compiled = compiler().compile(
        [
            item(Slot.EVIDENCE, "src/a.py"),
            item(Slot.EVIDENCE, "src/a.py"),
            item(Slot.EVIDENCE, "src/b.py"),
        ]
    )

    assert compiled.duplicates_removed == 1
    assert len(compiled.messages) == 2


def test_an_explicit_key_defines_identity() -> None:
    """Same key, different text: still one item, so a restated fact costs once."""

    compiled = compiler().compile(
        [
            item(Slot.TASK, "objective v1", key="objective"),
            item(Slot.TASK, "objective v2", key="objective"),
        ]
    )

    assert compiled.duplicates_removed == 1
    assert len(compiled.messages) == 1


def test_dedup_keeps_the_most_relevant_copy() -> None:
    """Sorting runs before dedup, so the survivor is chosen rather than arbitrary."""

    compiled = compiler().compile(
        [
            item(Slot.CAPABILITY, "weak", key="skill", relevance=0.1),
            item(Slot.CAPABILITY, "strong", key="skill", relevance=0.9),
        ]
    )

    assert [message.content for message in compiled.messages] == ["strong"]


def test_the_same_content_in_two_slots_is_not_a_duplicate() -> None:
    """A path can be both the task and the evidence for it."""

    compiled = compiler().compile(
        [
            item(Slot.TASK, "src/a.py"),
            item(Slot.EVIDENCE, "src/a.py"),
        ]
    )

    assert compiled.duplicates_removed == 0
    assert len(compiled.messages) == 2


# --------------------------------------------------------------------------- #
# redaction
# --------------------------------------------------------------------------- #


def test_secrets_are_redacted_on_the_way_into_the_prompt() -> None:
    """The log being clean is not enough: a secret in the prompt is disclosed."""

    compiled = compiler().compile([item(Slot.EVIDENCE, "config: api_key=super-secret-value-1234")])

    body = compiled.messages[0].content
    assert "super-secret-value-1234" not in body
    assert "[redacted]" in body


def test_redaction_reaches_a_carried_message() -> None:
    """An item that wraps a real transcript message is redacted too."""

    message = ModelMessage.user("token=ghp_abcdefghijklmnopqrstuvwxyz0123")
    compiled = compiler().compile(
        [ContextItem(slot=Slot.SHORT_TERM, content=message.content, message=message)]
    )

    assert "ghp_abcdefghijklmnopqrstuvwxyz0123" not in compiled.messages[0].content


def test_every_slot_is_redacted_not_just_tool_output() -> None:
    compiled = compiler().compile(
        [
            item(Slot.FIXED, "rule: password=hunter2hunter2", role=Role.SYSTEM),
            item(Slot.TASK, "secret=abcd1234abcd"),
        ]
    )

    joined = " ".join(message.content for message in compiled.messages)
    assert "hunter2hunter2" not in joined
    assert "abcd1234abcd" not in joined


# --------------------------------------------------------------------------- #
# trimming
# --------------------------------------------------------------------------- #


def test_nothing_is_dropped_when_everything_fits() -> None:
    compiled = compiler().compile([item(Slot.TASK, "small")])

    assert compiled.dropped == ()
    assert compiled.used_tokens <= compiled.limit_tokens


def test_evidence_is_sacrificed_before_the_task() -> None:
    """Trim order in practice: the objective outlives the evidence for it."""

    compiled = compiler(limit=120).compile(
        [
            item(Slot.FIXED, SYSTEM_PROMPT, role=Role.SYSTEM),
            item(Slot.TASK, "OBJECTIVE " * 10),
            item(Slot.EVIDENCE, "EVIDENCE " * 40),
        ]
    )

    kept = " ".join(message.content for message in compiled.messages)
    assert "OBJECTIVE" in kept
    assert "EVIDENCE" not in kept
    assert [dropped.slot for dropped in compiled.dropped] == [Slot.EVIDENCE]


def test_a_huge_tool_result_cannot_displace_the_system_prompt() -> None:
    """The plan's rule, stated directly: 固定槽位不允许被工具结果覆盖.

    The short-term slot holds a result far larger than the whole budget. It is
    dropped in its entirety and the fixed slot survives untouched, which is the
    opposite of what a naive newest-wins context would do.
    """

    compiled = compiler(limit=200).compile(
        [
            item(Slot.FIXED, SYSTEM_PROMPT, role=Role.SYSTEM),
            item(Slot.SHORT_TERM, "TOOL OUTPUT " * 500),
        ]
    )

    assert [message.content for message in compiled.messages] == [SYSTEM_PROMPT]
    assert compiled.dropped[0].slot is Slot.SHORT_TERM


def test_short_term_drops_the_oldest_first() -> None:
    """Recency is the relevance ordering for this slot."""

    messages = [ModelMessage.user(f"turn {index} " + "x" * 200) for index in range(6)]
    # Sized so only the two newest turns fit alongside the system prompt.
    compiled = compiler(limit=130).compile(
        [
            item(Slot.FIXED, SYSTEM_PROMPT, role=Role.SYSTEM),
            *short_term_items(messages),
        ]
    )

    survived = [
        message.content for message in compiled.messages if message.content.startswith("turn")
    ]
    assert survived, "at least one recent turn should survive"
    assert "turn 5" in survived[-1]
    assert not any(content.startswith("turn 0") for content in survived)


def test_lower_relevance_goes_first_within_a_trimmed_slot() -> None:
    compiled = compiler(limit=40).compile(
        [
            item(Slot.CAPABILITY, "KEEP " * 20, relevance=0.9),
            item(Slot.CAPABILITY, "DROP " * 20, relevance=0.1),
        ]
    )

    kept = " ".join(message.content for message in compiled.messages)
    assert "KEEP" in kept
    assert "DROP" not in kept


def test_a_pinned_item_outside_the_fixed_slot_is_never_trimmed() -> None:
    compiled = compiler(limit=40).compile(
        [
            item(Slot.EVIDENCE, "PINNED " * 10, pinned=True),
            item(Slot.EVIDENCE, "LOOSE " * 30),
        ]
    )

    kept = " ".join(message.content for message in compiled.messages)
    assert "PINNED" in kept
    assert "LOOSE" not in kept


def test_a_budget_too_small_for_the_fixed_slot_raises() -> None:
    """Better to refuse than to ship a prompt with no instructions in it."""

    with pytest.raises(BudgetExceededError) as excinfo:
        compiler(limit=5).compile([item(Slot.FIXED, SYSTEM_PROMPT * 20, role=Role.SYSTEM)])

    assert excinfo.value.details["limit_tokens"] == 5
    assert "hint" in excinfo.value.details


# --------------------------------------------------------------------------- #
# accounting
# --------------------------------------------------------------------------- #


def test_tool_declarations_count_against_the_budget() -> None:
    """Leaving them out would make the budget optimistic exactly where it hurts."""

    declaration = {"name": "read_file", "description": "d" * 400, "input_schema": {}}
    without = ContextCompiler(budget=ContextBudget(limit_tokens=4_000))
    with_tools = ContextCompiler(budget=ContextBudget(limit_tokens=4_000), tools=[declaration])

    plain = without.compile([item(Slot.TASK, "hello")])
    counted = with_tools.compile([item(Slot.TASK, "hello")])

    assert counted.used_tokens > plain.used_tokens


def test_slot_tokens_only_report_slots_that_survived() -> None:
    compiled = compiler().compile(
        [
            item(Slot.FIXED, SYSTEM_PROMPT, role=Role.SYSTEM),
            item(Slot.TASK, "objective"),
        ]
    )

    assert set(compiled.slot_tokens) == {"fixed", "task"}
    assert all(count > 0 for count in compiled.slot_tokens.values())


def test_summary_is_json_safe_and_carries_the_ratio() -> None:
    compiled = compiler(limit=1_000).compile([item(Slot.TASK, "objective")])

    summary = compiled.summary()
    assert summary["limit_tokens"] == 1_000
    assert 0 < float(summary["ratio"]) < 1
    assert summary["dropped"] == 0


def test_an_empty_compilation_is_not_an_error() -> None:
    compiled = compiler().compile([])

    assert compiled == CompiledContext(messages=(), used_tokens=0, limit_tokens=4_000)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def test_fixed_items_pins_everything_it_builds() -> None:
    items = fixed_items(SYSTEM_PROMPT, project_rules=["rule a"], safety_rules=["never rm -rf"])

    assert len(items) == 3
    assert all(built.is_pinned for built in items)
    assert all(built.role is Role.SYSTEM for built in items)
    assert [built.key for built in items] == ["system_prompt", "project_rule:0", "safety_rule:0"]


def test_short_term_items_carry_the_original_message() -> None:
    """Tool-call bindings must survive, or the transcript stops being valid."""

    call = ModelToolCall(call_id="c1", name="read_file", arguments={"path": "a.txt"})
    assistant = ModelMessage.assistant(tool_calls=(call,))

    items = short_term_items([assistant])

    assert items[0].message is assistant
    assert items[0].message.tool_calls[0].call_id == "c1"


def test_short_term_relevance_increases_with_recency() -> None:
    items = short_term_items([ModelMessage.user(f"turn {index}") for index in range(4)])

    relevances = [built.relevance for built in items]
    assert relevances == sorted(relevances)


def test_a_carried_tool_message_survives_compilation_intact() -> None:
    tool_message = ModelMessage.tool(tool_call_id="c1", content="{}", name="read_file")

    compiled = compiler().compile(short_term_items([tool_message]))

    assert compiled.messages[0].role is Role.TOOL
    assert compiled.messages[0].tool_call_id == "c1"
