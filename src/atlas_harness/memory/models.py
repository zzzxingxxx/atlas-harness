"""The four memory layers and the provenance every record carries.

The plan asks for memory that is retrievable, auditable and expirable. Those
three words decide the whole shape of :class:`MemoryRecord`: retrievable means the
content is indexed text, auditable means every record names the task that produced
it and the evidence behind it, and expirable means the record itself states when it
stops being true rather than leaving a reader to judge by its age.

The layers differ in exactly one respect that matters here — how long a record is
allowed to speak for:

============  ==========================================  ================
layer         content                                     default lifetime
============  ==========================================  ================
``working``   the current task's scratch state             one hour
``episodic``  what happened during a specific task         14 days
``semantic``  facts about the project                      no expiry
``procedural`` how to carry out a recurring task           no expiry
============  ==========================================  ================

``working`` and ``episodic`` are observations: they were true of one run and may
not be true now. ``semantic`` and ``procedural`` are the two layers a prompt may
present as standing fact, which is why :data:`LONG_TERM_LAYERS` exists separately
from the layer enum. An expired episodic record is not merely low-ranked — it is
ineligible, because a stale observation read as a durable fact is worse than
having no memory at all.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from atlas_harness.events.models import MEMORY_LAYERS
from atlas_harness.kernel.errors import EventValidationError

MAX_CONTENT_CHARS = 2_000
"""Cap on one record's rendered content. A memory that fills the context defeats
the point of retrieving a few relevant ones."""

HOUR_MS = 60 * 60 * 1000
DAY_MS = 24 * HOUR_MS

DEFAULT_TTL_MS: dict[str, int | None] = {
    "working": HOUR_MS,
    "episodic": 14 * DAY_MS,
    "semantic": None,
    "procedural": None,
}
"""Default lifetime per layer. ``None`` means the record does not expire on its
own; retiring it takes an explicit management command."""


class MemoryLayer(StrEnum):
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"

    @property
    def default_ttl_ms(self) -> int | None:
        return DEFAULT_TTL_MS[self.value]

    @property
    def is_long_term(self) -> bool:
        """Whether a record in this layer may be presented as standing fact."""

        return self in LONG_TERM_LAYERS


LONG_TERM_LAYERS = frozenset({MemoryLayer.SEMANTIC, MemoryLayer.PROCEDURAL})
"""Layers whose records describe the project rather than one run of it."""

LAYER_WEIGHT: dict[MemoryLayer, float] = {
    MemoryLayer.PROCEDURAL: 1.0,
    MemoryLayer.SEMANTIC: 0.9,
    MemoryLayer.EPISODIC: 0.6,
    MemoryLayer.WORKING: 0.5,
}
"""Relevance multiplier applied on top of the text score. A procedural memory that
matches as well as an episodic one is the more useful of the two, so the ordering
is deliberate rather than left to BM25 alone."""


def parse_layer(value: str) -> MemoryLayer:
    """Coerce a stored string into a layer, refusing anything outside the set."""

    if value not in MEMORY_LAYERS:
        raise EventValidationError(
            "unknown memory layer",
            details={"layer": value, "supported": sorted(MEMORY_LAYERS)},
        )
    return MemoryLayer(value)


class MemoryRecord(BaseModel):
    """One memory with the provenance that makes it auditable.

    ``source_task`` and ``evidence_refs`` are what let an operator answer "why does
    the agent believe this", which is the question that makes retrieved memory
    trustworthy enough to act on.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_id: str = Field(min_length=1)
    layer: MemoryLayer = MemoryLayer.WORKING
    content: str = ""
    source_task: str | None = None
    source_session_id: str | None = None
    created_at_ms: int = 0
    expires_at_ms: int | None = None
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    evidence_refs: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    def is_expired(self, now_ms: int) -> bool:
        """Expiry is read off the record, never inferred from its age.

        A record with no ``expires_at_ms`` never expires on its own; removing it is
        a deliberate management action, not a side effect of time passing.
        """

        return self.expires_at_ms is not None and now_ms >= self.expires_at_ms

    def is_injectable(self, now_ms: int) -> bool:
        return not self.is_expired(now_ms)

    @property
    def is_long_term(self) -> bool:
        return self.layer.is_long_term

    def as_context_text(self) -> str:
        """Render for the capability slot, provenance included.

        The layer and confidence travel with the content on purpose: a model reading
        an episodic observation should be able to tell it apart from a project fact.
        """

        head = f"[memory:{self.layer.value} confidence={self.confidence:.2f}]"
        parts = [f"{head} {self.content[:MAX_CONTENT_CHARS]}"]
        if self.source_task:
            parts.append(f"source task: {self.source_task}")
        if self.evidence_refs:
            parts.append("evidence: " + ", ".join(self.evidence_refs))
        return "\n".join(parts)

    def to_payload(self) -> dict[str, object]:
        return {
            "memory_id": self.memory_id,
            "layer": self.layer.value,
            "content": self.content,
            "source_task": self.source_task,
            "source_session_id": self.source_session_id,
            "created_at_ms": self.created_at_ms,
            "expires_at_ms": self.expires_at_ms,
            "confidence": self.confidence,
            "evidence_refs": list(self.evidence_refs),
            "tags": list(self.tags),
        }


def expiry_for(layer: MemoryLayer, created_at_ms: int, ttl_ms: int | None = None) -> int | None:
    """Resolve a record's expiry from its layer's default or an explicit TTL."""

    effective = layer.default_ttl_ms if ttl_ms is None else ttl_ms
    return None if effective is None else created_at_ms + effective
