"""Adapter for any provider speaking the OpenAI ``/chat/completions`` dialect.

This covers OpenAI itself plus the many gateways that copy its wire format
(DeepSeek, vLLM, Ollama's compatible endpoint, most cloud proxies). Only the
streaming shape is implemented, because the whole loop is built on incremental
events.

Two rules shape the error handling:

* :meth:`OpenAICompatibleAdapter.stream` never raises for a provider fault. Every
  failure becomes a :class:`ProviderErrorEvent` so the agent loop has exactly one
  code path for "the model did not answer".
* A retry only happens *before* the first event reaches the caller. Once a text
  delta has been handed downstream, replaying the request would duplicate that
  text, so a mid-stream break is reported instead of retried.

Network note: the endpoint comes from operator configuration
(``ATLAS_MODEL_BASE_URL``), never from model output, so it is trusted egress and
is not subject to the tool-level network policy. The API key is read from
settings at call time and is never logged, echoed into an error, or persisted to
an event.
"""

from __future__ import annotations

import asyncio
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
    ModelToolCall,
    ProviderErrorEvent,
    Role,
    StopReason,
    TextDelta,
    ThinkingDelta,
    TokenInput,
    TokenUsage,
    ToolCallDelta,
    ToolCallStarted,
)
from atlas_harness.model.tokens import count_tokens_estimate

DEFAULT_BASE_URL = "https://api.openai.com/v1"

RETRYABLE_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
"""Statuses worth a second attempt: overload, throttling, transient gateways."""

_MAX_ERROR_CHARS = 400
"""Provider error bodies can be long HTML pages; only the head is useful."""

_BACKOFF_BASE_SECONDS = 0.5
_BACKOFF_CAP_SECONDS = 8.0

_FINISH_REASONS: Mapping[str, StopReason] = {
    "stop": StopReason.END_TURN,
    "tool_calls": StopReason.TOOL_USE,
    "function_call": StopReason.TOOL_USE,
    "length": StopReason.MAX_TOKENS,
    "max_tokens": StopReason.MAX_TOKENS,
    "content_filter": StopReason.ERROR,
}


