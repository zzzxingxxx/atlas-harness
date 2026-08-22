"""Stream assembly: fragment reordering, malformed arguments, broken streams."""

from __future__ import annotations

import pytest

from atlas_harness.model.assembler import (
    MAX_ARGUMENT_BYTES,
    StreamAssembler,
)
from atlas_harness.model.protocol import (
    MessageCompleted,
    ModelEvent,
    ProviderErrorEvent,
    Role,
    StopReason,
    TextDelta,
    ThinkingDelta,
    TokenUsage,
    ToolCallCompleted,
    ToolCallDelta,
    ToolCallStarted,
)


def _assemble(events: list[ModelEvent]) -> object:
    assembler = StreamAssembler()
    assembler.feed_all(events)
    return assembler.finish()


def test_text_deltas_join_in_order() -> None:
    result = _assemble(
        [
            TextDelta(text="Hello"),
            TextDelta(text=", "),
            TextDelta(text="world"),
            MessageCompleted(usage=TokenUsage(input_tokens=10, output_tokens=3)),
        ]
    )

    assert result.text == "Hello, world"
    assert result.completed is True
    assert result.failed is False
    assert result.stop_reason is StopReason.END_TURN
    assert result.usage.total_tokens == 13


def test_thinking_is_kept_separate_from_text() -> None:
    result = _assemble(
        [
            ThinkingDelta(text="let me check"),
            TextDelta(text="answer"),
            MessageCompleted(),
        ]
    )

    assert result.thinking == "let me check"
    assert result.text == "answer"


def test_tool_arguments_reassemble_across_chunks() -> None:
    result = _assemble(
        [
            ToolCallStarted(index=0, call_id="call_1", name="read_file"),
            ToolCallDelta(index=0, arguments_delta='{"pa'),
            ToolCallDelta(index=0, arguments_delta='th": "a'),
            ToolCallDelta(index=0, arguments_delta='.txt"}'),
            ToolCallCompleted(index=0),
            MessageCompleted(stop_reason=StopReason.TOOL_USE),
        ]
    )

    assert len(result.tool_calls) == 1
    call = result.tool_calls[0]
    assert call.call_id == "call_1"
    assert call.name == "read_file"
    assert call.arguments == {"path": "a.txt"}
    assert call.valid is True


def test_call_id_and_name_may_arrive_on_a_later_delta() -> None:
    """Some gateways omit them on the opening frame and send them mid-stream."""

    result = _assemble(
        [
            ToolCallDelta(index=0, arguments_delta="{}"),
            ToolCallDelta(index=0, call_id="call_late", name="list_dir"),
            MessageCompleted(),
        ]
    )

    call = result.tool_calls[0]
    assert call.call_id == "call_late"
    assert call.name == "list_dir"
    assert call.valid is True


def test_multiple_calls_keep_first_seen_order() -> None:
    result = _assemble(
        [
            ToolCallStarted(index=1, call_id="call_b", name="b"),
            ToolCallStarted(index=0, call_id="call_a", name="a"),
            ToolCallDelta(index=0, arguments_delta="{}"),
            ToolCallDelta(index=1, arguments_delta="{}"),
            MessageCompleted(),
        ]
    )

    assert [call.name for call in result.tool_calls] == ["b", "a"]


def test_malformed_json_becomes_an_invalid_call_not_an_exception() -> None:
    result = _assemble(
        [
            ToolCallStarted(index=0, call_id="call_1", name="read_file"),
            ToolCallDelta(index=0, arguments_delta='{"path": '),
            MessageCompleted(),
        ]
    )

    call = result.tool_calls[0]
    assert call.valid is False
    assert call.error is not None
    assert "not valid JSON" in call.error
    assert call.raw_arguments == '{"path": '
    assert result.invalid_tool_calls == (call,)
    assert result.valid_tool_calls == ()


def test_non_object_arguments_are_rejected() -> None:
    result = _assemble(
        [
            ToolCallStarted(index=0, call_id="call_1", name="read_file"),
            ToolCallDelta(index=0, arguments_delta="[1, 2]"),
            MessageCompleted(),
        ]
    )

    call = result.tool_calls[0]
    assert call.valid is False
    assert call.error == "arguments must be a JSON object, got list"


