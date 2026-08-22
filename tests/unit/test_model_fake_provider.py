"""The scripted adapter and the shared token estimate."""

from __future__ import annotations

import pytest

from atlas_harness.config import Settings
from atlas_harness.model.assembler import AssembledResponse, StreamAssembler
from atlas_harness.model.protocol import (
    ModelAdapter,
    ModelEvent,
    ModelMessage,
    ModelRequest,
    ModelToolCall,
    StopReason,
    TextDelta,
    TokenInput,
    TokenUsage,
)
from atlas_harness.model.providers.fake import (
    CANNED_TEXT,
    FakeAdapter,
    error_turn,
    malformed_tool_call_turn,
    text_turn,
    tool_call_turn,
    truncated_turn,
)
from atlas_harness.model.tokens import count_tokens_estimate


def _request(
    text: str = "hello",
    *,
    tools: tuple[dict[str, object], ...] = (),
) -> ModelRequest:
    return ModelRequest(
        model="fake-model",
        messages=(ModelMessage.user(text),),
        tools=tools,
    )


async def _drain(adapter: FakeAdapter, request: ModelRequest) -> list[ModelEvent]:
    return [event async for event in adapter.stream(request)]


async def _assemble(adapter: FakeAdapter, request: ModelRequest) -> AssembledResponse:
    assembler = StreamAssembler()
    async for event in adapter.stream(request):
        assembler.feed(event)
    return assembler.finish()


def test_fake_adapter_satisfies_the_model_adapter_protocol() -> None:
    assert isinstance(FakeAdapter(), ModelAdapter)


async def test_canned_mode_answers_every_call_without_a_script() -> None:
    adapter = FakeAdapter()

    first = await _assemble(adapter, _request())
    second = await _assemble(adapter, _request())

    assert first.text == CANNED_TEXT
    assert second.text == CANNED_TEXT
    assert adapter.calls == 2
    assert adapter.remaining_turns == -1


async def test_scripted_turns_are_served_in_order() -> None:
    adapter = FakeAdapter([text_turn("first"), text_turn("second")])

    assert (await _assemble(adapter, _request())).text == "first"
    assert adapter.remaining_turns == 1
    assert (await _assemble(adapter, _request())).text == "second"
    assert adapter.remaining_turns == 0


async def test_exhausted_script_raises_because_it_signals_a_test_bug() -> None:
    adapter = FakeAdapter([text_turn("only one")])
    await _drain(adapter, _request())

    with pytest.raises(RuntimeError, match="script exhausted"):
        await _drain(adapter, _request())


async def test_every_request_is_recorded_for_assertions() -> None:
    adapter = FakeAdapter([text_turn("ok"), text_turn("ok")])
    tools: tuple[dict[str, object], ...] = ({"type": "function", "name": "read_file"},)

    await _drain(adapter, _request("first"))
    await _drain(adapter, _request("second", tools=tools))

    assert len(adapter.requests) == 2
    assert adapter.requests[0].messages[0].content == "first"
    last = adapter.last_request()
    assert last is not None
    assert last.tools == tools


async def test_text_turn_streams_in_chunks_that_reassemble() -> None:
    adapter = FakeAdapter([text_turn("abcdefghij", chunk_size=3)])

    events = await _drain(adapter, _request())

    deltas = [event.text for event in events if isinstance(event, TextDelta)]
    assert len(deltas) == 4
    assert "".join(deltas) == "abcdefghij"


async def test_tool_call_turn_reassembles_into_parsed_arguments() -> None:
    adapter = FakeAdapter(
        [tool_call_turn("read_file", {"path": "notes.txt"}, call_id="call_7", chunk_size=4)]
    )

    result = await _assemble(adapter, _request())

    assert result.stop_reason is StopReason.TOOL_USE
    (call,) = result.tool_calls
    assert call.call_id == "call_7"
    assert call.name == "read_file"
    assert call.arguments == {"path": "notes.txt"}
    assert call.valid is True


async def test_malformed_tool_call_turn_yields_an_invalid_call() -> None:
    adapter = FakeAdapter([malformed_tool_call_turn("read_file", "{not json")])

    result = await _assemble(adapter, _request())

    (call,) = result.tool_calls
    assert call.valid is False
    assert call.raw_arguments == "{not json"


async def test_error_turn_surfaces_a_provider_error() -> None:
    adapter = FakeAdapter([error_turn("upstream exploded", status_code=503)])

    result = await _assemble(adapter, _request())

    assert result.failed is True
    assert result.error is not None
    assert result.error.status_code == 503
    assert result.error.retryable is True


async def test_truncated_turn_is_incomplete() -> None:
    adapter = FakeAdapter([truncated_turn("half")])

    result = await _assemble(adapter, _request())

    assert result.completed is False
    assert result.failed is True


async def test_usage_flows_through_the_stream() -> None:
    adapter = FakeAdapter([text_turn("hi", usage=TokenUsage(input_tokens=11, output_tokens=3))])

    result = await _assemble(adapter, _request())

    assert result.usage.input_tokens == 11
    assert result.usage.total_tokens == 14


async def test_thinking_is_kept_separate_from_the_answer() -> None:
    adapter = FakeAdapter([text_turn("answer", thinking="deliberating")])

    result = await _assemble(adapter, _request())

    assert result.text == "answer"
    assert result.thinking == "deliberating"


def test_capabilities_come_from_the_catalog() -> None:
    capabilities = FakeAdapter().capabilities()

    assert capabilities.provider == "fake"
    assert capabilities.model == "fake-model"
    assert capabilities.supports_thinking is True


def test_from_settings_uses_the_configured_model_name() -> None:
    adapter = FakeAdapter.from_settings(Settings(model_name="some-other-model"))

    assert adapter.capabilities().model == "some-other-model"


async def test_count_tokens_is_deterministic_and_grows_with_input() -> None:
    adapter = FakeAdapter()
    small = TokenInput(messages=(ModelMessage.user("hi"),))
    large = TokenInput(messages=(ModelMessage.user("hi" * 200),))

    assert await adapter.count_tokens(small) == await adapter.count_tokens(small)
    assert await adapter.count_tokens(large) > await adapter.count_tokens(small)


def test_token_estimate_counts_messages_tools_and_text() -> None:
    empty = count_tokens_estimate(TokenInput())
    with_message = count_tokens_estimate(TokenInput(messages=(ModelMessage.user("abcd"),)))
    with_tools = count_tokens_estimate(TokenInput(tools=({"name": "read_file"},)))
    with_text = count_tokens_estimate(TokenInput(text="abcd"))

    assert empty == 0
    assert with_message > empty
    assert with_tools > empty
    assert with_text == 1


def test_token_estimate_includes_tool_call_arguments() -> None:
    call = ModelToolCall(
        call_id="c1",
        name="read_file",
        raw_arguments='{"path": "a-long-file-name.txt"}',
    )
    plain = TokenInput(messages=(ModelMessage.assistant("call it"),))
    with_call = TokenInput(messages=(ModelMessage.assistant("call it", tool_calls=(call,)),))

    assert count_tokens_estimate(with_call) > count_tokens_estimate(plain)
