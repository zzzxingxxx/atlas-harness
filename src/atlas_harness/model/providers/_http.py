"""Shared HTTP plumbing for streaming provider adapters.

Every provider that speaks HTTP has to answer the same three questions the same
way, so the answers live here rather than once per dialect:

* **When is a failure worth retrying?** Only transient statuses, and only
  *before* the first event has been handed downstream. Once a text delta has
  reached the caller, replaying the request would duplicate that text, so a
  mid-stream break is reported instead of retried. :func:`stream_with_retry`
  owns that rule; an adapter cannot get it subtly wrong on its own.
* **How much of an error body is useful?** The head. Gateways answer with HTML
  pages and stack traces, and the whole point of the message is to fit in one
  log line.
* **How long to wait between attempts?** Deterministic exponential backoff, so
  a test can assert the waits instead of tolerating them.

Nothing here is dialect-specific. Reading a provider's error *shape* out of a
response body is, so :func:`fault_from_response` takes an extractor.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any

import httpx

from atlas_harness.model.protocol import ModelEvent, ProviderErrorEvent

RETRYABLE_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})
"""Statuses worth a second attempt: overload, throttling, transient gateways.

529 is not in any RFC; it is what Anthropic returns for ``overloaded_error``, and
it is the single most common transient failure that dialect produces.
"""

MAX_ERROR_CHARS = 400
"""Provider error bodies can be long HTML pages; only the head is useful."""

BACKOFF_BASE_SECONDS = 0.5
BACKOFF_CAP_SECONDS = 8.0


class ProviderFault(Exception):
    """Internal signal carrying everything one failure needs to report.

    Raised inside an attempt and converted to a :class:`ProviderErrorEvent` by
    :func:`stream_with_retry`. It never escapes to the agent loop, which has
    exactly one code path for "the model did not answer".
    """

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


def clip(text: str) -> str:
    """Collapse whitespace and cap length, for a message that fits one log line."""

    collapsed = " ".join(text.split())
    if len(collapsed) <= MAX_ERROR_CHARS:
        return collapsed
    return f"{collapsed[:MAX_ERROR_CHARS]}... (truncated)"


def backoff_seconds(attempt: int) -> float:
    """Exponential backoff, capped. Deterministic so tests can assert the waits."""

    growth = float(2 ** (attempt - 1))
    return min(BACKOFF_BASE_SECONDS * growth, BACKOFF_CAP_SECONDS)


async def default_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def detail_from_error_mapping(decoded: Mapping[str, Any]) -> str:
    """Pull a human message out of the ``{"error": {"message": ...}}`` shape.

    Both dialects this runtime speaks use it, and a gateway that invents its own
    shape still gets the JSON head via the caller's fallback.
    """

    error = decoded.get("error")
    if isinstance(error, Mapping):
        message = error.get("message")
        if isinstance(message, str) and message:
            return clip(message)
    elif isinstance(error, str) and error:
        return clip(error)
    return ""


async def fault_from_response(
    response: httpx.Response,
    *,
    extract_detail: Callable[[Mapping[str, Any]], str] = detail_from_error_mapping,
) -> ProviderFault:
    """Build a fault from an error response, reading the body for context.

    The body is read rather than streamed: an error response is small enough to
    hold, and a message without the provider's own explanation of the refusal
    forces the operator to reproduce it by hand.
    """

    raw = await response.aread()
    detail = ""
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        detail = clip(raw.decode("utf-8", errors="replace"))
    else:
        if isinstance(decoded, Mapping):
            detail = extract_detail(decoded)
        if not detail:
            detail = clip(json.dumps(decoded)[:MAX_ERROR_CHARS])

    status = response.status_code
    suffix = f": {detail}" if detail else ""
    return ProviderFault(
        f"provider returned HTTP {status}{suffix}",
        error_code=f"provider_http_{status}",
        retryable=status in RETRYABLE_STATUS_CODES,
        status_code=status,
    )


async def stream_with_retry(
    attempt_factory: Callable[[], AsyncIterator[ModelEvent]],
    *,
    max_retries: int,
    sleep: Callable[[float], Awaitable[None]],
) -> AsyncIterator[ModelEvent]:
    """Run attempts until one succeeds, the retries run out, or output escapes.

    ``attempt_factory`` must return a fresh iterator per call and raise
    :class:`ProviderFault` for any provider failure. The fault is reported as a
    single :class:`ProviderErrorEvent`, never raised, so the caller has one code
    path for a failed model call.

    The ``emitted`` flag is the load-bearing part: once an event has been
    yielded, ``retryable`` is forced false in the reported event even when the
    status says otherwise, because a caller that retried would receive the
    already-delivered text a second time.
    """

    attempt = 1
    while True:
        emitted = False
        try:
            async for event in attempt_factory():
                emitted = True
                yield event
            return
        except ProviderFault as fault:
            exhausted = attempt > max_retries
            if emitted or not fault.retryable or exhausted:
                yield ProviderErrorEvent(
                    message=fault.message,
                    error_code=fault.error_code,
                    status_code=fault.status_code,
                    retryable=fault.retryable and not emitted,
                    attempt=attempt,
                )
                return
            await sleep(backoff_seconds(attempt))
            attempt += 1
