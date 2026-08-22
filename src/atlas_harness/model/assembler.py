"""Fold a stream of :class:`ModelEvent` into one finished assistant message.

Providers emit tool-call arguments as text fragments across many chunks, in an
order nobody guarantees. The assembler is the one place that knows how to put
those fragments back together, so every provider stays a thin translator.

Design rules:

* An assembler instance handles exactly one response. Reusing one is an error.
* A malformed argument payload never raises. It becomes a
  :class:`~atlas_harness.model.protocol.ModelToolCall` with ``valid=False`` so
  the loop can hand the parse failure back to the model as evidence.
* A ``provider_error`` event is recorded, not raised. The caller decides whether
  the attempt is retryable.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from atlas_harness.kernel.ids import new_id
from atlas_harness.model.protocol import (
    MessageCompleted,
    ModelEvent,
    ModelMessage,
    ModelToolCall,
    ProviderErrorEvent,
    StopReason,
    TextDelta,
    ThinkingDelta,
    TokenUsage,
    ToolCallCompleted,
    ToolCallDelta,
    ToolCallStarted,
)

MAX_ARGUMENT_BYTES = 256 * 1024
"""Cap on one tool call's accumulated argument text.

A provider stuck in a repetition loop can stream unbounded JSON. Truncating
turns that into a normal invalid-call result instead of unbounded memory use.
"""


class AssembledResponse(BaseModel):
    """The complete result of one model response."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = ""
    thinking: str = ""
    tool_calls: tuple[ModelToolCall, ...] = ()
    stop_reason: StopReason = StopReason.END_TURN
    usage: TokenUsage = Field(default_factory=TokenUsage)
    error: ProviderErrorEvent | None = None
    completed: bool = False
    """False when the stream ended without a ``message_completed`` event."""

    @property
    def failed(self) -> bool:
        return self.error is not None or not self.completed

    @property
    def valid_tool_calls(self) -> tuple[ModelToolCall, ...]:
        return tuple(call for call in self.tool_calls if call.valid)

    @property
    def invalid_tool_calls(self) -> tuple[ModelToolCall, ...]:
        return tuple(call for call in self.tool_calls if not call.valid)

    def to_message(self) -> ModelMessage:
        """Render as the assistant turn to append to the conversation."""

        return ModelMessage.assistant(self.text, tool_calls=self.tool_calls)

    def summary(self) -> dict[str, Any]:
        """Loggable counts only; no model text is copied."""

        return {
            "text_length": len(self.text),
            "thinking_length": len(self.thinking),
            "tool_call_count": len(self.tool_calls),
            "invalid_tool_call_count": len(self.invalid_tool_calls),
            "stop_reason": self.stop_reason.value,
            "input_tokens": self.usage.input_tokens,
            "output_tokens": self.usage.output_tokens,
            "completed": self.completed,
        }


class _PartialToolCall:
    """Mutable accumulator for one in-flight tool call."""

    __slots__ = ("arguments", "call_id", "index", "name", "truncated")

    def __init__(self, index: int) -> None:
        self.index = index
        self.call_id: str | None = None
        self.name: str | None = None
        self.arguments: list[str] = []
        self.truncated = False

    def add_arguments(self, fragment: str) -> None:
        if not fragment or self.truncated:
            return
        current = sum(len(part) for part in self.arguments)
        room = MAX_ARGUMENT_BYTES - current
        if room <= 0:
            self.truncated = True
            return
        if len(fragment) > room:
            self.arguments.append(fragment[:room])
            self.truncated = True
            return
        self.arguments.append(fragment)

    def raw(self) -> str:
        return "".join(self.arguments)

    def finish(self) -> ModelToolCall:
        raw = self.raw()
        call_id = self.call_id or f"call_{new_id()}"
        name = self.name or ""

        if not name:
            return ModelToolCall(
                call_id=call_id,
                name="",
                raw_arguments=raw,
                valid=False,
                error="tool call has no name",
            )
        if self.truncated:
            return ModelToolCall(
                call_id=call_id,
                name=name,
                raw_arguments=raw,
                valid=False,
                error=f"arguments exceeded {MAX_ARGUMENT_BYTES} bytes",
            )

        arguments, error = _parse_arguments(raw)
        if error is not None:
            return ModelToolCall(
                call_id=call_id,
                name=name,
                raw_arguments=raw,
                valid=False,
                error=error,
            )
        return ModelToolCall(
            call_id=call_id,
            name=name,
            arguments=arguments,
            raw_arguments=raw,
        )


