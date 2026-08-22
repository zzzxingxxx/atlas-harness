"""SQLite queries over lanes, operations, tool calls and snapshots.

These four tables are a projection, exactly like the ``events`` table: the JSONL log
is the only truth and every row here can be rebuilt from it by :meth:`sync`. Nothing
reads them to make a recovery decision — recovery replays the log — so a stale or
missing row costs a query result, never correctness.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict

from atlas_harness.events.models import DEFAULT_LANE
from atlas_harness.events.reducer import SessionState
from atlas_harness.events.store import EventStore


class LaneRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: str
    lane: str
    parent_lane: str | None = None
    status: str = "idle"
    created_at: int = 0
    updated_at: int = 0


class OperationRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    session_id: str
    lane: str = DEFAULT_LANE
    status: str = "started"
    started_at: int | None = None
    finished_at: int | None = None
    deadline_ms: int | None = None


class ToolCallRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    tool_call_id: str
    session_id: str
    operation_id: str
    tool_name: str
    idempotency_key: str | None = None
    risk: str | None = None
    idempotent: bool = False
    status: str = "started"


class SnapshotRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    snapshot_id: str
    session_id: str
    lane: str = DEFAULT_LANE
    seq: int = 0
    path: str | None = None
    checksum: str | None = None
    created_at: int = 0


class SessionRepository:
    """Read and refresh the session-scoped index tables."""

    def __init__(self, store: EventStore) -> None:
        self._store = store

    @property
    def _connection(self) -> sqlite3.Connection:
        return self._store.index.connection

    def sync(self, session_id: str, state: SessionState | None = None) -> SessionState:
        """Rewrite every row for one session from the projected state.

        Delete-then-insert rather than upsert: the log is authoritative, so a row that
        no longer has a matching event has to disappear instead of lingering.
        """

        projected = state if state is not None else self._store.load_state(session_id)
        connection = self._connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.cursor()
            for table in ("lanes", "operations", "tool_calls", "snapshots"):
                cursor.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))
            cursor.executemany(
                "INSERT INTO lanes (session_id, lane, parent_lane, status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        session_id,
                        lane.lane_id,
                        lane.parent_lane,
                        lane.status,
                        projected.created_at_ms or 0,
                        projected.updated_at_ms or 0,
                    )
                    for lane in projected.lanes.values()
                ],
            )
            cursor.executemany(
                "INSERT INTO operations (id, session_id, lane, status, started_at, finished_at,"
                " deadline_ms) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        operation.operation_id,
                        session_id,
                        operation.lane_id,
                        operation.status,
                        operation.started_at_ms,
                        operation.finished_at_ms,
                        None,
                    )
                    for operation in projected.operations.values()
                ],
            )
            cursor.executemany(
                "INSERT INTO tool_calls (tool_call_id, session_id, operation_id, tool_name,"
                " idempotency_key, risk, idempotent, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        call.call_id,
                        session_id,
                        operation.operation_id,
                        call.tool_name,
                        call.idempotency_key,
                        call.risk,
                        int(call.idempotent),
                        call.status,
                    )
                    for operation in projected.operations.values()
                    for call in operation.tool_calls.values()
                ],
            )
            cursor.executemany(
                "INSERT INTO snapshots (snapshot_id, session_id, lane, seq, path, checksum,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        record.snapshot_id,
                        session_id,
                        record.lane_id,
                        record.last_seq,
                        record.path,
                        record.checksum,
                        record.created_at_ms or 0,
                    )
                    for record in projected.snapshot_records
                ],
            )
            connection.execute("COMMIT")
        except BaseException:
            self._rollback()
            raise
        return projected

    def _rollback(self) -> None:
        try:
            self._connection.execute("ROLLBACK")
        except sqlite3.Error:  # nothing was open
            pass

    def lanes(self, session_id: str) -> list[LaneRow]:
        rows = self._query(
            "SELECT session_id, lane, parent_lane, status, created_at, updated_at FROM lanes"
            " WHERE session_id = ? ORDER BY lane",
            (session_id,),
        )
        return [
            LaneRow(
                session_id=str(row[0]),
                lane=str(row[1]),
                parent_lane=_optional_str(row[2]),
                status=str(row[3]),
                created_at=int(row[4]),
                updated_at=int(row[5]),
            )
            for row in rows
        ]

    def operations(self, session_id: str, *, status: str | None = None) -> list[OperationRow]:
        sql = (
            "SELECT id, session_id, lane, status, started_at, finished_at, deadline_ms"
            " FROM operations WHERE session_id = ?"
        )
        params: list[Any] = [session_id]
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY COALESCE(started_at, 0), id"
        return [
            OperationRow(
                id=str(row[0]),
                session_id=str(row[1]),
                lane=str(row[2]),
                status=str(row[3]),
                started_at=_optional_int(row[4]),
                finished_at=_optional_int(row[5]),
                deadline_ms=_optional_int(row[6]),
            )
            for row in self._query(sql, tuple(params))
        ]

    def tool_calls(
        self, session_id: str, *, operation_id: str | None = None, status: str | None = None
    ) -> list[ToolCallRow]:
        sql = (
            "SELECT tool_call_id, session_id, operation_id, tool_name, idempotency_key, risk,"
            " idempotent, status FROM tool_calls WHERE session_id = ?"
        )
        params: list[Any] = [session_id]
        if operation_id is not None:
            sql += " AND operation_id = ?"
            params.append(operation_id)
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY tool_call_id"
        return [
            ToolCallRow(
                tool_call_id=str(row[0]),
                session_id=str(row[1]),
                operation_id=str(row[2]),
                tool_name=str(row[3]),
                idempotency_key=_optional_str(row[4]),
                risk=_optional_str(row[5]),
                idempotent=bool(row[6]),
                status=str(row[7]),
            )
            for row in self._query(sql, tuple(params))
        ]

    def snapshots(self, session_id: str, *, lane: str | None = None) -> list[SnapshotRow]:
        sql = (
            "SELECT snapshot_id, session_id, lane, seq, path, checksum, created_at FROM snapshots"
            " WHERE session_id = ?"
        )
        params: list[Any] = [session_id]
        if lane is not None:
            sql += " AND lane = ?"
            params.append(lane)
        sql += " ORDER BY seq"
        return [
            SnapshotRow(
                snapshot_id=str(row[0]),
                session_id=str(row[1]),
                lane=str(row[2]),
                seq=int(row[3]),
                path=_optional_str(row[4]),
                checksum=_optional_str(row[5]),
                created_at=int(row[6]),
            )
            for row in self._query(sql, tuple(params))
        ]

    def latest_snapshot(self, session_id: str, *, lane: str | None = None) -> SnapshotRow | None:
        records = self.snapshots(session_id, lane=lane)
        return records[-1] if records else None

    def _query(self, sql: str, params: Sequence[Any]) -> list[tuple[Any, ...]]:
        cursor = self._connection.execute(sql, tuple(params))
        return cursor.fetchall()


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)