def test_empty_arguments_are_valid_for_zero_argument_tools() -> None:
    result = _assemble(
        [
            ToolCallStarted(index=0, call_id="call_1", name="whoami"),
            ToolCallCompleted(index=0),
            MessageCompleted(),
        ]
    )

    call = result.tool_calls[0]
    assert call.valid is True
    assert call.arguments == {}


def test_nameless_tool_call_is_invalid() -> None:
    result = _assemble(
        [
            ToolCallDelta(index=0, arguments_delta="{}"),
            MessageCompleted(),
        ]
    )

    call = result.tool_calls[0]
    assert call.valid is False
    assert call.error == "tool call has no name"
    assert call.call_id.startswith("call_")


def test_oversized_arguments_are_truncated_and_marked_invalid() -> None:
    oversized = "x" * (MAX_ARGUMENT_BYTES + 1_000)
    result = _assemble(
        [
            ToolCallStarted(index=0, call_id="call_1", name="write_file"),
            ToolCallDelta(index=0, arguments_delta=oversized),
            MessageCompleted(),
        ]
    )

    call = result.tool_calls[0]
    assert call.valid is False
    assert call.error is not None
    assert "exceeded" in call.error
    assert len(call.raw_arguments) == MAX_ARGUMENT_BYTES


def test_tool_calls_force_tool_use_even_when_provider_says_end_turn() -> None:
    result = _assemble(
        [
            ToolCallStarted(index=0, call_id="call_1", name="read_file"),
            ToolCallDelta(index=0, arguments_delta="{}"),
            MessageCompleted(stop_reason=StopReason.END_TURN),
        ]
    )

    assert result.stop_reason is StopReason.TOOL_USE


def test_stream_without_completion_marker_is_a_failure() -> None:
    result = _assemble([TextDelta(text="half an answer")])

    assert result.completed is False
    assert result.failed is True
    assert result.text == "half an answer"


def test_provider_error_is_recorded_rather_than_raised() -> None:
    assembler = StreamAssembler()
    assembler.feed(TextDelta(text="partial"))
    assembler.feed(ProviderErrorEvent(message="429 slow down", retryable=True, status_code=429))

    assert assembler.error is not None
    result = assembler.finish()

    assert result.error is not None
    assert result.error.status_code == 429
    assert result.stop_reason is StopReason.ERROR
    assert result.failed is True
    assert result.text == "partial"


def test_to_message_renders_the_assistant_turn() -> None:
    result = _assemble(
        [
            TextDelta(text="using a tool"),
            ToolCallStarted(index=0, call_id="call_1", name="read_file"),
            ToolCallDelta(index=0, arguments_delta='{"path": "a.txt"}'),
            MessageCompleted(stop_reason=StopReason.TOOL_USE),
        ]
    )

    message = result.to_message()

    assert message.role is Role.ASSISTANT
    assert message.content == "using a tool"
    assert message.tool_calls[0].name == "read_file"


def test_summary_reports_counts_without_copying_text() -> None:
    result = _assemble(
        [
            TextDelta(text="0123456789"),
            ThinkingDelta(text="abc"),
            MessageCompleted(usage=TokenUsage(input_tokens=7, output_tokens=2)),
        ]
    )

    summary = result.summary()

    assert summary == {
        "text_length": 10,
        "thinking_length": 3,
        "tool_call_count": 0,
        "invalid_tool_call_count": 0,
        "stop_reason": "end_turn",
        "input_tokens": 7,
        "output_tokens": 2,
        "completed": True,
    }
    assert "0123456789" not in str(summary)


def test_assembler_refuses_reuse_after_finish() -> None:
    assembler = StreamAssembler()
    assembler.feed(MessageCompleted())
    assembler.finish()

    with pytest.raises(RuntimeError, match="already called"):
        assembler.finish()
    with pytest.raises(RuntimeError, match="already called"):
        assembler.feed(TextDelta(text="late"))