def _parse_arguments(raw: str) -> tuple[dict[str, Any], str | None]:
    """Decode accumulated argument text into a mapping.

    Empty arguments mean "call with no parameters", which is legitimate for
    zero-argument tools, so it is not treated as a failure.
    """

    text = raw.strip()
    if not text:
        return {}, None
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, f"arguments are not valid JSON: {exc.msg} at position {exc.pos}"
    if not isinstance(decoded, dict):
        return {}, f"arguments must be a JSON object, got {type(decoded).__name__}"
    return decoded, None


class StreamAssembler:
    """Accumulate one response's events, then produce an immutable result.

    The assembler tolerates providers that skip ``tool_call_started`` or
    ``tool_call_completed`` and only ever send deltas: any index seen for the
    first time implicitly opens a call, and whatever is still open when the
    stream ends is finished anyway.
    """

    def __init__(self) -> None:
        self._text: list[str] = []
        self._thinking: list[str] = []
        self._calls: dict[int, _PartialToolCall] = {}
        self._order: list[int] = []
        self._stop_reason = StopReason.END_TURN
        self._usage = TokenUsage()
        self._error: ProviderErrorEvent | None = None
        self._completed = False
        self._consumed = False

    @property
    def error(self) -> ProviderErrorEvent | None:
        """The provider failure seen so far, if any."""

        return self._error

    def feed(self, event: ModelEvent) -> None:
        """Apply one stream event."""

        if self._consumed:
            raise RuntimeError("StreamAssembler.finish() was already called")

        match event:
            case TextDelta():
                self._text.append(event.text)
            case ThinkingDelta():
                self._thinking.append(event.text)
            case ToolCallStarted():
                call = self._slot(event.index)
                if event.call_id is not None:
                    call.call_id = event.call_id
                if event.name is not None:
                    call.name = event.name
            case ToolCallDelta():
                call = self._slot(event.index)
                if event.call_id is not None:
                    call.call_id = event.call_id
                if event.name is not None:
                    call.name = event.name
                call.add_arguments(event.arguments_delta)
            case ToolCallCompleted():
                # Nothing to close eagerly; finish() renders every slot. The
                # event still matters because it forces the slot to exist for
                # providers that emit a bare completion.
                self._slot(event.index)
            case MessageCompleted():
                self._stop_reason = event.stop_reason
                self._usage = event.usage
                self._completed = True
            case ProviderErrorEvent():
                self._error = event
                self._stop_reason = StopReason.ERROR

    def feed_all(self, events: list[ModelEvent]) -> None:
        for event in events:
            self.feed(event)

    def finish(self) -> AssembledResponse:
        """Render the accumulated state. Callable once per assembler."""

        if self._consumed:
            raise RuntimeError("StreamAssembler.finish() was already called")
        self._consumed = True

        tool_calls = tuple(self._calls[index].finish() for index in self._order)
        stop_reason = self._stop_reason
        if self._error is None and tool_calls and stop_reason is StopReason.END_TURN:
            # Some providers report end_turn even while requesting tools; the
            # presence of calls is the more reliable signal.
            stop_reason = StopReason.TOOL_USE

        return AssembledResponse(
            text="".join(self._text),
            thinking="".join(self._thinking),
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            usage=self._usage,
            error=self._error,
            completed=self._completed,
        )

    def _slot(self, index: int) -> _PartialToolCall:
        call = self._calls.get(index)
        if call is None:
            call = _PartialToolCall(index)
            self._calls[index] = call
            self._order.append(index)
        return call
