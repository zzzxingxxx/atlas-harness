"""Persisting memory records and their provenance.

Two things are written for every record, in this order: a ``memory_stored`` event in
the session that produced it, then the row and FTS entry that make it retrievable.
The event is the record; the table is an index over it, exactly like ``events`` is an
index over the JSONL log. :meth:`MemoryRepository.rebuild` proves the direction of
that dependency by throwing the tables away and refilling them from the log.

Expiry follows the same rule. ``expire`` writes a ``memory_expired`` event and drops
the FTS entry so the record stops being retrievable, but the row and the original
event both stay: the plan requires an explicit management command, an audit record
and a backup before anything is truly deleted, and none of that is expiry's job.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from typing import Any

from atlas_harness.events.models import Event, EventType
from atlas_harness.events.store import EventStore
from atlas_harness.kernel.clock import Clock, SystemClock
from atlas_harness.kernel.ids import new_id
from atlas_harness.memory.models import (
    MAX_CONTENT_CHARS,
    MemoryLayer,
    MemoryRecord,
    expiry_for,
    parse_layer,
)
from atlas_harness.tools.redaction import redact

_INSERT_MEMORY = """
INSERT INTO memories (
    memory_id, session_id, layer, content, source_task, source_session_id,
    created_at_ms, expires_at_ms, confidence, evidence_json, tags_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(memory_id) DO UPDATE SET
    layer = excluded.layer,
    content = excluded.content,
    source_task = excluded.source_task,
    source_session_id = excluded.source_session_id,
    created_at_ms = excluded.created_at_ms,
    expires_at_ms = excluded.expires_at_ms,
    confidence = excluded.confidence,
    evidence_json = excluded.evidence_json,
    tags_json = excluded.tags_json
"""

MEMORY_COLUMNS: tuple[str, ...] = (
    "memory_id",
    "layer",
    "content",
    "source_task",
    "source_session_id",
    "created_at_ms",
    "expires_at_ms",
    "confidence",
    "evidence_json",
    "tags_json",
)
"""Column order :func:`record_from_row` expects. Shared with retrieval so a schema
change cannot leave the two readers disagreeing about which column is which."""

_MEMORY_COLUMNS = ", ".join(MEMORY_COLUMNS)


class MemoryRepository:
    """Write and read memory records over the shared SQLite connection."""

    def __init__(self, store: EventStore, *, clock: Clock | None = None) -> None:
        self.store = store
        self._clock = clock or SystemClock()

    @property
    def _connection(self) -> sqlite3.Connection:
        return self.store.index.connection

    def remember(
        self,
        content: str,
        *,
        session_id: str,
        layer: MemoryLayer | str = MemoryLayer.WORKING,
        operation_id: str | None = None,
        source_task: str | None = None,
        confidence: float = 0.5,
        evidence_refs: Sequence[str] = (),
        tags: Sequence[str] = (),
        ttl_ms: int | None = None,
        memory_id: str | None = None,
    ) -> MemoryRecord:
        """Record one memory. The content is redacted before it is stored.

        A memory is durable and re-enters prompts long after the run that produced
        it, so a secret captured here would outlive every filter downstream.
        """

        resolved_layer = layer if isinstance(layer, MemoryLayer) else parse_layer(layer)
        created_at_ms = self._clock.now_ms()
        record = MemoryRecord(
            memory_id=memory_id or new_id("mem"),
            layer=resolved_layer,
            content=redact(content)[:MAX_CONTENT_CHARS],
            source_task=source_task,
            source_session_id=session_id,
            created_at_ms=created_at_ms,
            expires_at_ms=expiry_for(resolved_layer, created_at_ms, ttl_ms),
            confidence=confidence,
            evidence_refs=tuple(evidence_refs),
            tags=tuple(tags),
        )
        self.store.append_new(
            EventType.MEMORY_STORED,
            session_id=session_id,
            operation_id=operation_id,
            payload=record.to_payload(),
        )
        self._index(record, session_id=session_id)
        return record

    def expire(
        self,
        memory_id: str,
        *,
        session_id: str,
        operation_id: str | None = None,
        reason: str = "ttl",
    ) -> MemoryRecord | None:
        """Take a record out of the retrievable set without deleting it."""

        record = self.get(memory_id)
        if record is None:
            return None
        self.store.append_new(
            EventType.MEMORY_EXPIRED,
            session_id=session_id,
            operation_id=operation_id,
            payload={
                "memory_id": memory_id,
                "layer": record.layer.value,
                "reason": reason,
                "expired_at_ms": self._clock.now_ms(),
            },
        )
        self._unindex(memory_id)
        return record

    def sweep(self, *, session_id: str, operation_id: str | None = None) -> list[str]:
        """Expire every record whose own ``expires_at_ms`` has passed.

        Retrieval already refuses expired records, so this is housekeeping rather
        than a correctness boundary: it keeps the FTS index from carrying entries
        that can never be selected.
        """

        now_ms = self._clock.now_ms()
        stale = [record.memory_id for record in self.all() if record.is_expired(now_ms)]
        for memory_id in stale:
            self.expire(memory_id, session_id=session_id, operation_id=operation_id, reason="ttl")
        return stale

    def get(self, memory_id: str) -> MemoryRecord | None:
        cursor = self._connection.execute(
            f"SELECT {_MEMORY_COLUMNS} FROM memories WHERE memory_id = ?", (memory_id,)
        )
        row = cursor.fetchone()
        return None if row is None else record_from_row(row)

    def all(self, *, layer: MemoryLayer | None = None) -> list[MemoryRecord]:
        sql = f"SELECT {_MEMORY_COLUMNS} FROM memories"
        params: tuple[object, ...] = ()
        if layer is not None:
            sql += " WHERE layer = ?"
            params = (layer.value,)
        sql += " ORDER BY created_at_ms DESC, memory_id"
        cursor = self._connection.execute(sql, params)
        return [record_from_row(row) for row in cursor.fetchall()]

    def retrievable(self, now_ms: int | None = None) -> list[MemoryRecord]:
        """Records eligible for injection: everything the clock has not retired."""

        moment = self._clock.now_ms() if now_ms is None else now_ms
        return [record for record in self.all() if record.is_injectable(moment)]

    def rebuild(self, session_id: str) -> int:
        """Refill one session's rows and FTS entries from its event log.

        Delete-then-insert, like every other projection here: a row whose event is
        gone from the log has to disappear rather than linger.
        """

        events = self.store.read_events(session_id)
        connection = self._connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.cursor()
            cursor.execute(
                "DELETE FROM memories_fts WHERE memory_id IN"
                " (SELECT memory_id FROM memories WHERE session_id = ?)",
                (session_id,),
            )
            cursor.execute("DELETE FROM memories WHERE session_id = ?", (session_id,))
            connection.execute("COMMIT")
        except BaseException:
            self._rollback()
            raise
        return self._replay(events, session_id=session_id)

    def _replay(self, events: Iterable[Event], *, session_id: str) -> int:
        expired: set[str] = set()
        records: dict[str, MemoryRecord] = {}
        for event in events:
            payload = event.payload.model_dump(mode="python")
            if event.event_type is EventType.MEMORY_STORED:
                record = _record_from_payload(payload)
                records[record.memory_id] = record
            elif event.event_type is EventType.MEMORY_EXPIRED:
                expired.add(str(payload["memory_id"]))
        for memory_id, record in records.items():
            self._index(record, session_id=session_id, searchable=memory_id not in expired)
        return len(records)

    def _index(self, record: MemoryRecord, *, session_id: str, searchable: bool = True) -> None:
        connection = self._connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.cursor()
            cursor.execute(
                _INSERT_MEMORY,
                (
                    record.memory_id,
                    session_id,
                    record.layer.value,
                    record.content,
                    record.source_task,
                    record.source_session_id,
                    record.created_at_ms,
                    record.expires_at_ms,
                    record.confidence,
                    json.dumps(list(record.evidence_refs), ensure_ascii=False),
                    json.dumps(list(record.tags), ensure_ascii=False),
                ),
            )
            cursor.execute("DELETE FROM memories_fts WHERE memory_id = ?", (record.memory_id,))
            if searchable:
                cursor.execute(
                    "INSERT INTO memories_fts (content, tags, memory_id) VALUES (?, ?, ?)",
                    (record.content, " ".join(record.tags), record.memory_id),
                )
            connection.execute("COMMIT")
        except BaseException:
            self._rollback()
            raise

    def _unindex(self, memory_id: str) -> None:
        connection = self._connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM memories_fts WHERE memory_id = ?", (memory_id,))
            connection.execute("COMMIT")
        except BaseException:
            self._rollback()
            raise

    def _rollback(self) -> None:
        try:
            self._connection.execute("ROLLBACK")
        except sqlite3.Error:  # nothing was open
            pass


def record_from_row(row: Sequence[Any]) -> MemoryRecord:
    """Build a record from a row selected in :data:`MEMORY_COLUMNS` order."""

    return MemoryRecord(
        memory_id=str(row[0]),
        layer=parse_layer(str(row[1])),
        content=str(row[2]),
        source_task=_optional_str(row[3]),
        source_session_id=_optional_str(row[4]),
        created_at_ms=int(row[5] or 0),
        expires_at_ms=_optional_int(row[6]),
        confidence=float(row[7] or 0.0),
        evidence_refs=tuple(json.loads(str(row[8] or "[]"))),
        tags=tuple(json.loads(str(row[9] or "[]"))),
    )


def _record_from_payload(payload: dict[str, Any]) -> MemoryRecord:
    return MemoryRecord(
        memory_id=str(payload["memory_id"]),
        layer=parse_layer(str(payload.get("layer") or "working")),
        content=str(payload.get("content") or ""),
        source_task=_optional_str(payload.get("source_task")),
        source_session_id=_optional_str(payload.get("source_session_id")),
        created_at_ms=int(payload.get("created_at_ms") or 0),
        expires_at_ms=_optional_int(payload.get("expires_at_ms")),
        confidence=float(payload.get("confidence") or 0.0),
        evidence_refs=tuple(str(value) for value in payload.get("evidence_refs") or ()),
        tags=tuple(str(value) for value in payload.get("tags") or ()),
    )


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)
