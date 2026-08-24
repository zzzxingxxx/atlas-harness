"""One real round trip against the configured provider, reported as a verdict.

Every other test in this repository drives a provider through an injected
transport, which proves the adapter parses what the dialect is documented to
send. It cannot prove the endpoint an operator configured actually speaks that
dialect: a wrong base URL, an expired key, a gateway that answers ``200`` with
an HTML error page and a model name the account cannot reach all look identical
from offline tests. This module is the check that closes that gap, so it is the
one place that is *allowed* to need the network -- and it is never part of the
offline test gate.

Two rules shape what comes back:

*The key never appears in the report.* The probe knows whether a key was
configured and nothing else about it. A verdict is printed by operators and
pasted into tickets, so a report carrying the credential it authenticated with
would be the leak the rest of the runtime works to prevent.

*A provider fault is a verdict, not an exception.* The adapter already turns
every fault into a ``ProviderErrorEvent``; the probe reports that event's code
so an operator gets ``provider_http_401`` rather than a traceback, and so the
exit code can be chosen from the fault instead of from whether Python raised.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from atlas_harness.config import Settings
from atlas_harness.model.assembler import StreamAssembler
from atlas_harness.model.catalog import build_adapter
from atlas_harness.model.protocol import (
    ModelAdapter,
    ModelMessage,
    ModelRequest,
    Role,
    StopReason,
    TokenUsage,
)
from atlas_harness.tools.redaction import REDACTED, redact

DEFAULT_PROMPT = "Reply with the single word: ready."
"""Short on purpose. The probe is billed to whoever runs it, and a longer prompt
would buy no more confidence: what is being tested is that a request reaches the
endpoint and a parsable stream comes back."""

PROBE_MAX_OUTPUT_TOKENS = 32


class ProbeReport(BaseModel):
    """What one round trip proved, in a form safe to print and to paste."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    model: str
    base_url: str
    api_key_configured: bool
    """Whether a key was present. Never the key, and never a prefix of it."""

    ok: bool
    duration_ms: int
    stop_reason: StopReason = StopReason.END_TURN
    completed: bool = False
    text_chars: int = Field(default=0, ge=0)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    error_code: str | None = None
    error: str | None = None
    retryable: bool = False
    status_code: int | None = None

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def render(self) -> list[str]:
        lines = [
            f"provider: {self.provider}/{self.model}",
            f"endpoint: {self.base_url}",
            f"api_key: {'configured' if self.api_key_configured else 'missing'}",
            f"latency: {self.duration_ms}ms",
        ]
        if self.ok:
            lines.append(
                f"stream: {self.text_chars} chars, stop={self.stop_reason.value}, "
                f"tokens={self.usage.total_tokens}"
            )
        else:
            lines.append(f"error: {self.error_code}: {self.error}")
            if self.status_code is not None:
                lines.append(f"status: {self.status_code}")
            lines.append(f"retryable: {self.retryable}")
        lines.append(f"verdict: {'ok' if self.ok else 'failed'}")
        return lines


def probe_request(settings: Settings, *, prompt: str = DEFAULT_PROMPT) -> ModelRequest:
    """The smallest request that still exercises the whole request path.

    No tools are declared. Tool schemas are the part of a request most likely to
    be rejected for provider-specific reasons, and a probe that failed on one
    would report a broken endpoint when the endpoint was fine.
    """

    return ModelRequest(
        model=settings.model_name,
        messages=(ModelMessage(role=Role.USER, content=prompt),),
        max_output_tokens=PROBE_MAX_OUTPUT_TOKENS,
    )


def scrub(message: str, api_key: str | None) -> str:
    """Redact by shape, then remove the one credential we know by value.

    ``redact`` matches credential *shapes*, which is all it can do for text
    arriving from anywhere. Here we also hold the exact key that was sent, and a
    gateway is free to quote it back in a shape no pattern covers -- a bare opaque
    string, a self-issued token, a vendor prefix nobody has written a rule for.
    Removing it by identity closes that gap for the one value whose exposure this
    command would be responsible for.
    """

    scrubbed = redact(message)
    if api_key:
        scrubbed = scrubbed.replace(api_key, REDACTED)
    return scrubbed


async def probe_adapter(
    adapter: ModelAdapter,
    request: ModelRequest,
    *,
    provider: str,
    base_url: str,
    api_key: str | None,
) -> ProbeReport:
    """Drive one stream to its end and fold it into a verdict.

    The stream is consumed through the same assembler the agent loop uses, so a
    passing probe means the loop would have been able to read this response too --
    which is the claim an operator needs, rather than "some bytes arrived".

    The key is taken by value rather than as a "was one configured" flag so that
    an endpoint echoing it back cannot put it in the report; see :func:`scrub`.
    """

    assembler = StreamAssembler()
    started = time.monotonic()
    async for event in adapter.stream(request):
        assembler.feed(event)
    response = assembler.finish()
    duration_ms = int((time.monotonic() - started) * 1000)

    fault = response.error
    return ProbeReport(
        provider=provider,
        model=request.model,
        base_url=base_url,
        api_key_configured=api_key is not None,
        # A completed stream that carries a fault is still a failure: the loop
        # would have recorded a provider error and produced no answer.
        ok=fault is None and response.completed,
        duration_ms=duration_ms,
        stop_reason=response.stop_reason,
        completed=response.completed,
        text_chars=len(response.text),
        usage=response.usage,
        error_code=None if fault is None else fault.error_code,
        # Scrubbed because the message may quote the provider's response body,
        # which is outside this codebase's control.
        error=None if fault is None else scrub(fault.message, api_key),
        retryable=False if fault is None else fault.retryable,
        status_code=None if fault is None else fault.status_code,
    )


def probe(
    settings: Settings,
    *,
    provider: str | None = None,
    prompt: str = DEFAULT_PROMPT,
) -> ProbeReport:
    """Build the configured adapter, send one request, and close it again.

    Blocking, because both callers -- the CLI and the opt-in live test -- are
    synchronous, and the adapter owns an HTTP client that has to be closed on the
    same loop that opened it.
    """

    name = provider or settings.model_provider
    adapter = build_adapter(settings, provider=name)
    request = probe_request(settings, prompt=prompt)
    base_url = settings.model_base_url or "provider default"

    async def _run() -> ProbeReport:
        try:
            return await probe_adapter(
                adapter,
                request,
                provider=name,
                base_url=base_url,
                api_key=settings.api_key(),
            )
        finally:
            closer = getattr(adapter, "aclose", None)
            if closer is not None:
                await closer()

    return asyncio.run(_run())
