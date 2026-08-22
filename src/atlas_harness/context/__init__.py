"""Context compilation, token budgets and structured compaction.

The layer between "what happened" (the event log) and "what the model sees" (the
prompt). Everything here treats the context as a cache of the log: it can be
rebuilt, trimmed or replaced, and none of that costs an event, a diff or an
artifact.
"""

from atlas_harness.context.artifacts import (
    ARTIFACTS_DIRNAME,
    DEFAULT_INLINE_LIMIT,
    PREVIEW_BYTES,
    ArtifactRef,
    ArtifactStore,
)
from atlas_harness.context.compaction import (
    MAX_ITEM_CHARS,
    MAX_SUMMARY_ITEMS,
    REASON_MANUAL,
    REASON_OVERFLOW,
    REASON_THRESHOLD,
    CompactionResult,
    CompactionSummary,
    Compactor,
    compaction_reason_for,
    summary_from_event,
)
from atlas_harness.context.compiler import (
    SLOT_ORDER,
    TRIM_ORDER,
    CompiledContext,
    ContextCompiler,
    ContextItem,
    DroppedItem,
    Slot,
    fixed_items,
    short_term_items,
)
from atlas_harness.context.tokens import (
    COMPACT_RATIO,
    FORCE_RATIO,
    PREPARE_RATIO,
    AdapterCounter,
    ContextBudget,
    ContextPressure,
    EstimatingCounter,
    TokenCounter,
)

__all__ = [
    "ARTIFACTS_DIRNAME",
    "COMPACT_RATIO",
    "DEFAULT_INLINE_LIMIT",
    "FORCE_RATIO",
    "MAX_ITEM_CHARS",
    "MAX_SUMMARY_ITEMS",
    "PREPARE_RATIO",
    "PREVIEW_BYTES",
    "REASON_MANUAL",
    "REASON_OVERFLOW",
    "REASON_THRESHOLD",
    "SLOT_ORDER",
    "TRIM_ORDER",
    "AdapterCounter",
    "ArtifactRef",
    "ArtifactStore",
    "CompactionResult",
    "CompactionSummary",
    "Compactor",
    "CompiledContext",
    "ContextBudget",
    "ContextCompiler",
    "ContextItem",
    "ContextPressure",
    "DroppedItem",
    "EstimatingCounter",
    "Slot",
    "TokenCounter",
    "compaction_reason_for",
    "fixed_items",
    "short_term_items",
    "summary_from_event",
]
