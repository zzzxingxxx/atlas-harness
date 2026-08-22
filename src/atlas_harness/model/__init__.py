"""Provider-neutral model layer.

``providers`` is deliberately absent from this namespace: each provider module
imports :mod:`atlas_harness.model.catalog`, which registers them lazily. Import
a provider by module path when a test needs one directly
(``from atlas_harness.model.providers.fake import FakeAdapter``).
"""

from atlas_harness.model.assembler import (
    MAX_ARGUMENT_BYTES,
    AssembledResponse,
    StreamAssembler,
)
from atlas_harness.model.catalog import (
    AdapterFactory,
    build_adapter,
    capabilities_for,
    known_models,
    register_provider,
    registered_providers,
    unregister_provider,
)
from atlas_harness.model.protocol import (
    MessageCompleted,
    ModelAdapter,
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
    ToolCallCompleted,
    ToolCallDelta,
    ToolCallStarted,
)
from atlas_harness.model.tokens import count_tokens_estimate

__all__ = [
    "MAX_ARGUMENT_BYTES",
    "AdapterFactory",
    "AssembledResponse",
    "MessageCompleted",
    "ModelAdapter",
    "ModelCapabilities",
    "ModelEvent",
    "ModelMessage",
    "ModelRequest",
    "ModelToolCall",
    "ProviderErrorEvent",
    "Role",
    "StopReason",
    "StreamAssembler",
    "TextDelta",
    "ThinkingDelta",
    "TokenInput",
    "TokenUsage",
    "ToolCallCompleted",
    "ToolCallDelta",
    "ToolCallStarted",
    "build_adapter",
    "capabilities_for",
    "count_tokens_estimate",
    "known_models",
    "register_provider",
    "registered_providers",
    "unregister_provider",
]