class _ProviderFault(Exception):
    """Internal signal carrying everything one failure needs to report."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        retryable: bool,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.retryable = retryable
        self.status_code = status_code


def _clip(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= _MAX_ERROR_CHARS:
        return collapsed
    return f"{collapsed[:_MAX_ERROR_CHARS]}... (truncated)"


def _tool_calls_payload(calls: Sequence[ModelToolCall]) -> list[dict[str, Any]]:
    """Render prior tool requests back into wire format.

    Invalid calls are replayed with their original ``raw_arguments`` so the model
    sees the exact malformed text it produced next to the error it caused.
    """

    payload: list[dict[str, Any]] = []
    for call in calls:
        arguments = call.raw_arguments or json.dumps(call.arguments, sort_keys=True)
        payload.append(
            {
                "id": call.call_id,
                "type": "function",
                "function": {"name": call.name, "arguments": arguments},
            }
        )
    return payload


def _message_payload(message: ModelMessage) -> dict[str, Any]:
    if message.role is Role.TOOL:
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id or "",
            "content": message.content,
        }

    body: dict[str, Any] = {"role": message.role.value, "content": message.content}
    if message.name is not None:
        body["name"] = message.name
    if message.tool_calls:
        body["tool_calls"] = _tool_calls_payload(message.tool_calls)
        if not message.content:
            # An assistant turn that only requests tools must send null, not "",
            # or strict gateways reject the message.
            body["content"] = None
    return body


def _usage_from(payload: Mapping[str, Any] | None) -> TokenUsage | None:
    if not isinstance(payload, Mapping):
        return None
    prompt = payload.get("prompt_tokens")
    completion = payload.get("completion_tokens")
    return TokenUsage(
        input_tokens=prompt if isinstance(prompt, int) and prompt >= 0 else 0,
        output_tokens=completion if isinstance(completion, int) and completion >= 0 else 0,
    )


class OpenAICompatibleAdapter:
    """Streaming adapter for OpenAI-dialect chat completion endpoints."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        provider: str = "openai",
        timeout_seconds: float = 120.0,
        max_retries: int = 2,
        max_output_tokens: int | None = None,
        include_usage: bool = True,
        default_headers: Mapping[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._provider = provider
        self._timeout_seconds = timeout_seconds
        self._max_retries = max(max_retries, 0)
        self._max_output_tokens = max_output_tokens
        self._include_usage = include_usage
        self._default_headers = dict(default_headers or {})
        self._transport = transport
        self._sleep = sleep or _default_sleep
        self._client: httpx.AsyncClient | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> OpenAICompatibleAdapter:
        return cls(
            model=settings.model_name,
            api_key=settings.api_key(),
            base_url=settings.model_base_url or DEFAULT_BASE_URL,
            provider=settings.model_provider,
            timeout_seconds=settings.model_timeout_seconds,
            max_retries=settings.model_max_retries,
            max_output_tokens=settings.model_max_output_tokens,
        )

    async def __aenter__(self) -> OpenAICompatibleAdapter:
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
        """Estimate locally; the chat-completions dialect has no count endpoint."""

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
        attempt = 1
        while True:
            emitted = False
            try:
                async for event in self._attempt(body):
                    emitted = True
                    yield event
                return
            except _ProviderFault as fault:
                exhausted = attempt > self._max_retries
                if emitted or not fault.retryable or exhausted:
                    yield ProviderErrorEvent(
                        message=fault.message,
                        error_code=fault.error_code,
                        status_code=fault.status_code,
                        # Partial output already went downstream, so replaying
                        # would duplicate it: report rather than retry.
                        retryable=fault.retryable and not emitted,
                        attempt=attempt,
                    )
                    return
                await self._sleep(_backoff_seconds(attempt))
                attempt += 1

    def _request_body(self, request: ModelRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": request.model or self._model,
            "messages": [_message_payload(message) for message in request.messages],
            "stream": True,
        }
        max_output = request.max_output_tokens or self._max_output_tokens
        if max_output is not None:
            body["max_tokens"] = max_output
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.stop:
            body["stop"] = list(request.stop)
        if request.tools:
            body["tools"] = list(request.tools)
            body["tool_choice"] = "auto"
        if self._include_usage:
            body["stream_options"] = {"include_usage": True}
        return body

    def _headers(self) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "accept": "text/event-stream",
            **self._default_headers,
        }
        if self._api_key:
            headers["authorization"] = f"Bearer {self._api_key}"
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
        """One HTTP call. Raises :class:`_ProviderFault`; never yields an error."""

        client = self._ensure_client()
        state = _StreamState()
        try:
            async with client.stream(
                "POST", "/chat/completions", json=dict(body), headers=self._headers()
            ) as response:
                if response.status_code >= 400:
                    raise await _fault_from_response(response)
                async for line in response.aiter_lines():
                    for event in state.consume(line):
                        yield event
        except httpx.TimeoutException as exc:
            raise _ProviderFault(
                f"model request timed out after {self._timeout_seconds}s",
                error_code="provider_timeout",
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise _ProviderFault(
                f"model request failed: {_clip(str(exc))}",
                error_code="provider_transport_error",
                retryable=True,
            ) from exc

        if not state.completed:
            raise _ProviderFault(
                "model stream ended without a completion marker",
                error_code="provider_incomplete_stream",
                retryable=True,
            )
        yield state.completion()


async def _fault_from_response(response: httpx.Response) -> _ProviderFault:
    """Build a fault from an error response, reading the body for context."""

    raw = await response.aread()
    detail = ""
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        detail = _clip(raw.decode("utf-8", errors="replace"))
    else:
        if isinstance(decoded, Mapping):
            error = decoded.get("error")
            if isinstance(error, Mapping):
                detail = _clip(str(error.get("message", "")))
            elif isinstance(error, str):
                detail = _clip(error)
        if not detail:
            detail = _clip(json.dumps(decoded)[:_MAX_ERROR_CHARS])

    status = response.status_code
    suffix = f": {detail}" if detail else ""
    return _ProviderFault(
        f"provider returned HTTP {status}{suffix}",
        error_code=f"provider_http_{status}",
        retryable=status in RETRYABLE_STATUS_CODES,
        status_code=status,
    )


class _StreamState:
    """Translate SSE lines into unified events, tracking what has been announced."""

    def __init__(self) -> None:
        self.completed = False
        self._stop_reason = StopReason.END_TURN
        self._usage: TokenUsage | None = None
        self._announced: set[int] = set()

    def consume(self, line: str) -> list[ModelEvent]:
        """Handle one SSE line, returning any events it produced."""

        stripped = line.strip()
        if not stripped or stripped.startswith(":"):
            return []
        if not stripped.startswith("data:"):
            # Field names other than `data` (event, id, retry) carry nothing the
            # chat-completions dialect needs.
            return []

        data = stripped[len("data:") :].strip()
        if data == "[DONE]":
            self.completed = True
            return []
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError as exc:
            raise _ProviderFault(
                f"provider sent a malformed stream chunk: {exc.msg}",
                error_code="provider_bad_chunk",
                retryable=False,
            ) from exc
        if not isinstance(chunk, Mapping):
            raise _ProviderFault(
                f"provider stream chunk is not an object, got {type(chunk).__name__}",
                error_code="provider_bad_chunk",
                retryable=False,
            )
        return self._consume_chunk(chunk)

    def _consume_chunk(self, chunk: Mapping[str, Any]) -> list[ModelEvent]:
        usage = _usage_from(chunk.get("usage"))
        if usage is not None:
            self._usage = usage

        error = chunk.get("error")
        if isinstance(error, Mapping):
            # Some gateways report a mid-stream failure inside a data frame
            # rather than by breaking the connection.
            raise _ProviderFault(
                f"provider reported a stream error: {_clip(str(error.get('message', error)))}",
                error_code="provider_stream_error",
                retryable=False,
            )

        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            return []
        choice = choices[0]
        if not isinstance(choice, Mapping):
            return []

        events: list[ModelEvent] = []
        delta = choice.get("delta")
        if isinstance(delta, Mapping):
            events.extend(self._consume_delta(delta))

        finish_reason = choice.get("finish_reason")
        if isinstance(finish_reason, str) and finish_reason:
            self._stop_reason = _FINISH_REASONS.get(finish_reason, StopReason.END_TURN)
            self.completed = True
        return events

    def _consume_delta(self, delta: Mapping[str, Any]) -> list[ModelEvent]:
        events: list[ModelEvent] = []

        content = delta.get("content")
        if isinstance(content, str) and content:
            events.append(TextDelta(text=content))

        # DeepSeek and several gateways expose chain-of-thought under one of
        # these keys; both map onto the same unified event.
        for key in ("reasoning_content", "reasoning"):
            thinking = delta.get(key)
            if isinstance(thinking, str) and thinking:
                events.append(ThinkingDelta(text=thinking))
                break

        tool_calls = delta.get("tool_calls")
        if isinstance(tool_calls, list):
            for entry in tool_calls:
                if isinstance(entry, Mapping):
                    events.extend(self._consume_tool_call(entry))
        return events

    def _consume_tool_call(self, entry: Mapping[str, Any]) -> list[ModelEvent]:
        raw_index = entry.get("index", 0)
        index = raw_index if isinstance(raw_index, int) and raw_index >= 0 else 0

        call_id = entry.get("id")
        call_id = call_id if isinstance(call_id, str) and call_id else None

        function = entry.get("function")
        name: str | None = None
        arguments: str | None = None
        if isinstance(function, Mapping):
            raw_name = function.get("name")
            if isinstance(raw_name, str) and raw_name:
                name = raw_name
            raw_arguments = function.get("arguments")
            if isinstance(raw_arguments, str):
                arguments = raw_arguments

        events: list[ModelEvent] = []
        if index not in self._announced:
            self._announced.add(index)
            events.append(ToolCallStarted(index=index, call_id=call_id, name=name))
        if arguments or call_id or name:
            events.append(
                ToolCallDelta(
                    index=index,
                    arguments_delta=arguments or "",
                    call_id=call_id,
                    name=name,
                )
            )
        return events

    def completion(self) -> MessageCompleted:
        stop_reason = self._stop_reason
        if self._announced and stop_reason is StopReason.END_TURN:
            stop_reason = StopReason.TOOL_USE
        return MessageCompleted(stop_reason=stop_reason, usage=self._usage or TokenUsage())


def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff, capped. Deterministic so tests can assert the waits."""

    growth = float(2 ** (attempt - 1))
    return min(_BACKOFF_BASE_SECONDS * growth, _BACKOFF_CAP_SECONDS)


async def _default_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)
