"""The OpenAI-dialect adapter, driven through httpx's MockTransport.

No network is touched. Each test scripts the exact bytes a gateway would send
and asserts the unified events the adapter produces, or the single
``provider_error`` event it produces instead of raising.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

import httpx
import pytest

from atlas_harness.config import Settings
from atlas_harness.model.assembler import AssembledResponse, StreamAssembler
from atlas_harness.model.protocol import (
    ModelAdapter,
    ModelEvent,
    ModelMessage,
    ModelRequest,
    ModelToolCall,
    ProviderErrorEvent,
    StopReason,
    TextDelta,
    ThinkingDelta,
    TokenInput,
)
from atlas_harness.model.providers.openai_compatible import (
    DEFAULT_BASE_URL,
    OpenAICompatibleAdapter,
)


def _sse(*chunks: dict[str, object], done: bool = True) -> bytes:
    """Render chunks as a chat-completions SSE body."""

    lines = [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks]
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode("utf-8")


def _text_chunk(text: str) -> dict[str, object]:
    return {"choices": [{"index": 0, "delta": {"content": text}}]}


def _finish_chunk(reason: str = "stop") -> dict[str, object]:
    return {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}


def _adapter(
    handler: object,
    *,
    max_retries: int = 0,
    api_key: str | None = "test-key",
    **kwargs: object,
) -> OpenAICompatibleAdapter:
    """Build an adapter whose transport is a scripted handler."""

    return OpenAICompatibleAdapter(
        model="gpt-4o-mini",
        api_key=api_key,
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        max_retries=max_retries,
        sleep=_no_sleep,
        **kwargs,  # type: ignore[arg-type]
    )


async def _no_sleep(_seconds: float) -> None:
    """Collapse backoff so retry tests stay instant."""


def _request(text: str = "hello") -> ModelRequest:
    return ModelRequest(model="gpt-4o-mini", messages=(ModelMessage.user(text),))


def _responder(body: bytes, *, status_code: int = 200) -> object:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content=body)

    return handler


async def _drain(adapter: OpenAICompatibleAdapter, request: ModelRequest) -> list[ModelEvent]:
    try:
        return [event async for event in adapter.stream(request)]
    finally:
        await adapter.aclose()


async def _assemble(adapter: OpenAICompatibleAdapter, request: ModelRequest) -> AssembledResponse:
    assembler = StreamAssembler()
    assembler.feed_all(await _drain(adapter, request))
    return assembler.finish()


def _errors(events: Iterable[ModelEvent]) -> list[ProviderErrorEvent]:
    return [event for event in events if isinstance(event, ProviderErrorEvent)]


def test_adapter_satisfies_the_model_adapter_protocol() -> None:
    assert isinstance(_adapter(_responder(_sse())), ModelAdapter)


async def test_text_stream_reassembles_into_one_answer() -> None:
    body = _sse(_text_chunk("Hel"), _text_chunk("lo"), _finish_chunk())
    adapter = _adapter(_responder(body))

    result = await _assemble(adapter, _request())

    assert result.text == "Hello"
    assert result.stop_reason is StopReason.END_TURN
    assert result.completed is True
    assert result.failed is False


async def test_usage_is_captured_from_the_final_frame() -> None:
    body = _sse(
        _text_chunk("hi"),
        _finish_chunk(),
        {"choices": [], "usage": {"prompt_tokens": 31, "completion_tokens": 7}},
    )

    result = await _assemble(_adapter(_responder(body)), _request())

    assert result.usage.input_tokens == 31
    assert result.usage.output_tokens == 7


async def test_reasoning_content_maps_to_thinking() -> None:
    body = _sse(
        {"choices": [{"index": 0, "delta": {"reasoning_content": "thinking..."}}]},
        _text_chunk("answer"),
        _finish_chunk(),
    )

    events = await _drain(_adapter(_responder(body)), _request())

    assert [event.text for event in events if isinstance(event, ThinkingDelta)] == ["thinking..."]
    assert [event.text for event in events if isinstance(event, TextDelta)] == ["answer"]


async def test_tool_call_fragments_reassemble_into_parsed_arguments() -> None:
    body = _sse(
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_abc",
                                "function": {"name": "read_file", "arguments": ""},
                            }
                        ]
                    },
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [{"index": 0, "function": {"arguments": '{"path": "a'}}]
                    },
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '.txt"}'}}]},
                }
            ]
        },
        _finish_chunk("tool_calls"),
    )

    result = await _assemble(_adapter(_responder(body)), _request())

    assert result.stop_reason is StopReason.TOOL_USE
    (call,) = result.tool_calls
    assert call.call_id == "call_abc"
    assert call.name == "read_file"
    assert call.arguments == {"path": "a.txt"}
    assert call.valid is True


async def test_two_parallel_tool_calls_stay_separate() -> None:
    body = _sse(
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "function": {"name": "read_file", "arguments": '{"path":"a"}'},
                            },
                            {
                                "index": 1,
                                "id": "call_2",
                                "function": {"name": "read_file", "arguments": '{"path":"b"}'},
                            },
                        ]
                    },
                }
            ]
        },
        _finish_chunk("tool_calls"),
    )

    result = await _assemble(_adapter(_responder(body)), _request())

    assert [call.call_id for call in result.tool_calls] == ["call_1", "call_2"]
    assert [call.arguments["path"] for call in result.tool_calls] == ["a", "b"]


async def test_length_finish_reason_maps_to_max_tokens() -> None:
    body = _sse(_text_chunk("truncated"), _finish_chunk("length"))

    result = await _assemble(_adapter(_responder(body)), _request())

    assert result.stop_reason is StopReason.MAX_TOKENS


async def test_comments_and_blank_lines_are_ignored() -> None:
    body = (
        b": keep-alive\n\n"
        b"\n" + _sse(_text_chunk("ok"), _finish_chunk(), done=False) + b"event: ping\n\n"
        b"data: [DONE]\n\n"
    )

    result = await _assemble(_adapter(_responder(body)), _request())

    assert result.text == "ok"
    assert result.completed is True


async def test_missing_api_key_is_reported_as_an_event_not_an_exception() -> None:
    adapter = OpenAICompatibleAdapter(model="gpt-4o-mini", api_key=None)

    events = await _drain(adapter, _request())

    (error,) = _errors(events)
    assert error.error_code == "missing_api_key"
    assert error.retryable is False


async def test_non_retryable_http_status_yields_one_error_event() -> None:
    body = json.dumps({"error": {"message": "invalid api key"}}).encode()
    adapter = _adapter(_responder(body, status_code=401), max_retries=3)

    events = await _drain(adapter, _request())

    (error,) = _errors(events)
    assert error.status_code == 401
    assert error.retryable is False
    assert "invalid api key" in error.message


async def test_retryable_status_is_retried_then_succeeds() -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, content=b"upstream busy")
        return httpx.Response(200, content=_sse(_text_chunk("recovered"), _finish_chunk()))

    result = await _assemble(_adapter(handler, max_retries=2), _request())

    assert attempts == 2
    assert result.text == "recovered"
    assert result.failed is False


async def test_retries_are_bounded_by_max_retries() -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, content=b"slow down")

    events = await _drain(_adapter(handler, max_retries=2), _request())

    assert attempts == 3
    (error,) = _errors(events)
    assert error.status_code == 429
    assert error.attempt == 3


async def test_transport_failure_is_reported_as_a_provider_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    events = await _drain(_adapter(handler, max_retries=0), _request())

    (error,) = _errors(events)
    assert error.error_code == "provider_transport_error"
    assert "connection refused" in error.message


async def test_timeout_is_reported_as_a_provider_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    events = await _drain(_adapter(handler, max_retries=0), _request())

    (error,) = _errors(events)
    assert error.error_code == "provider_timeout"


async def test_stream_ending_without_a_completion_marker_is_an_error() -> None:
    body = _sse(_text_chunk("half an answer"), done=False)

    events = await _drain(_adapter(_responder(body), max_retries=0), _request())

    assert [event.text for event in events if isinstance(event, TextDelta)] == ["half an answer"]
    (error,) = _errors(events)
    assert error.error_code == "provider_incomplete_stream"


async def test_a_break_after_output_is_not_marked_retryable() -> None:
    """Replaying a request whose text already shipped would duplicate it."""

    body = _sse(_text_chunk("partial"), done=False)
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, content=body)

    events = await _drain(_adapter(handler, max_retries=3), _request())

    assert attempts == 1
    (error,) = _errors(events)
    assert error.retryable is False


async def test_malformed_chunk_is_not_retried() -> None:
    body = b"data: {not json}\n\n"

    events = await _drain(_adapter(_responder(body), max_retries=2), _request())

    (error,) = _errors(events)
    assert error.error_code == "provider_bad_chunk"
    assert error.retryable is False


async def test_mid_stream_error_frame_is_surfaced() -> None:
    body = _sse({"error": {"message": "context length exceeded"}}, done=False)

    events = await _drain(_adapter(_responder(body)), _request())

    (error,) = _errors(events)
    assert error.error_code == "provider_stream_error"
    assert "context length exceeded" in error.message


async def test_request_body_carries_messages_tools_and_limits() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, content=_sse(_text_chunk("ok"), _finish_chunk()))

    tools: tuple[dict[str, object], ...] = (
        {"type": "function", "function": {"name": "read_file", "parameters": {}}},
    )
    request = ModelRequest(
        model="gpt-4o-mini",
        messages=(ModelMessage.system("be brief"), ModelMessage.user("hi")),
        tools=tools,
        max_output_tokens=256,
        temperature=0.2,
        stop=("STOP",),
    )

    await _drain(_adapter(handler), request)

    assert captured["stream"] is True
    assert captured["max_tokens"] == 256
    assert captured["temperature"] == 0.2
    assert captured["stop"] == ["STOP"]
    assert captured["tool_choice"] == "auto"
    assert captured["tools"] == list(tools)
    assert [message["role"] for message in captured["messages"]] == ["system", "user"]  # type: ignore[union-attr]


async def test_tool_result_turns_are_sent_back_in_wire_format() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, content=_sse(_text_chunk("ok"), _finish_chunk()))

    call = ModelToolCall(
        call_id="call_9",
        name="read_file",
        arguments={"path": "a.txt"},
        raw_arguments='{"path": "a.txt"}',
    )
    request = ModelRequest(
        model="gpt-4o-mini",
        messages=(
            ModelMessage.user("read it"),
            ModelMessage.assistant(tool_calls=(call,)),
            ModelMessage.tool(tool_call_id="call_9", content="file body", name="read_file"),
        ),
    )

    await _drain(_adapter(handler), request)

    messages = captured["messages"]
    assert isinstance(messages, list)
    assistant, tool_turn = messages[1], messages[2]
    # A tool-only assistant turn must send null content, not "".
    assert assistant["content"] is None
    assert assistant["tool_calls"][0]["function"]["arguments"] == '{"path": "a.txt"}'
    assert tool_turn == {
        "role": "tool",
        "tool_call_id": "call_9",
        "content": "file body",
    }


async def test_invalid_tool_call_is_replayed_with_its_original_text() -> None:
    """The model must see the malformed JSON it produced, not a cleaned version."""

    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, content=_sse(_text_chunk("ok"), _finish_chunk()))

    broken = ModelToolCall(
        call_id="call_bad",
        name="read_file",
        raw_arguments="{not json",
        valid=False,
        error="arguments are not valid JSON",
    )
    request = ModelRequest(
        model="gpt-4o-mini",
        messages=(ModelMessage.assistant(tool_calls=(broken,)),),
    )

    await _drain(_adapter(handler), request)

    messages = captured["messages"]
    assert isinstance(messages, list)
    assert messages[0]["tool_calls"][0]["function"]["arguments"] == "{not json"


async def test_authorization_header_carries_the_key() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, content=_sse(_text_chunk("ok"), _finish_chunk()))

    await _drain(_adapter(handler, api_key="secret-key"), _request())

    assert seen["authorization"] == "Bearer secret-key"


async def test_base_url_is_used_for_the_completions_path() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=_sse(_text_chunk("ok"), _finish_chunk()))

    adapter = _adapter(handler, base_url="https://gateway.example/v9/")
    await _drain(adapter, _request())

    assert seen == ["https://gateway.example/v9/chat/completions"]


def test_from_settings_maps_configuration_onto_the_adapter() -> None:
    settings = Settings(
        model_provider="deepseek",
        model_name="deepseek-chat",
        model_base_url="https://api.deepseek.com/v1",
        model_api_key="k",  # type: ignore[arg-type]
    )

    adapter = OpenAICompatibleAdapter.from_settings(settings)

    assert adapter.capabilities().provider == "deepseek"
    assert adapter.capabilities().max_context_tokens == 64_000


def test_default_base_url_is_openai() -> None:
    adapter = OpenAICompatibleAdapter.from_settings(Settings(model_api_key="k"))  # type: ignore[arg-type]

    assert adapter.capabilities().provider == "openai"
    assert DEFAULT_BASE_URL == "https://api.openai.com/v1"


async def test_count_tokens_falls_back_to_a_local_estimate() -> None:
    adapter = _adapter(_responder(_sse()))

    count = await adapter.count_tokens(TokenInput(messages=(ModelMessage.user("hello"),)))

    assert count > 0


async def test_aclose_is_idempotent() -> None:
    adapter = _adapter(_responder(_sse(_text_chunk("ok"), _finish_chunk())))
    await _drain(adapter, _request())

    await adapter.aclose()
    await adapter.aclose()


async def test_adapter_works_as_an_async_context_manager() -> None:
    body = _sse(_text_chunk("ok"), _finish_chunk())

    async with _adapter(_responder(body)) as adapter:
        assembler = StreamAssembler()
        async for event in adapter.stream(_request()):
            assembler.feed(event)

    assert assembler.finish().text == "ok"


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("stop", StopReason.END_TURN),
        ("tool_calls", StopReason.TOOL_USE),
        ("length", StopReason.MAX_TOKENS),
        ("content_filter", StopReason.ERROR),
        ("something_new", StopReason.END_TURN),
    ],
)
async def test_finish_reasons_map_onto_stop_reasons(reason: str, expected: StopReason) -> None:
    body = _sse(_text_chunk("x"), _finish_chunk(reason))

    result = await _assemble(_adapter(_responder(body)), _request())

    assert result.stop_reason is expected
