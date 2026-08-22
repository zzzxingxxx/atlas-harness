"""Provider-independent token estimation.

Real tokenizers differ per model and most OpenAI-compatible gateways expose no
counting endpoint. The loop only needs a deterministic, roughly proportional
number to decide when a conversation is nearing its context window, so a
character-based estimate is enough. It is never used for billing.
"""

from __future__ import annotations

import json

from atlas_harness.model.protocol import TokenInput

CHARS_PER_TOKEN = 4
"""Coarse but stable divisor. The value only has to stay constant across runs."""

MESSAGE_OVERHEAD_TOKENS = 4
"""Per-message framing every chat API adds around role and delimiters."""


def count_tokens_estimate(value: TokenInput) -> int:
    """Estimate the token cost of messages, tool declarations and loose text.

    ``sort_keys`` keeps JSON-encoded tool schemas byte-stable so the same input
    always produces the same count.
    """

    characters = 0
    tokens = 0

    for message in value.messages:
        tokens += MESSAGE_OVERHEAD_TOKENS
        characters += len(message.role.value) + len(message.content)
        for call in message.tool_calls:
            arguments = call.raw_arguments or json.dumps(call.arguments, sort_keys=True)
            characters += len(call.name) + len(arguments)

    for tool in value.tools:
        characters += len(json.dumps(tool, sort_keys=True))

    if value.text:
        characters += len(value.text)

    return tokens + (characters + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN
