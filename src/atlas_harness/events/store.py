"""Append-only JSONL event log with a derived SQLite index.

The JSONL file is the source of truth. SQLite is a rebuildable index used for
uniqueness checks and session listings, so a crash between the two writes is
repaired by re-reading the log rather than by trusting the database.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Iterator, Sequence
from pathlib import Path
from types import TracebackType
from typing import Any

from pydantic import BaseModel, ValidationError

from atlas_harness.config import Settings
from atlas_harness.events.models import (
    CURRENT_SCHEMA_VERSION,
    DEFAULT_LANE,
    Event,
    EventType,
    Payload,
)
from atlas_harness.events.reducer import SessionState, replay
from atlas_harness.events.subscriptions import EventBus
from atlas_harness.kernel.clock import Clock
from atlas_harness.kernel.errors import (
    EventLogCorruptionError,
    EventStoreError,
    EventValidationError,
    SessionNotFoundError,
)
from atlas_harness.kernel.faults import FaultInjector
from atlas_harness.kernel.ids import (
    SESSION_ID_PATTERN,
    IdFactory,
    new_id,
    validate_session_id,
)

LOGGER = logging.getLogger("atlas_harness.events.store")

SESSIONS_DIRNAME = "sessions"
LOG_FILENAME = "events.jsonl"
INDEX_FILENAME = "index.sqlite3"

FAULT_BEFORE_LOG_WRITE = "event_store.before_log_write"
FAULT_AFTER_LOG_WRITE = "event_store.after_log_write"
FAULT_BEFORE_INDEX_COMMIT = "event_store.before_index_commit"
FAULT_AFTER_INDEX_COMMIT = "event_store.after_index_commit"


class SessionSummary(BaseModel):
    """Row of the SQLite session index, used for listings."""

    session_id: str
    status: str = "created"
    title: str | None = None
    workspace_root: str | None = None
    schema_version: int = CURRENT_SCHEMA_VERSION
    created_at_ms: int = 0
    updated_at_ms: int = 0
    last_seq: int = 0
    event_count: int = 0


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id     TEXT PRIMARY KEY,
    status         TEXT NOT NULL,
    title          TEXT,
    workspace_root TEXT,
    schema_version INTEGER NOT NULL,
    created_at_ms  INTEGER NOT NULL,
    updated_at_ms  INTEGER NOT NULL,
    last_seq       INTEGER NOT NULL,
    event_count    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    session_id      TEXT NOT NULL,
    lane_id         TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    event_id        TEXT NOT NULL,
    operation_id    TEXT,
    event_type      TEXT NOT NULL,
    timestamp_ms    INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL,
    schema_version  INTEGER NOT NULL,
    payload_json    TEXT NOT NULL,
    PRIMARY KEY (session_id, seq)
);
CREATE UNIQUE INDEX IF NOT EXISTS events_event_id ON events (event_id);
CREATE UNIQUE INDEX IF NOT EXISTS events_idempotency
    ON events (session_id, idempotency_key);
CREATE INDEX IF NOT EXISTS events_lane_seq ON events (session_id, lane_id, seq);
"""

_INSERT_EVENT = """
INSERT INTO events (
    session_id, lane_id, seq, event_id, operation_id,
    event_type, timestamp_ms, idempotency_key, schema_version, payload_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_UPSERT_SESSION = """
