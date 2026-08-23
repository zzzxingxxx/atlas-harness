"""Persisting skill versions and their lifecycle transitions.

Same shape as :mod:`atlas_harness.memory.repository`: the event is the record, the
``skills`` table and its FTS index are projections that :meth:`SkillRepository.rebuild`
can throw away and refill from the log.

The one rule this module enforces beyond persistence is that a status change goes
through :func:`~atlas_harness.skills.models.check_transition` before it is written.
An illegal promotion is refused at the write, not filtered at read time, because a
``skill_status_changed`` event that the log accepted but the loader ignores is worse
than an error: the audit trail would then disagree with the effective version.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from typing import Any

from atlas_harness.events.models import Event, EventType
from atlas_harness.events.store import EventStore
from atlas_harness.kernel.clock import Clock, SystemClock
from atlas_harness.memory.retrieval import build_match_query
from atlas_harness.skills.models import (
    SkillRecord,
    SkillStatus,
    check_transition,
    checksum_for,
    parse_status,
)

SKILL_COLUMNS: tuple[str, ...] = (
    "skill_id",
    "version",
    "status",
    "name",
    "description",
    "body",
    "source_path",
    "checksum",
    "scopes_json",
    "triggers_json",
    "evidence_json",
    "source_task",
    "registered_at_ms",
)

_SKILL_COLUMNS = ", ".join(SKILL_COLUMNS)

_INSERT_SKILL = """
INSERT INTO skills (
    skill_id, version, status, name, description, body, source_path, checksum,
    scopes_json, triggers_json, evidence_json, source_task, registered_at_ms
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(skill_id, version) DO UPDATE SET
    status = excluded.status,
    name = excluded.name,
    description = excluded.description,
    body = excluded.body,
    source_path = excluded.source_path,
    checksum = excluded.checksum,
    scopes_json = excluded.scopes_json,
    triggers_json = excluded.triggers_json,
    evidence_json = excluded.evidence_json,
    source_task = excluded.source_task,
    registered_at_ms = excluded.registered_at_ms
"""


_SELECT_COLUMNS = ", ".join(f"s.{column}" for column in SKILL_COLUMNS)

_SEARCH_SQL = f"""
SELECT {_SELECT_COLUMNS}, bm25(skills_fts, 1.0, 2.0) AS rank
FROM skills_fts
JOIN skills AS s ON s.skill_id || '@' || s.version = skills_fts.row_key
WHERE skills_fts MATCH ?
ORDER BY rank
LIMIT ?
"""
"""A declared trigger weighs more than prose in the description: the trigger is the
author's own statement of when the skill applies."""


def row_key(skill_id: str, version: str) -> str:
    """Key the FTS index uses. FTS5 has no composite primary key of its own."""

    return f"{skill_id}@{version}"


class SkillRepository:
    """Write and read skill versions over the shared SQLite connection."""

    def __init__(self, store: EventStore, *, clock: Clock | None = None) -> None:
        self.store = store
        self._clock = clock or SystemClock()

    @property
    def _connection(self) -> sqlite3.Connection:
        return self.store.index.connection

    def register(
        self,
        record: SkillRecord,
        *,
        session_id: str,
        operation_id: str | None = None,
    ) -> SkillRecord:
        """Record that a skill version exists. Registration is not activation.

        A freshly registered skill lands in whatever status its definition declares,
        and the loader defaults that to ``draft``, so loading a directory of skill
        files never makes them readable by the model on its own.
        """

        stored = record.model_copy(
            update={
                "checksum": record.checksum or checksum_for(record.body),
                "registered_at_ms": record.registered_at_ms or self._clock.now_ms(),
            }
        )
        self.store.append_new(
            EventType.SKILL_REGISTERED,
            session_id=session_id,
            operation_id=operation_id,
            payload=stored.to_payload(),
        )
        self._index(stored)
        return stored

    def set_status(
        self,
        skill_id: str,
        version: str,
        to_status: SkillStatus,
        *,
        session_id: str,
        operation_id: str | None = None,
        reason: str | None = None,
        evaluation_ref: str | None = None,
    ) -> SkillRecord:
        """Move one version along the lifecycle graph.

        ``evaluation_ref`` is what makes a promotion auditable: the plan requires a
        candidate to pass evaluation first, and a reference to that evaluation is the
        only durable evidence that it did.
        """

        current = self.get(skill_id, version)
        if current is None:
            raise _unknown_skill(skill_id, version)
        check_transition(current.status, to_status)
        self.store.append_new(
            EventType.SKILL_STATUS_CHANGED,
            session_id=session_id,
            operation_id=operation_id,
            payload={
                "skill_id": skill_id,
                "version": version,
                "from_status": current.status.value,
                "to_status": to_status.value,
                "reason": reason,
                "evaluation_ref": evaluation_ref,
            },
        )
        updated = current.model_copy(update={"status": to_status})
        self._index(updated)
        return updated

    def get(self, skill_id: str, version: str) -> SkillRecord | None:
        cursor = self._connection.execute(
            f"SELECT {_SKILL_COLUMNS} FROM skills WHERE skill_id = ? AND version = ?",
            (skill_id, version),
        )
        row = cursor.fetchone()
        return None if row is None else record_from_row(row)

    def all(self, *, status: SkillStatus | None = None) -> list[SkillRecord]:
        sql = f"SELECT {_SKILL_COLUMNS} FROM skills"
        params: tuple[object, ...] = ()
        if status is not None:
            sql += " WHERE status = ?"
            params = (status.value,)
        sql += " ORDER BY skill_id, version"
        cursor = self._connection.execute(sql, params)
        return [record_from_row(row) for row in cursor.fetchall()]

    def active(self) -> list[SkillRecord]:
        """Versions eligible for injection, one per skill id.

        When several versions of one skill are active — which a bad promotion can
        produce — the highest version wins so the choice is deterministic rather
        than dependent on row order.
        """

        newest: dict[str, SkillRecord] = {}
        for record in self.all(status=SkillStatus.ACTIVE):
            existing = newest.get(record.skill_id)
            if existing is None or _version_key(record.version) > _version_key(existing.version):
                newest[record.skill_id] = record
        return [newest[key] for key in sorted(newest)]

    def search(self, query: str, *, limit: int = 10) -> list[tuple[SkillRecord, float]]:
        """Keyword search over description, name, body and triggers.

        Only ``active`` versions are searched. A candidate that matched perfectly
        still must not surface, and filtering here rather than at the caller means
        every path into retrieval inherits that rule.
        """

        match = build_match_query(query)
        if not match:
            return []
        try:
            rows = self._connection.execute(_SEARCH_SQL, (match, max(limit, 1) * 4)).fetchall()
        except sqlite3.OperationalError:
            return []
        active = {record.key: record for record in self.active()}
        hits: list[tuple[SkillRecord, float]] = []
        for row in rows:
            record = record_from_row(row[: len(SKILL_COLUMNS)])
            if active.get(record.key) is None:
                continue
            hits.append((record, -float(row[len(SKILL_COLUMNS)] or 0.0)))
        hits.sort(key=lambda hit: (-hit[1], hit[0].skill_id, hit[0].version))
        return hits[:limit]

    def rebuild(self, session_id: str) -> int:
        """Refill rows and FTS entries for the skills this session registered."""

        events = self.store.read_events(session_id)
        connection = self._connection
        registered = {
            str(event.payload.model_dump(mode="python")["skill_id"])
            for event in events
            if event.event_type is EventType.SKILL_REGISTERED
        }
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.cursor()
            for skill_id in sorted(registered):
                cursor.execute(
                    "DELETE FROM skills_fts WHERE row_key IN"
                    " (SELECT skill_id || '@' || version FROM skills WHERE skill_id = ?)",
                    (skill_id,),
                )
                cursor.execute("DELETE FROM skills WHERE skill_id = ?", (skill_id,))
            connection.execute("COMMIT")
        except BaseException:
            self._rollback()
            raise
        return self._replay(events)

    def _replay(self, events: Iterable[Event]) -> int:
        records: dict[tuple[str, str], SkillRecord] = {}
        for event in events:
            payload = event.payload.model_dump(mode="python")
            if event.event_type is EventType.SKILL_REGISTERED:
                record = record_from_payload(payload)
                records[record.key] = record
            elif event.event_type is EventType.SKILL_STATUS_CHANGED:
                key = (str(payload["skill_id"]), str(payload.get("version") or "0.1.0"))
                known = records.get(key)
                if known is not None:
                    records[key] = known.model_copy(
                        update={"status": parse_status(str(payload["to_status"]))}
                    )
        for record in records.values():
            self._index(record)
        return len(records)

    def _index(self, record: SkillRecord) -> None:
        connection = self._connection
        key = row_key(record.skill_id, record.version)
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.cursor()
            cursor.execute(
                _INSERT_SKILL,
                (
                    record.skill_id,
                    record.version,
                    record.status.value,
                    record.name,
                    record.description,
                    record.body,
                    record.source_path,
                    record.checksum or checksum_for(record.body),
                    json.dumps(list(record.required_scopes), ensure_ascii=False),
                    json.dumps(list(record.triggers), ensure_ascii=False),
                    json.dumps(list(record.evidence_refs), ensure_ascii=False),
                    record.source_task,
                    record.registered_at_ms,
                ),
            )
            cursor.execute("DELETE FROM skills_fts WHERE row_key = ?", (key,))
            cursor.execute(
                "INSERT INTO skills_fts (description, triggers, row_key) VALUES (?, ?, ?)",
                (
                    " ".join(filter(None, (record.name, record.description, record.body))),
                    " ".join(record.triggers),
                    key,
                ),
            )
            connection.execute("COMMIT")
        except BaseException:
            self._rollback()
            raise

    def _rollback(self) -> None:
        try:
            self._connection.execute("ROLLBACK")
        except sqlite3.Error:  # nothing was open
            pass


def record_from_row(row: Sequence[Any]) -> SkillRecord:
    return SkillRecord(
        skill_id=str(row[0]),
        version=str(row[1]),
        status=parse_status(str(row[2])),
        name=None if row[3] is None else str(row[3]),
        description=str(row[4] or ""),
        body=str(row[5] or ""),
        source_path=None if row[6] is None else str(row[6]),
        checksum=None if row[7] is None else str(row[7]),
        required_scopes=tuple(json.loads(str(row[8] or "[]"))),
        triggers=tuple(json.loads(str(row[9] or "[]"))),
        evidence_refs=tuple(json.loads(str(row[10] or "[]"))),
        source_task=None if row[11] is None else str(row[11]),
        registered_at_ms=int(row[12] or 0),
    )


def record_from_payload(payload: dict[str, Any]) -> SkillRecord:
    return SkillRecord(
        skill_id=str(payload["skill_id"]),
        version=str(payload.get("version") or "0.1.0"),
        status=parse_status(str(payload.get("status") or "draft")),
        name=_optional_str(payload.get("name")),
        description=str(payload.get("description") or ""),
        body=str(payload.get("body") or ""),
        source_path=_optional_str(payload.get("source_path")),
        checksum=_optional_str(payload.get("checksum")),
        required_scopes=tuple(str(value) for value in payload.get("required_scopes") or ()),
        triggers=tuple(str(value) for value in payload.get("triggers") or ()),
        evidence_refs=tuple(str(value) for value in payload.get("evidence_refs") or ()),
        source_task=_optional_str(payload.get("source_task")),
        registered_at_ms=int(payload.get("registered_at_ms") or 0),
    )


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _version_key(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in version.split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _unknown_skill(skill_id: str, version: str) -> Exception:
    from atlas_harness.kernel.errors import EventValidationError

    return EventValidationError(
        "unknown skill version",
        details={"skill_id": skill_id, "version": version},
    )
