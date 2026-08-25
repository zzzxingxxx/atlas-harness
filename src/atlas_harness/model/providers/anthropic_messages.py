"""Adapter for the native Anthropic Messages API.

This is a second dialect, not a variation on the first. The differences that
actually change the code, rather than just the URL:

* **Auth.** ``x-api-key`` plus a required ``anthropic-version``, not a Bearer
  token. A missing version header is a 400, so it is always sent.
* **The system prompt is not a message.** Only ``user`` and ``assistant`` roles
  exist; system text is a top-level ``system`` parameter. Several system turns
  are joined rather than dropped, because a compiled context can legitimately
  produce more than one.
* **Roles must alternate.** Tool results are ``tool_result`` content blocks
  inside a *user* message, and consecutive results have to be merged into one
  turn — a run of three tool messages is one user message with three blocks, not
  three user messages the API will reject.
* **``max_tokens`` is required.** No configured limit means falling back to the
  model's documented ceiling rather than omitting the field.
* **The stream is named SSE events**, not anonymous ``data:`` frames, and tool
  arguments arrive as ``input_json_delta`` text fragments inside a
  ``content_block_delta``. Block indices pass straight through to the assembler,
  which keys tool slots by index.

Error handling and retries come from :mod:`._http`, so the "retry only before
the first event is delivered" guarantee has one implementation across dialects.

Network note: the endpoint comes from operator configuration
(``ATLAS_MODEL_BASE_URL``), never from model output, so it is trusted egress and
is not subject to the tool-level network policy. The API key is read from
settings at call time and is never logged, echoed into an error, or persisted to
an event.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from types import TracebackType
from typing import Any

import httpx

from atlas_harness.config import Settings
from atlas_harness.model.catalog import capabilities_for
from atlas_harness.model.protocol import (
    MessageCompleted,
    ModelCapabilities,
    ModelEvent,
    ModelMessage,
    ModelRequest,
    ProviderErrorEvent,
    Role,
    StopReason,
    TextDelta,
    ThinkingDelta,
    TokenInput,
    TokenUsage,
    ToolCallCompleted,
    ToolCallDelta,
    ToolCallStarted,
)
from atlas_harness.model.providers._http import (
    ProviderFault,
    clip,
    default_sleep,
    fault_from_response,
    stream_with_retry,
)
from atlas_harness.model.tokens import count_tokens_estimate

__all__ = [
    "DEFAULT_ANTHROPIC_VERSION",
    "DEFAULT_BASE_URL",
    "FALLBACK_MAX_OUTPUT_TOKENS",
    "AnthropicMessagesAdapter",
]

DEFAULT_BASE_URL = "https://api.anthropic.com/v1"

DEFAULT_ANTHROPIC_VERSION = "2023-06-01"
"""Pinned, not tracking latest: the version header selects a wire contract, and
silently following a newer one would change parsing without a code change."""

FALLBACK_MAX_OUTPUT_TOKENS = 4_096
"""Used only when neither the request nor settings give a limit. The field is
required by the API, so there is no "omit it" branch to fall back to."""

_STOP_REASONS: Mapping[str, StopReason] = {
    "end_turn": StopReason.END_TURN,
    "tool_use": StopReason.TOOL_USE,
    "max_tokens": StopReason.MAX_TOKENS,
    "stop_sequence": StopReason.STOP_SEQUENCE,
    "refusal": StopReason.ERROR,
    "pause_turn": StopReason.END_TURN,
}


def _tool_declaration_payload(tool: Mapping[str, Any]) -> dict[str, Any]:
    """Render a neutral declaration into the Messages tool shape.

    Close to the neutral form already — ``input_schema`` keeps its name — so this
    exists to drop unexpected keys rather than to rename anything.
    """

    return {
        "name": tool.get("name", ""),
        "description": tool.get("description", ""),
        "input_schema": tool.get("input_schema", {"type": "object", "properties": {}}),
    }


def _assistant_content(message: ModelMessage) -> list[dict[str, Any]]:
    """Replay an assistant turn as text plus ``tool_use`` blocks.

    Invalid calls are replayed with an empty input rather than their malformed
    text: ``input`` is a JSON object here, not a string, so there is nowhere to
    put unparseable text. The error itself still reaches the model through the
    matching ``tool_result``, which is what the model needs to correct itself.
    """

    blocks: list[dict[str, Any]] = []
    if message.content:
        blocks.append({"type": "text", "text": message.content})
    for call in message.tool_calls:
        blocks.append(
            {
                "type": "tool_use",
                "id": call.call_id,
                "name": call.name,
                "input": call.arguments if call.valid else {},
            }
        )
    return blocks


def _tool_result_block(message: ModelMessage) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": message.tool_call_id or "",
        "content": message.content,
    }


def _split_system(messages: Sequence[ModelMessage]) -> tuple[str, list[ModelMessage]]:
    """Hoist system text out of the turn sequence.

    Several system turns are joined with a blank line rather than the last one
    winning, because dropping compiled context silently would be worse than a
    slightly longer prompt.
    """

    system_parts = [m.content for m in messages if m.role is Role.SYSTEM and m.content]
    remaining = [m for m in messages if m.role is not Role.SYSTEM]
    return "\n\n".join(system_parts), remaining


def _conversation_payload(messages: Sequence[ModelMessage]) -> list[dict[str, Any]]:
    """Build alternating user/assistant turns, merging consecutive tool results.

    The merge is required, not an optimisation: the API rejects two user messages
    in a row, and a parallel tool batch produces one ``ModelMessage`` per result.
    """

    payload: list[dict[str, Any]] = []
    pending_results: list[dict[str, Any]] = []

    def flush() -> None:
        if pending_results:
            payload.append({"role": "user", "content": list(pending_results)})
            pending_results.clear()

    for message in messages:
        if message.role is Role.TOOL:
            pending_results.append(_tool_result_block(message))
            continue

        flush()
        if message.role is Role.ASSISTANT:
            content = _assistant_content(message)
            if content:
                payload.append({"role": "assistant", "content": content})
            continue
        payload.append({"role": "user", "content": [{"type": "text", "text": message.content}]})

    flush()
    return payload


def _usage_from(payload: Mapping[str, Any] | None) -> TokenUsage | None:
    if not isinstance(payload, Mapping):
        return None
    prompt = payload.get("input_tokens")
    completion = payload.get("output_tokens")
    if not isinstance(prompt, int) and not isinstance(completion, int):
        return None
    return TokenUsage(
        input_tokens=prompt if isinstance(prompt, int) and prompt >= 0 else 0,
        output_tokens=completion if isinstance(completion, int) and completion >= 0 else 0,
    )


class AnthropicMessagesAdapter:
    """Streaming adapter for the native Anthropic ``/messages`` endpoint."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        provider: str = "anthropic",
        anthropic_version: str = DEFAULT_ANTHROPIC_VERSION,
        timeout_seconds: float = 120.0,
        max_retries: int = 2,
        max_output_tokens: int | None = None,
        default_headers: Mapping[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._provider = provider
        self._anthropic_version = anthropic_version
        self._timeout_seconds = timeout_seconds
        self._max_retries = max(max_retries, 0)
        self._max_output_tokens = max_output_tokens
        self._default_headers = dict(default_headers or {})
        self._transport = transport
        self._sleep = sleep or default_sleep
        self._client: httpx.AsyncClient | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> AnthropicMessagesAdapter:
        return cls(
            model=settings.model_name,
            api_key=settings.api_key(),
            base_url=settings.model_base_url or DEFAULT_BASE_URL,
            provider=settings.model_provider,
            anthropic_version=settings.model_anthropic_version,
            timeout_seconds=settings.model_timeout_seconds,
            max_retries=settings.model_max_retries,
            max_output_tokens=settings.model_max_output_tokens,
        )

    async def __aenter__(self) -> AnthropicMessagesAdapter:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Release the pooled connections. Safe to call more than once."""

        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def capabilities(self) -> ModelCapabilities:
        return capabilities_for(self._provider, self._model)

    async def count_tokens(self, value: TokenInput) -> int:
        """Estimate locally.

        The API does offer a real ``/messages/count_tokens``, deliberately not
        used: this runs on the context-budget path before every request, and a
        billed network round trip there would make measuring the budget cost more
        than the work being measured, and could fail where estimating cannot.
        """

        return count_tokens_estimate(value)

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        return self._stream(request)

    async def _stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        if not self._api_key and self._transport is None:
            # Surfaced as an event, not an exception, so the loop records it the
            # same way it records any other failed model call.
            yield ProviderErrorEvent(
                message=(
                    "no model API key configured; set ATLAS_MODEL_API_KEY or use --provider fake"
                ),
                error_code="missing_api_key",
                retryable=False,
            )
            return

        body = self._request_body(request)
        async for event in stream_with_retry(
            lambda: self._attempt(body),
            max_retries=self._max_retries,
            sleep=self._sleep,
        ):
            yield event

    def _request_body(self, request: ModelRequest) -> dict[str, Any]:
        system, turns = _split_system(request.messages)
        body: dict[str, Any] = {
            "model": request.model or self._model,
            "messages": _conversation_payload(turns),
            "max_tokens": (
                request.max_output_tokens or self._max_output_tokens or FALLBACK_MAX_OUTPUT_TOKENS
            ),
            "stream": True,
        }
        if system:
            body["system"] = system
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.stop:
            body["stop_sequences"] = list(request.stop)
        if request.tools:
            body["tools"] = [_tool_declaration_payload(tool) for tool in request.tools]
            body["tool_choice"] = {"type": "auto"}
        return body

    def _headers(self) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "accept": "text/event-stream",
            "anthropic-version": self._anthropic_version,
            **self._default_headers,
        }
        if self._api_key:
            headers["x-api-key"] = self._api_key
        return headers

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(self._timeout_seconds),
                transport=self._transport,
            )
        return self._client

    async def _attempt(self, body: Mapping[str, Any]) -> AsyncIterator[ModelEvent]:
        """One HTTP call. Raises :class:`ProviderFault`; never yields an error."""

        client = self._ensure_client()
        state = _StreamState()
        try:
            async with client.stream(
                "POST", "/messages", json=dict(body), headers=self._headers()
            ) as response:
                if response.status_code >= 400:
                    raise await fault_from_response(response)
                async for line in response.aiter_lines():
                    for event in state.consume(line):
                        yield event
        except httpx.TimeoutException as exc:
            raise ProviderFault(
                f"model request timed out after {self._timeout_seconds}s",
                error_code="provider_timeout",
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderFault(
                f"model request failed: {clip(str(exc))}",
                error_code="provider_transport_error",
                retryable=True,
            ) from exc

        if not state.completed:
            raise ProviderFault(
                "model stream ended without a completion marker",
                error_code="provider_incomplete_stream",
                retryable=True,
            )
        yield state.completion()


class _StreamState:
    """Translate named SSE frames into unified events.

    Only ``data:`` payloads are read. The ``event:`` line names the same type the
    payload already carries, so parsing one field is enough and a reordered or
    absent name line cannot desynchronise the parse.
    """

    def __init__(self) -> None:
        self.completed = False
        self._stop_reason = StopReason.END_TURN
        self._input_tokens = 0
        self._output_tokens = 0
        self._tool_blocks: set[int] = set()

    def consume(self, line: str) -> list[ModelEvent]:
        stripped = line.strip()
        if not stripped or stripped.startswith(":"):
            return []
        if not stripped.startswith("data:"):
            return []

        data = stripped[len("data:") :].strip()
        if not data:
            return []
        try:
            frame = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ProviderFault(
                f"provider sent a malformed stream chunk: {exc.msg}",
                error_code="provider_bad_chunk",
                retryable=False,
            ) from exc
        if not isinstance(frame, Mapping):
            raise ProviderFault(
                f"provider stream chunk is not an object, got {type(frame).__name__}",
                error_code="provider_bad_chunk",
                retryable=False,
            )
        return self._consume_frame(frame)

    def _consume_frame(self, frame: Mapping[str, Any]) -> list[ModelEvent]:
        frame_type = frame.get("type")

        if frame_type == "error":
            error = frame.get("error")
            detail = error.get("message", error) if isinstance(error, Mapping) else error
            raise ProviderFault(
                f"provider reported a stream error: {clip(str(detail))}",
                error_code="provider_stream_error",
                retryable=False,
            )
        if frame_type == "ping":
            return []
        if frame_type == "message_start":
            message = frame.get("message")
            if isinstance(message, Mapping):
                self._absorb_usage(message.get("usage"))
            return []
        if frame_type == "content_block_start":
            return self._block_start(frame)
        if frame_type == "content_block_delta":
            return self._block_delta(frame)
        if frame_type == "content_block_stop":
            return self._block_stop(frame)
        if frame_type == "message_delta":
            delta = frame.get("delta")
            if isinstance(delta, Mapping):
                reason = delta.get("stop_reason")
                if isinstance(reason, str) and reason:
                    self._stop_reason = _STOP_REASONS.get(reason, StopReason.END_TURN)
            self._absorb_usage(frame.get("usage"))
            return []
        if frame_type == "message_stop":
            self.completed = True
        return []

    def _absorb_usage(self, payload: Any) -> None:
        """Accumulate usage across frames.

        Input tokens arrive in ``message_start`` and output tokens in
        ``message_delta``, and the second frame omits the first field. Taking the
        max keeps both instead of letting the later frame zero the earlier one.
        """

        usage = _usage_from(payload if isinstance(payload, Mapping) else None)
        if usage is None:
            return
        self._input_tokens = max(self._input_tokens, usage.input_tokens)
        self._output_tokens = max(self._output_tokens, usage.output_tokens)

    def _block_index(self, frame: Mapping[str, Any]) -> int:
        raw = frame.get("index", 0)
        return raw if isinstance(raw, int) and raw >= 0 else 0

    def _block_start(self, frame: Mapping[str, Any]) -> list[ModelEvent]:
        block = frame.get("content_block")
        if not isinstance(block, Mapping) or block.get("type") != "tool_use":
            # Text and thinking blocks need no announcement; their content
            # arrives as deltas and the assembler creates no slot for them.
            return []

        index = self._block_index(frame)
        self._tool_blocks.add(index)
        call_id = block.get("id")
        name = block.get("name")
        return [
            ToolCallStarted(
                index=index,
                call_id=call_id if isinstance(call_id, str) and call_id else None,
                name=name if isinstance(name, str) and name else None,
            )
        ]

    def _block_delta(self, frame: Mapping[str, Any]) -> list[ModelEvent]:
        delta = frame.get("delta")
        if not isinstance(delta, Mapping):
            return []

        delta_type = delta.get("type")
        if delta_type == "text_delta":
            text = delta.get("text")
            return [TextDelta(text=text)] if isinstance(text, str) and text else []
        if delta_type == "thinking_delta":
            thinking = delta.get("thinking")
            return [ThinkingDelta(text=thinking)] if isinstance(thinking, str) and thinking else []
        if delta_type == "input_json_delta":
            partial = delta.get("partial_json")
            if not isinstance(partial, str) or not partial:
                return []
            return [ToolCallDelta(index=self._block_index(frame), arguments_delta=partial)]
        # signature_delta and any future variant carry nothing the unified events
        # represent; ignoring them keeps an added delta type from being a fault.
        return []

    def _block_stop(self, frame: Mapping[str, Any]) -> list[ModelEvent]:
        index = self._block_index(frame)
        if index not in self._tool_blocks:
            return []
        return [ToolCallCompleted(index=index)]

    def completion(self) -> MessageCompleted:
        stop_reason = self._stop_reason
        if self._tool_blocks and stop_reason is StopReason.END_TURN:
            # A gateway that omits stop_reason while emitting tool_use blocks
            # would otherwise end the loop with the calls unexecuted.
            stop_reason = StopReason.TOOL_USE
        return MessageCompleted(
            stop_reason=stop_reason,
            usage=TokenUsage(
                input_tokens=self._input_tokens,
                output_tokens=self._output_tokens,
            ),
        )
