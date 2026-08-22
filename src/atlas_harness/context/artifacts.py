"""Externalize large tool output to a file and keep only a reference in context.

A 2 MB build log is evidence: it must survive, and it must not occupy the prompt.
Those two requirements pull in opposite directions, and this module is where they
are separated. The bytes go to a file under the session directory, the log gets an
``artifact_stored`` event naming it, and the model sees a short preview plus the
artifact id it can ask about later.

Nothing here deletes anything. An artifact is written once and referenced
thereafter, so compaction never costs evidence.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from atlas_harness.events.models import ArtifactStored, EventType
from atlas_harness.events.store import EventStore
from atlas_harness.kernel.ids import new_id, validate_session_id
from atlas_harness.tools.redaction import redact, redact_value, truncate_text

ARTIFACTS_DIRNAME = "artifacts"

DEFAULT_INLINE_LIMIT = 4_096
"""Outputs at or under this many bytes stay inline; the reference would cost more
context than the content it replaces."""

PREVIEW_BYTES = 512
"""Enough of the head to let a model decide whether it needs the whole artifact."""


class ArtifactRef(BaseModel):
    """A stored artifact. ``preview`` is what the prompt carries in its place."""

    model_config = ConfigDict(frozen=True)

    artifact_id: str
    session_id: str
    operation_id: str | None = None
    kind: str = "tool_output"
    path: str
    checksum: str
    size: int = 0
    tool_name: str | None = None
    call_id: str | None = None
    preview: str = ""

    def as_context_value(self) -> dict[str, Any]:
        """The shape that replaces the output in the model's tool message."""

        return {
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "size": self.size,
            "checksum": self.checksum,
            "preview": self.preview,
            "note": (
                "output exceeded the context budget and was stored as an artifact; "
                "the full content is preserved and can be retrieved by artifact_id"
            ),
        }


class ArtifactStore:
    """Write artifacts to disk and announce them in the event log."""

    def __init__(
        self,
        store: EventStore,
        *,
        inline_limit: int = DEFAULT_INLINE_LIMIT,
        preview_bytes: int = PREVIEW_BYTES,
    ) -> None:
        self._store = store
        self._inline_limit = max(0, inline_limit)
        self._preview_bytes = max(0, preview_bytes)

    @property
    def inline_limit(self) -> int:
        return self._inline_limit

    def directory(self, session_id: str) -> Path:
        validate_session_id(session_id)
        return self._store.log_path(session_id).parent / ARTIFACTS_DIRNAME

    def should_externalize(self, value: Any) -> bool:
        return _encoded_size(value) > self._inline_limit

    def store(
        self,
        value: Any,
        *,
        session_id: str,
        operation_id: str | None = None,
        lane_id: str | None = None,
        kind: str = "tool_output",
        tool_name: str | None = None,
        call_id: str | None = None,
    ) -> ArtifactRef:
        """Persist one value as an artifact and record the reference.

        The file is written before the event, the same ordering snapshots use: a
        crash in between leaves an orphan file nothing points at, which is
        recoverable, rather than an event pointing at a file that never landed.
        """

        body = _render(value)
        artifact_id = new_id("art")
        directory = self.directory(session_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{artifact_id}.txt"
        encoded = body.encode("utf-8")
        checksum = hashlib.sha256(encoded).hexdigest()
        path.write_text(body, encoding="utf-8", newline="\n")
        preview, _ = truncate_text(body, self._preview_bytes)
        self._store.append_new(
            EventType.ARTIFACT_STORED,
            session_id=session_id,
            operation_id=operation_id,
            lane_id=lane_id or self._store.load_state(session_id).current_lane_id,
            payload=ArtifactStored(
                artifact_id=artifact_id,
                kind=kind,
                path=path.name,
                checksum=checksum,
                size=len(encoded),
                tool_name=tool_name,
                call_id=call_id,
                preview=preview,
            ),
        )
        return ArtifactRef(
            artifact_id=artifact_id,
            session_id=session_id,
            operation_id=operation_id,
            kind=kind,
            path=path.name,
            checksum=checksum,
            size=len(encoded),
            tool_name=tool_name,
            call_id=call_id,
            preview=preview,
        )

    def read(self, session_id: str, artifact_id: str) -> str | None:
        """Read one artifact back. ``None`` when the file is missing."""

        path = self.directory(session_id) / f"{artifact_id}.txt"
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def verify(self, session_id: str, artifact_id: str, checksum: str) -> bool:
        """True when the stored bytes still hash to the recorded checksum."""

        body = self.read(session_id, artifact_id)
        if body is None:
            return False
        return hashlib.sha256(body.encode("utf-8")).hexdigest() == checksum


def _render(value: Any) -> str:
    """Serialize a tool output for storage, redacted before it touches disk.

    Redaction happens here rather than at read time because an artifact file is
    as durable as the event log; a secret written into one would outlive every
    filter downstream of it.
    """

    if isinstance(value, str):
        return redact(value)
    return json.dumps(
        redact_value(value), ensure_ascii=False, sort_keys=True, indent=2, default=str
    )


def _encoded_size(value: Any) -> int:
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    try:
        serialized = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return len(str(value).encode("utf-8"))
    return len(serialized.encode("utf-8"))