INSERT INTO sessions (
    session_id, status, title, workspace_root, schema_version,
    created_at_ms, updated_at_ms, last_seq, event_count
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(session_id) DO UPDATE SET
    status = excluded.status,
    title = excluded.title,
    workspace_root = excluded.workspace_root,
    created_at_ms = excluded.created_at_ms,
    updated_at_ms = excluded.updated_at_ms,
    last_seq = excluded.last_seq,
    event_count = excluded.event_count
"""

_SESSION_COLUMNS = (
    "session_id, status, title, workspace_root, schema_version, "
    "created_at_ms, updated_at_ms, last_seq, event_count"
)


class SqliteEventIndex:
    """Rebuildable index over the append-only logs."""

    def __init__(self, path: Path, *, faults: FaultInjector | None = None) -> None:
        self.path = path
        self._faults = faults or FaultInjector()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(path), isolation_level=None)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.executescript(_SCHEMA)

    def close(self) -> None:
        self._connection.close()

    def indexed_event_ids(self, session_id: str) -> dict[int, str]:
        cursor = self._connection.execute(
            "SELECT seq, event_id FROM events WHERE session_id = ? ORDER BY seq",
            (session_id,),
        )
        return {int(row[0]): str(row[1]) for row in cursor.fetchall()}

    def last_seq(self, session_id: str) -> int:
        cursor = self._connection.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM events WHERE session_id = ?",
            (session_id,),
        )
        return int(cursor.fetchone()[0])

    def has_event_id(self, event_id: str) -> bool:
        cursor = self._connection.execute(
            "SELECT 1 FROM events WHERE event_id = ? LIMIT 1", (event_id,)
        )
        return cursor.fetchone() is not None

    def has_idempotency_key(self, session_id: str, key: str) -> bool:
        cursor = self._connection.execute(
            "SELECT 1 FROM events WHERE session_id = ? AND idempotency_key = ? LIMIT 1",
            (session_id, key),
        )
        return cursor.fetchone() is not None

    def get_session(self, session_id: str) -> SessionSummary | None:
        cursor = self._connection.execute(
            f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE session_id = ?", (session_id,)
        )
        row = cursor.fetchone()
        return None if row is None else _session_summary(row)

    def list_sessions(self) -> list[SessionSummary]:
        cursor = self._connection.execute(
            f"SELECT {_SESSION_COLUMNS} FROM sessions ORDER BY updated_at_ms DESC, session_id"
        )
        return [_session_summary(row) for row in cursor.fetchall()]

    def write(
        self,
        session_id: str,
        *,
        insert: Sequence[Event] = (),
        delete_seqs: Sequence[int] = (),
    ) -> None:
        """Apply index changes for one session inside a single transaction."""

        connection = self._connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.cursor()
            if delete_seqs:
                cursor.executemany(
                    "DELETE FROM events WHERE session_id = ? AND seq = ?",
                    [(session_id, seq) for seq in delete_seqs],
                )
            if insert:
                cursor.executemany(_INSERT_EVENT, [_event_row(event) for event in insert])
            self._refresh_session(cursor, session_id)
            self._faults.check(FAULT_BEFORE_INDEX_COMMIT)
            connection.execute("COMMIT")
        except BaseException as exc:
            self._rollback()
            if isinstance(exc, sqlite3.Error):
                raise EventStoreError(
                    "event index write failed",
                    details={"session_id": session_id, "error": str(exc)},
                ) from exc
            raise

    def _rollback(self) -> None:
        try:
            self._connection.execute("ROLLBACK")
        except sqlite3.Error:  # no transaction was open
            pass

    def _refresh_session(self, cursor: sqlite3.Cursor, session_id: str) -> None:
        row = cursor.execute(
            "SELECT COUNT(*), COALESCE(MAX(seq), 0), COALESCE(MIN(timestamp_ms), 0), "
            "COALESCE(MAX(timestamp_ms), 0) FROM events WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        event_count = int(row[0])
        last_seq = int(row[1])
        created_at_ms = int(row[2])
        updated_at_ms = int(row[3])
        if event_count == 0:
            cursor.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            return
        created = cursor.execute(
            "SELECT payload_json FROM events WHERE session_id = ? AND event_type = ? "
            "ORDER BY seq LIMIT 1",
            (session_id, EventType.SESSION_CREATED.value),
        ).fetchone()
        status = "created"
        title: str | None = None
        workspace_root: str | None = None
        if created is not None:
            payload = json.loads(created[0])
            status = "active"
            title = payload.get("title")
            workspace_root = payload.get("workspace_root")
        cursor.execute(
            _UPSERT_SESSION,
            (
                session_id,
                status,
                title,
                workspace_root,
                CURRENT_SCHEMA_VERSION,
                created_at_ms,
                updated_at_ms,
                last_seq,
                event_count,
            ),
        )


def _session_summary(row: tuple[Any, ...]) -> SessionSummary:
    return SessionSummary(
        session_id=str(row[0]),
        status=str(row[1]),
        title=None if row[2] is None else str(row[2]),
        workspace_root=None if row[3] is None else str(row[3]),
        schema_version=int(row[4]),
        created_at_ms=int(row[5]),
        updated_at_ms=int(row[6]),
        last_seq=int(row[7]),
        event_count=int(row[8]),
    )


def _event_row(event: Event) -> tuple[Any, ...]:
    return (
        event.session_id,
        event.lane_id,
        event.seq,
        event.event_id,
        event.operation_id,
        event.event_type.value,
        event.timestamp_ms,
        event.idempotency_key,
        event.schema_version,
        json.dumps(event.payload.model_dump(mode="json"), sort_keys=True, ensure_ascii=False),
    )


class EventStore:
    """Append events to JSONL, index them in SQLite, project them on demand."""

    def __init__(
        self,
        data_dir: Path,
        *,
        clock: Clock | None = None,
        faults: FaultInjector | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self.data_dir = data_dir
        self.sessions_dir = data_dir / SESSIONS_DIRNAME
        self.faults = faults or FaultInjector()
        self.bus = bus
        self.ids = IdFactory(clock)
        self._index = SqliteEventIndex(data_dir / INDEX_FILENAME, faults=self.faults)
        self._last_seq: dict[str, int] = {}
        self._reconciled: set[str] = set()

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        clock: Clock | None = None,
        faults: FaultInjector | None = None,
        bus: EventBus | None = None,
    ) -> EventStore:
        return cls(settings.resolved_data_dir(), clock=clock, faults=faults, bus=bus)

    @property
    def index(self) -> SqliteEventIndex:
        return self._index

    def log_path(self, session_id: str) -> Path:
        validate_session_id(session_id)
        return self.sessions_dir / session_id / LOG_FILENAME

    def new_session_id(self) -> str:
        return new_id("ses")

    def close(self) -> None:
        self._index.close()

    def __enter__(self) -> EventStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def append(self, event: Event) -> Event:
        """Persist one event. The log and the index move together or not at all."""

        if event.schema_version != CURRENT_SCHEMA_VERSION:
            raise EventValidationError(
                "unsupported event schema version",
                details={"schema_version": event.schema_version},
            )
        session_id = event.session_id
        path = self.log_path(session_id)
        self._reconcile(session_id)
        expected = self._last_seq.get(session_id, 0) + 1
        if event.seq != expected:
            raise EventValidationError(
                "event seq is duplicated, missing or out of order",
                details={
                    "session_id": session_id,
                    "expected_seq": expected,
                    "actual_seq": event.seq,
                    "last_valid_seq": expected - 1,
                },
            )
        if self._index.has_event_id(event.event_id):
            raise EventStoreError("duplicate event id", details={"event_id": event.event_id})
        if self._index.has_idempotency_key(session_id, event.idempotency_key):
            raise EventStoreError(
                "duplicate idempotency key",
                details={"session_id": session_id, "idempotency_key": event.idempotency_key},
            )

        line = json.dumps(event.to_json_dict(), sort_keys=True, ensure_ascii=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        offset = path.stat().st_size if path.exists() else 0
        self.faults.check(FAULT_BEFORE_LOG_WRITE)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{line}\n")
            handle.flush()
            os.fsync(handle.fileno())
        # A failure here leaves a durable log line the index has not seen yet;
        # _reconcile repairs that on the next open.
        self.faults.check(FAULT_AFTER_LOG_WRITE)
        try:
            self._index.write(session_id, insert=[event])
        except BaseException:
            os.truncate(path, offset)
            raise
        self._last_seq[session_id] = event.seq
        self.faults.check(FAULT_AFTER_INDEX_COMMIT)
        if self.bus is not None:
            self.bus.publish(event)
        return event

    def append_new(
        self,
        event_type: EventType,
        *,
        session_id: str,
        payload: Payload | dict[str, Any] | None = None,
        lane_id: str = DEFAULT_LANE,
        operation_id: str | None = None,
        idempotency_key_value: str | None = None,
    ) -> Event:
        """Allocate the next seq for the session and append the event."""

        event = Event.create(
            event_type,
            session_id=session_id,
            seq=self.next_seq(session_id),
            payload=payload,
            factory=self.ids,
            lane_id=lane_id,
            operation_id=operation_id,
            idempotency_key_value=idempotency_key_value,
        )
        return self.append(event)

    def iter_events(self, session_id: str) -> Iterator[Event]:
        """Yield validated events. The log is rejected on the first anomaly."""

        path = self.log_path(session_id)
        if not path.exists():
            return
        last_seq = 0
        seen_event_ids: set[str] = set()
        seen_keys: set[str] = set()
        with path.open("r", encoding="utf-8", newline="") as handle:
            for line_number, raw in enumerate(handle, start=1):
                event = _parse_line(session_id, raw, line_number, last_seq)
                if event.event_id in seen_event_ids:
                    raise _corruption(
                        "duplicate event id in log",
                        session_id=session_id,
                        line=line_number,
                        last_valid_seq=last_seq,
                        event_id=event.event_id,
                    )
                if event.idempotency_key in seen_keys:
                    raise _corruption(
                        "duplicate idempotency key in log",
                        session_id=session_id,
                        line=line_number,
                        last_valid_seq=last_seq,
                        idempotency_key=event.idempotency_key,
                    )
                seen_event_ids.add(event.event_id)
                seen_keys.add(event.idempotency_key)
                last_seq = event.seq
                yield event

    def read_events(self, session_id: str) -> list[Event]:
        return list(self.iter_events(session_id))

    def last_seq(self, session_id: str) -> int:
        self._reconcile(session_id)
        return self._last_seq.get(session_id, 0)

    def next_seq(self, session_id: str) -> int:
        return self.last_seq(session_id) + 1

    def session_exists(self, session_id: str) -> bool:
        return self.log_path(session_id).exists()

    def load_state(self, session_id: str) -> SessionState:
        events = self.read_events(session_id)
        if not events:
            raise SessionNotFoundError("session has no events", details={"session_id": session_id})
        self._reconcile_with(session_id, events)
        return replay(events, session_id=session_id)

    def list_session_ids(self) -> list[str]:
        if not self.sessions_dir.exists():
            return []
        return sorted(
            entry.name
            for entry in self.sessions_dir.iterdir()
            if SESSION_ID_PATTERN.match(entry.name) and (entry / LOG_FILENAME).exists()
        )

    def list_sessions(self, *, sync: bool = True) -> list[SessionSummary]:
        if sync:
            for session_id in self.list_session_ids():
                self._reconcile(session_id)
        return self._index.list_sessions()

    def _reconcile(self, session_id: str) -> None:
        """Bring the index in line with the log, which always wins."""

        if session_id in self._reconciled:
            return
        self._reconcile_with(session_id, self.read_events(session_id))

    def _reconcile_with(self, session_id: str, events: Sequence[Event]) -> None:
        if session_id in self._reconciled:
            return
        self._last_seq[session_id] = events[-1].seq if events else 0
        indexed = self._index.indexed_event_ids(session_id)
        logged = {event.seq: event.event_id for event in events}
        stale = [seq for seq, event_id in indexed.items() if logged.get(seq) != event_id]
        missing = [event for event in events if indexed.get(event.seq) != event.event_id]
        if stale or missing:
            LOGGER.info(
                "rebuilding event index for session %s: %d stale, %d missing rows",
                session_id,
                len(stale),
                len(missing),
            )
            self._index.write(session_id, insert=missing, delete_seqs=stale)
        self._reconciled.add(session_id)


def _corruption(
    message: str,
    *,
    session_id: str,
    line: int,
    last_valid_seq: int,
    **extra: Any,
) -> EventLogCorruptionError:
    return EventLogCorruptionError(
        message,
        details={
            **extra,
            "session_id": session_id,
            "line": line,
            "last_valid_seq": last_valid_seq,
        },
    )


def _parse_line(session_id: str, raw: str, line: int, last_seq: int) -> Event:
    if not raw.endswith("\n"):
        raise _corruption(
            "event log ends with a partial line",
            session_id=session_id,
            line=line,
            last_valid_seq=last_seq,
        )
    text = raw.strip()
    if not text:
        raise _corruption(
            "blank line in event log", session_id=session_id, line=line, last_valid_seq=last_seq
        )
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _corruption(
            "event log line is not valid json",
            session_id=session_id,
            line=line,
            last_valid_seq=last_seq,
            error=str(exc),
        ) from exc
    if not isinstance(data, dict):
        raise _corruption(
            "event log line is not a json object",
            session_id=session_id,
            line=line,
            last_valid_seq=last_seq,
        )
    try:
        event = Event.model_validate(data)
    except EventValidationError as exc:
        raise _corruption(
            exc.message,
            session_id=session_id,
            line=line,
            last_valid_seq=last_seq,
            **exc.details,
        ) from exc
    except ValidationError as exc:
        raise _corruption(
            "event does not match the current schema",
            session_id=session_id,
            line=line,
            last_valid_seq=last_seq,
            error=str(exc),
        ) from exc
    if event.session_id != session_id:
        raise _corruption(
            "event belongs to a different session",
            session_id=session_id,
            line=line,
            last_valid_seq=last_seq,
            event_session_id=event.session_id,
        )
    if event.seq != last_seq + 1:
        raise _corruption(
            "event seq is duplicated, missing or out of order",
            session_id=session_id,
            line=line,
            last_valid_seq=last_seq,
            expected_seq=last_seq + 1,
            actual_seq=event.seq,
        )
    return event
