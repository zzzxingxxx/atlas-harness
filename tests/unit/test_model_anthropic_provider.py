"""The native Anthropic Messages adapter, driven through httpx's MockTransport.

No network is touched. Each test scripts the exact frames the API would send and
asserts the unified events the adapter produces, or the single ``provider_error``
event it produces instead of raising.

The request-shaping tests carry the most weight here. Stream parsing fails loudly
on the first live call; a wrongly shaped request body is accepted by a mock and
rejected by the real API, so the message layout is pinned mechanically.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

import httpx

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
    ToolCallCompleted,
    ToolCallStarted,
)
from atlas_harness.model.providers.anthropic_messages import (
    DEFAULT_ANTHROPIC_VERSION,
    DEFAULT_BASE_URL,
    FALLBACK_MAX_OUTPUT_TOKENS,
    AnthropicMessagesAdapter,
)


def _sse(*frames: dict[str, object]) -> bytes:
    """Render frames as a named-event SSE body, the way the API sends them."""

    rendered = []
    for frame in frames:
        name = frame.get("type", "message_delta")
        rendered.append(f"event: {name}\ndata: {json.dumps(frame)}\n\n")
    return "".join(rendered).encode("utf-8")


def _start(input_tokens: int = 7) -> dict[str, object]:
    return {
        "type": "message_start",
        "message": {
            "id": "msg_1",
            "role": "assistant",
            "content": [],
            "usage": {"input_tokens": input_tokens, "output_tokens": 0},
        },
    }


def _text_block(index: int = 0) -> dict[str, object]:
    return {"type": "content_block_start", "index": index, "content_block": {"type": "text"}}


def _text(text: str, index: int = 0) -> dict[str, object]:
    return {
        "type": "content_block_delta",
        "index": index,
        "delta": {"type": "text_delta", "text": text},
    }


def _stop_block(index: int = 0) -> dict[str, object]:
    return {"type": "content_block_stop", "index": index}


def _delta(reason: str = "end_turn", output_tokens: int = 3) -> dict[str, object]:
    return {
        "type": "message_delta",
        "delta": {"stop_reason": reason, "stop_sequence": None},
        "usage": {"output_tokens": output_tokens},
    }


def _stop() -> dict[str, object]:
    return {"type": "message_stop"}


def _text_turn(text: str = "hi there") -> bytes:
    return _sse(_start(), _text_block(), _text(text), _stop_block(), _delta(), _stop())


def _adapter(
    handler: object,
    *,
    max_retries: int = 0,
    api_key: str | None = "test-key",
    **kwargs: object,
) -> AnthropicMessagesAdapter:
    return AnthropicMessagesAdapter(
        model="claude-sonnet-4-5",
        api_key=api_key,
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        max_retries=max_retries,
        sleep=_no_sleep,
        **kwargs,  # type: ignore[arg-type]
    )


async def _no_sleep(_seconds: float) -> None:
    """Collapse backoff so retry tests stay instant."""


def _request(text: str = "hello", **kwargs: object) -> ModelRequest:
    return ModelRequest(
        model="claude-sonnet-4-5",
        messages=(ModelMessage.user(text),),
        **kwargs,  # type: ignore[arg-type]
    )


def _responder(body: bytes, *, status_code: int = 200) -> object:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content=body)

    return handler


def _capturing(body: bytes = b"") -> tuple[object, list[httpx.Request]]:
    """A handler that records every request it answers."""

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=body or _text_turn())

    return handler, seen


async def _drain(adapter: AnthropicMessagesAdapter, request: ModelRequest) -> list[ModelEvent]:
    try:
        return [event async for event in adapter.stream(request)]
    finally:
        await adapter.aclose()


async def _assemble(adapter: AnthropicMessagesAdapter, request: ModelRequest) -> AssembledResponse:
    assembler = StreamAssembler()
    assembler.feed_all(await _drain(adapter, request))
    return assembler.finish()


def _errors(events: Iterable[ModelEvent]) -> list[ProviderErrorEvent]:
    return [event for event in events if isinstance(event, ProviderErrorEvent)]


def _body_of(request: httpx.Request) -> dict[str, object]:
    decoded = json.loads(request.content)
    assert isinstance(decoded, dict)
    return decoded


# --------------------------------------------------------------------------- #
# the protocol seam
# --------------------------------------------------------------------------- #


def test_the_adapter_satisfies_the_protocol() -> None:
    assert isinstance(_adapter(_responder(_text_turn())), ModelAdapter)


def test_capabilities_come_from_the_catalog() -> None:
    capabilities = _adapter(_responder(_text_turn())).capabilities()

    assert capabilities.provider == "anthropic"
    assert capabilities.model == "claude-sonnet-4-5"
    assert capabilities.supports_tools is True


async def test_token_counting_stays_local() -> None:
    """The API has a real count endpoint, deliberately unused: this runs on the
    budget path before every request, so a billed round trip there would cost more
    than the work it measures. A handler that fails proves nothing is sent."""

    def refuse(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("count_tokens must not make a request")

    adapter = _adapter(refuse)
    try:
        assert await adapter.count_tokens(TokenInput(text="hello world")) > 0
    finally:
        await adapter.aclose()


def test_settings_build_an_adapter_with_the_pinned_version() -> None:
    settings = Settings(model_provider="anthropic", model_name="claude-sonnet-4-5")

    adapter = AnthropicMessagesAdapter.from_settings(settings)

    assert adapter._base_url == DEFAULT_BASE_URL
    assert adapter._anthropic_version == DEFAULT_ANTHROPIC_VERSION


# --------------------------------------------------------------------------- #
# request shape
# --------------------------------------------------------------------------- #


async def test_the_request_goes_to_the_messages_path_with_the_auth_headers() -> None:
    """``x-api-key`` and ``anthropic-version``, not a Bearer token: a missing
    version header is a 400, so it is never conditional."""

    handler, seen = _capturing()

    await _drain(_adapter(handler), _request())

    assert seen[0].url.path.endswith("/messages")
    assert seen[0].headers["x-api-key"] == "test-key"
    assert seen[0].headers["anthropic-version"] == DEFAULT_ANTHROPIC_VERSION
    assert "authorization" not in seen[0].headers


async def test_max_tokens_is_always_sent_because_the_api_requires_it() -> None:
    handler, seen = _capturing()

    await _drain(_adapter(handler, max_output_tokens=None), _request())

    assert _body_of(seen[0])["max_tokens"] == FALLBACK_MAX_OUTPUT_TOKENS


async def test_the_request_limit_wins_over_the_configured_one() -> None:
    handler, seen = _capturing()

    await _drain(_adapter(handler, max_output_tokens=1_000), _request(max_output_tokens=256))

    assert _body_of(seen[0])["max_tokens"] == 256


async def test_the_system_prompt_is_hoisted_out_of_the_messages() -> None:
    """Only user and assistant roles exist in this dialect."""

    handler, seen = _capturing()
    request = ModelRequest(
        model="claude-sonnet-4-5",
        messages=(ModelMessage.system("be brief"), ModelMessage.user("hi")),
    )

    await _drain(_adapter(handler), request)

    body = _body_of(seen[0])
    assert body["system"] == "be brief"
    assert [message["role"] for message in body["messages"]] == ["user"]  # type: ignore[index,union-attr]


async def test_several_system_turns_are_joined_rather_than_dropped() -> None:
    """A compiled context can produce more than one. Letting the last one win
    would silently discard instructions the compiler decided to include."""

    handler, seen = _capturing()
    request = ModelRequest(
        model="claude-sonnet-4-5",
        messages=(
            ModelMessage.system("be brief"),
            ModelMessage.system("cite files"),
            ModelMessage.user("hi"),
        ),
    )

    await _drain(_adapter(handler), request)

    assert _body_of(seen[0])["system"] == "be brief\n\ncite files"


async def test_no_system_turn_means_no_system_field() -> None:
    handler, seen = _capturing()

    await _drain(_adapter(handler), _request())

    assert "system" not in _body_of(seen[0])


async def test_tools_use_the_input_schema_spelling() -> None:
    handler, seen = _capturing()
    request = _request(
        tools=({"name": "read_file", "description": "read it", "input_schema": {"type": "object"}},)
    )

    await _drain(_adapter(handler), request)

    body = _body_of(seen[0])
    assert body["tools"] == [
        {"name": "read_file", "description": "read it", "input_schema": {"type": "object"}}
    ]
    assert body["tool_choice"] == {"type": "auto"}


async def test_stop_sequences_use_the_dialects_field_name() -> None:
    handler, seen = _capturing()

    await _drain(_adapter(handler), _request(stop=("STOP",), temperature=0.2))

    body = _body_of(seen[0])
    assert body["stop_sequences"] == ["STOP"]
    assert body["temperature"] == 0.2
    assert "stop" not in body


async def test_an_assistant_turn_replays_text_and_tool_use_blocks() -> None:
    handler, seen = _capturing()
    call = ModelToolCall(call_id="call_1", name="read_file", arguments={"path": "a.txt"})
    request = ModelRequest(
        model="claude-sonnet-4-5",
        messages=(
            ModelMessage.user("read it"),
            ModelMessage.assistant("on it", tool_calls=(call,)),
            ModelMessage.tool(tool_call_id="call_1", content="hello"),
        ),
    )

    await _drain(_adapter(handler), request)

    messages = _body_of(seen[0])["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant", "user"]  # type: ignore[index,union-attr]
    assert messages[1]["content"] == [  # type: ignore[index]
        {"type": "text", "text": "on it"},
        {
            "type": "tool_use",
            "id": "call_1",
            "name": "read_file",
            "input": {"path": "a.txt"},
        },
    ]
    assert messages[2]["content"] == [  # type: ignore[index]
        {"type": "tool_result", "tool_use_id": "call_1", "content": "hello"}
    ]


async def test_consecutive_tool_results_merge_into_one_user_turn() -> None:
    """Required, not an optimisation: the API rejects two user messages in a row,
    and a parallel tool batch produces one ``ModelMessage`` per result."""

    handler, seen = _capturing()
    calls = (
        ModelToolCall(call_id="c1", name="read_file", arguments={"path": "a"}),
        ModelToolCall(call_id="c2", name="read_file", arguments={"path": "b"}),
    )
    request = ModelRequest(
        model="claude-sonnet-4-5",
        messages=(
            ModelMessage.user("read both"),
            ModelMessage.assistant(tool_calls=calls),
            ModelMessage.tool(tool_call_id="c1", content="A"),
            ModelMessage.tool(tool_call_id="c2", content="B"),
        ),
    )

    await _drain(_adapter(handler), request)

    messages = _body_of(seen[0])["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant", "user"]  # type: ignore[index,union-attr]
    assert [block["tool_use_id"] for block in messages[2]["content"]] == ["c1", "c2"]  # type: ignore[index]


async def test_roles_never_repeat_across_a_multi_turn_tool_conversation() -> None:
    """The alternation rule, asserted on the whole sequence rather than one pair,
    because a merge that only handles the last run still produces a 400."""

    handler, seen = _capturing()
    request = ModelRequest(
        model="claude-sonnet-4-5",
        messages=(
            ModelMessage.user("first"),
            ModelMessage.assistant(
                tool_calls=(ModelToolCall(call_id="c1", name="read_file", arguments={}),)
            ),
            ModelMessage.tool(tool_call_id="c1", content="A"),
            ModelMessage.tool(tool_call_id="c2", content="B"),
            ModelMessage.assistant(
                tool_calls=(ModelToolCall(call_id="c3", name="read_file", arguments={}),)
            ),
            ModelMessage.tool(tool_call_id="c3", content="C"),
        ),
    )

    await _drain(_adapter(handler), request)

    roles = [message["role"] for message in _body_of(seen[0])["messages"]]  # type: ignore[index,union-attr]
    assert roles == ["user", "assistant", "user", "assistant", "user"]
    assert all(a != b for a, b in zip(roles, roles[1:], strict=False))


async def test_an_invalid_call_replays_with_an_empty_input_object() -> None:
    """``input`` is a JSON object in this dialect, so malformed argument text has
    nowhere to go. The error still reaches the model via the tool_result."""

    handler, seen = _capturing()
    call = ModelToolCall(
        call_id="c1",
        name="read_file",
        raw_arguments="{not json",
        valid=False,
        error="invalid JSON",
    )
    request = ModelRequest(
        model="claude-sonnet-4-5",
        messages=(
            ModelMessage.user("read it"),
            ModelMessage.assistant(tool_calls=(call,)),
            ModelMessage.tool(tool_call_id="c1", content="error: invalid JSON"),
        ),
    )

    await _drain(_adapter(handler), request)

    messages = _body_of(seen[0])["messages"]
    assert messages[1]["content"][0]["input"] == {}  # type: ignore[index]


# --------------------------------------------------------------------------- #
# stream parsing
# --------------------------------------------------------------------------- #


async def test_text_deltas_assemble_into_one_message() -> None:
    body = _sse(
        _start(),
        _text_block(),
        _text("hello "),
        _text("world"),
        _stop_block(),
        _delta(output_tokens=5),
        _stop(),
    )

    assembled = await _assemble(_adapter(_responder(body)), _request())

    assert assembled.text == "hello world"
    assert assembled.stop_reason is StopReason.END_TURN
    assert assembled.usage.input_tokens == 7
    assert assembled.usage.output_tokens == 5


async def test_a_ping_frame_is_ignored() -> None:
    body = _sse(
        _start(),
        {"type": "ping"},
        _text_block(),
        _text("ok"),
        _stop_block(),
        _delta(),
        _stop(),
    )

    events = await _drain(_adapter(_responder(body)), _request())

    assert [event for event in events if isinstance(event, TextDelta)]
    assert not _errors(events)


async def test_thinking_deltas_map_onto_the_unified_event() -> None:
    body = _sse(
        _start(),
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "let me check"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "abc"},
        },
        _stop_block(),
        _text_block(1),
        _text("done", 1),
        _stop_block(1),
        _delta(),
        _stop(),
    )

    events = await _drain(_adapter(_responder(body)), _request())

    thinking = [event for event in events if isinstance(event, ThinkingDelta)]
    assert [event.text for event in thinking] == ["let me check"]
    assert not _errors(events)


async def test_a_tool_use_block_assembles_from_partial_json() -> None:
    body = _sse(
        _start(),
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "call_1", "name": "read_file"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"path": '},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '"a.txt"}'},
        },
        _stop_block(),
        _delta(reason="tool_use"),
        _stop(),
    )

    assembled = await _assemble(_adapter(_responder(body)), _request())

    assert assembled.stop_reason is StopReason.TOOL_USE
    assert len(assembled.tool_calls) == 1
    call = assembled.tool_calls[0]
    assert (call.call_id, call.name) == ("call_1", "read_file")
    assert call.arguments == {"path": "a.txt"}
    assert call.valid is True


async def test_a_tool_use_block_with_no_arguments_assembles_as_empty() -> None:
    """Anthropic sends no ``input_json_delta`` for a no-argument tool, so the
    assembler has to treat an announced-but-empty block as ``{}`` rather than as
    malformed JSON."""

    body = _sse(
        _start(),
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "call_1", "name": "list_files"},
        },
        _stop_block(),
        _delta(reason="tool_use"),
        _stop(),
    )

    assembled = await _assemble(_adapter(_responder(body)), _request())

    assert len(assembled.tool_calls) == 1
    assert assembled.tool_calls[0].arguments == {}


async def test_text_and_tool_blocks_at_different_indices_stay_separate() -> None:
    body = _sse(
        _start(),
        _text_block(0),
        _text("looking", 0),
        _stop_block(0),
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "id": "c1", "name": "read_file"},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": '{"path": "a"}'},
        },
        _stop_block(1),
        {
            "type": "content_block_start",
            "index": 2,
            "content_block": {"type": "tool_use", "id": "c2", "name": "read_file"},
        },
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": '{"path": "b"}'},
        },
        _stop_block(2),
        _delta(reason="tool_use"),
        _stop(),
    )

    assembled = await _assemble(_adapter(_responder(body)), _request())

    assert assembled.text == "looking"
    assert [call.call_id for call in assembled.tool_calls] == ["c1", "c2"]
    assert [call.arguments["path"] for call in assembled.tool_calls] == ["a", "b"]


async def test_only_tool_blocks_are_announced_and_completed() -> None:
    """A text block creates no slot in the assembler, so announcing one would
    invent a tool call with no name."""

    body = _sse(_start(), _text_block(), _text("ok"), _stop_block(), _delta(), _stop())

    events = await _drain(_adapter(_responder(body)), _request())

    assert not [event for event in events if isinstance(event, ToolCallStarted)]
    assert not [event for event in events if isinstance(event, ToolCallCompleted)]


async def test_stop_reasons_map_onto_the_unified_vocabulary() -> None:
    for reason, expected in (
        ("end_turn", StopReason.END_TURN),
        ("max_tokens", StopReason.MAX_TOKENS),
        ("stop_sequence", StopReason.STOP_SEQUENCE),
        ("refusal", StopReason.ERROR),
    ):
        body = _sse(
            _start(), _text_block(), _text("x"), _stop_block(), _delta(reason=reason), _stop()
        )

        assembled = await _assemble(_adapter(_responder(body)), _request())

        assert assembled.stop_reason is expected


async def test_an_unknown_stop_reason_falls_back_to_end_turn() -> None:
    body = _sse(
        _start(),
        _text_block(),
        _text("x"),
        _stop_block(),
        _delta(reason="something_new"),
        _stop(),
    )

    assembled = await _assemble(_adapter(_responder(body)), _request())

    assert assembled.stop_reason is StopReason.END_TURN


async def test_tool_blocks_force_tool_use_when_the_stop_reason_is_missing() -> None:
    """A gateway that omits stop_reason while emitting tool_use blocks would
    otherwise end the loop with the calls never executed."""

    body = _sse(
        _start(),
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "c1", "name": "read_file"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": "{}"},
        },
        _stop_block(),
        _stop(),
    )

    assembled = await _assemble(_adapter(_responder(body)), _request())

    assert assembled.stop_reason is StopReason.TOOL_USE


async def test_usage_survives_arriving_across_two_frames() -> None:
    """Input tokens come in ``message_start`` and output tokens in
    ``message_delta``, and the second frame omits the first field."""

    body = _sse(
        _start(input_tokens=11),
        _text_block(),
        _text("x"),
        _stop_block(),
        _delta(output_tokens=4),
        _stop(),
    )

    assembled = await _assemble(_adapter(_responder(body)), _request())

    assert (assembled.usage.input_tokens, assembled.usage.output_tokens) == (11, 4)


# --------------------------------------------------------------------------- #
# failures are events
# --------------------------------------------------------------------------- #


async def test_a_missing_key_is_reported_without_a_request() -> None:
    events = await _drain(
        AnthropicMessagesAdapter(model="claude-sonnet-4-5", api_key=None), _request()
    )

    assert _errors(events)[0].error_code == "missing_api_key"


async def test_an_http_error_becomes_one_provider_error_event() -> None:
    body = json.dumps({"type": "error", "error": {"message": "credit balance too low"}}).encode()
    events = await _drain(_adapter(_responder(body, status_code=400)), _request())

    error = _errors(events)[0]
    assert error.status_code == 400
    assert error.error_code == "provider_http_400"
    assert error.retryable is False
    assert "credit balance too low" in error.message


async def test_an_error_frame_mid_stream_is_reported_not_raised() -> None:
    body = _sse(
        _start(),
        _text_block(),
        _text("partial"),
        {"type": "error", "error": {"type": "overloaded_error", "message": "overloaded"}},
    )

    events = await _drain(_adapter(_responder(body)), _request())

    error = _errors(events)[0]
    assert error.error_code == "provider_stream_error"
    assert "overloaded" in error.message


async def test_a_stream_that_never_stops_is_incomplete() -> None:
    body = _sse(_start(), _text_block(), _text("half"))

    events = await _drain(_adapter(_responder(body)), _request())

    assert _errors(events)[0].error_code == "provider_incomplete_stream"


async def test_a_malformed_frame_is_not_retryable() -> None:
    body = b"event: content_block_delta\ndata: {not json\n\n"

    events = await _drain(_adapter(_responder(body)), _request())

    error = _errors(events)[0]
    assert error.error_code == "provider_bad_chunk"
    assert error.retryable is False


async def test_a_transient_status_is_retried_before_anything_is_emitted() -> None:
    attempts = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(529, content=b'{"error": {"message": "overloaded"}}')
        return httpx.Response(200, content=_text_turn("recovered"))

    assembled = await _assemble(_adapter(handler, max_retries=2), _request())

    assert attempts["count"] == 2
    assert assembled.text == "recovered"


async def test_a_break_after_the_first_delta_is_reported_rather_than_retried() -> None:
    """The one rule the retry logic exists to protect: replaying a stream that
    already delivered text would hand the caller that text twice."""

    attempts = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(200, content=_sse(_start(), _text_block(), _text("partial")))

    events = await _drain(_adapter(handler, max_retries=3), _request())

    assert attempts["count"] == 1
    error = _errors(events)[0]
    assert error.error_code == "provider_incomplete_stream"
    assert error.retryable is False
