"""A scripted in-process adapter.

This is the provider the test suite and ``--provider fake`` use to exercise the
whole model -> tool -> result -> model loop with no network and no API key. It
has two modes:

* **scripted** — a list of turns, one per :meth:`FakeAdapter.stream` call. When
  the script runs out the adapter raises, because a loop that asked for more
  turns than the test wrote is a bug in the test, not a provider fault.
* **canned** — no script at all; every call answers with the same short text.
  This is what the CLI gets from settings, so a fresh checkout can run the
  end-to-end flow before any credentials exist.

Every request is recorded in :attr:`FakeAdapter.requests` so tests can assert
what the loop actually sent: the tool declarations, and the tool results fed
back on the next turn.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence

from atlas_harness.config import Settings
from atlas_harness.model.catalog import capabilities_for
from atlas_harness.model.protocol import (
    MessageCompleted,
    ModelCapabilities,
    ModelEvent,
    ModelRequest,
    ProviderErrorEvent,
    StopReason,
    TextDelta,
    ThinkingDelta,
    TokenInput,
    TokenUsage,
    ToolCallCompleted,
    ToolCallDelta,
    ToolCallStarted,
)
from atlas_harness.model.tokens import count_tokens_estimate

CANNED_TEXT = "fake provider: no script configured"


def text_turn(
    text: str,
    *,
    thinking: str = "",
    chunk_size: int = 8,
    usage: TokenUsage | None = None,
    stop_reason: StopReason = StopReason.END_TURN,
) -> list[ModelEvent]:
    """Build a plain-text turn, split into deltas like a real stream."""

    events: list[ModelEvent] = []
    if thinking:
        events.append(ThinkingDelta(text=thinking))
    for start in range(0, len(text), max(chunk_size, 1)):
        events.append(TextDelta(text=text[start : start + max(chunk_size, 1)]))
    events.append(MessageCompleted(stop_reason=stop_reason, usage=usage or TokenUsage()))
    return events


def tool_call_turn(
    name: str,
    arguments: dict[str, object],
    *,
    call_id: str = "call_fake_1",
    index: int = 0,
    text: str = "",
    chunk_size: int = 8,
    usage: TokenUsage | None = None,
) -> list[ModelEvent]:
    """Build a turn that requests one tool, streaming its arguments."""

    events: list[ModelEvent] = []
    for start in range(0, len(text), max(chunk_size, 1)):
        events.append(TextDelta(text=text[start : start + max(chunk_size, 1)]))

    events.append(ToolCallStarted(index=index, call_id=call_id, name=name))
    raw = json.dumps(arguments, sort_keys=True)
    for start in range(0, len(raw), max(chunk_size, 1)):
        events.append(
            ToolCallDelta(index=index, arguments_delta=raw[start : start + max(chunk_size, 1)])
        )
    events.append(ToolCallCompleted(index=index))
    events.append(MessageCompleted(stop_reason=StopReason.TOOL_USE, usage=usage or TokenUsage()))
    return events


def malformed_tool_call_turn(
    name: str,
    raw_arguments: str,
    *,
    call_id: str = "call_fake_bad",
    index: int = 0,
) -> list[ModelEvent]:
    """Build a turn whose tool arguments are not valid JSON."""

    return [
        ToolCallStarted(index=index, call_id=call_id, name=name),
        ToolCallDelta(index=index, arguments_delta=raw_arguments),
        ToolCallCompleted(index=index),
        MessageCompleted(stop_reason=StopReason.TOOL_USE),
    ]


def error_turn(
    message: str,
    *,
    retryable: bool = True,
    error_code: str = "provider_error",
    status_code: int | None = None,
    attempt: int = 1,
) -> list[ModelEvent]:
    """Build a turn that fails mid-stream."""

    return [
        ProviderErrorEvent(
            message=message,
            error_code=error_code,
            status_code=status_code,
            retryable=retryable,
            attempt=attempt,
        )
    ]


def truncated_turn(text: str = "partial") -> list[ModelEvent]:
    """A stream that stops without ``message_completed``."""

    return [TextDelta(text=text)]


class FakeAdapter:
    """Deterministic adapter driven by a pre-written script."""

    def __init__(
        self,
        script: Sequence[Sequence[ModelEvent]] | None = None,
        *,
        model: str = "fake-model",
        provider: str = "fake",
        canned_text: str = CANNED_TEXT,
    ) -> None:
        self._script: list[list[ModelEvent]] | None = (
            None if script is None else [list(turn) for turn in script]
        )
        self._model = model
        self._provider = provider
        self._canned_text = canned_text
        self.requests: list[ModelRequest] = []
        """Every request received, in order, for test assertions."""

    @classmethod
    def from_settings(cls, settings: Settings) -> FakeAdapter:
        return cls(model=settings.model_name)

    @property
    def calls(self) -> int:
        return len(self.requests)

    @property
    def remaining_turns(self) -> int:
        """Turns left in the script; ``-1`` in canned mode, which never runs out."""

        return -1 if self._script is None else len(self._script)

    def last_request(self) -> ModelRequest | None:
        return self.requests[-1] if self.requests else None

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        return self._stream(self._next_turn())

    async def _stream(self, events: list[ModelEvent]) -> AsyncIterator[ModelEvent]:
        for event in events:
            yield event

    def _next_turn(self) -> list[ModelEvent]:
        if self._script is None:
            return text_turn(self._canned_text)
        if not self._script:
            raise RuntimeError(
                f"FakeAdapter script exhausted after {self.calls} request(s); "
                "the loop asked for another turn than the script provides"
            )
        return self._script.pop(0)

    async def count_tokens(self, value: TokenInput) -> int:
        return count_tokens_estimate(value)

    def capabilities(self) -> ModelCapabilities:
        return capabilities_for(self._provider, self._model)
