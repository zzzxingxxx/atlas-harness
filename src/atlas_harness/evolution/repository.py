"""Persisting feedback, candidates and evaluations.

Same shape as the memory and skill repositories: the event is the record, the tables
are projections. :meth:`EvolutionRepository.rebuild` throws the rows away and refills
them from the log, which is what keeps the log authoritative rather than merely
first.

The one thing this module does beyond persistence is register a candidate's skill
version at ``candidate`` status as a side effect of proposing it. That is deliberate
coupling: a candidate that existed as a row but not as a skill version could never be
promoted, because promotion moves a *skill version* along the lifecycle. Registering
it up front means the pending window and the lifecycle agree about what exists.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from typing import Any

from atlas_harness.events.models import EvaluationMetrics, Event, EventType
from atlas_harness.events.store import EventStore
from atlas_harness.evolution.models import (
    CandidateDecision,
    CandidateStatus,
    EvaluationRecord,
    EvaluationVerdict,
    FeedbackItem,
    SkillCandidate,
    parse_decision,
    parse_feedback_kind,
    parse_rejection,
    parse_verdict,
)
from atlas_harness.kernel.clock import Clock, SystemClock
from atlas_harness.kernel.errors import EventValidationError
from atlas_harness.skills.repository import SkillRepository
from atlas_harness.tools.redaction import redact

FEEDBACK_COLUMNS: tuple[str, ...] = (
    "feedback_id",
    "session_id",
    "kind",
    "content",
    "source_task",
    "source_session_id",
    "tool_name",
    "evidence_json",
    "tags_json",
    "created_at_ms",
)

CANDIDATE_COLUMNS: tuple[str, ...] = (
    "candidate_id",
    "skill_id",
    "version",
    "status",
    "decision",
    "name",
    "description",
    "body",
    "triggers_json",
    "scopes_json",
    "feedback_json",
    "evidence_json",
    "merged_from",
    "reject_reason",
    "created_at_ms",
)

EVALUATION_COLUMNS: tuple[str, ...] = (
    "evaluation_id",
    "candidate_id",
    "skill_id",
    "version",
    "dataset",
    "verdict",
    "stages_json",
    "failed_stages_json",
    "metrics_json",
    "baseline_json",
    "champion_version",
    "task_count",
    "failures_json",
    "notes_json",
    "evaluated_at_ms",
)

_FEEDBACK = ", ".join(FEEDBACK_COLUMNS)
_CANDIDATES = ", ".join(CANDIDATE_COLUMNS)
_EVALUATIONS = ", ".join(EVALUATION_COLUMNS)

_INSERT_FEEDBACK = f"""
INSERT INTO feedback ({_FEEDBACK})
VALUES ({", ".join("?" * len(FEEDBACK_COLUMNS))})
ON CONFLICT(feedback_id) DO UPDATE SET
    kind = excluded.kind,
    content = excluded.content,
    source_task = excluded.source_task,
    source_session_id = excluded.source_session_id,
    tool_name = excluded.tool_name,
    evidence_json = excluded.evidence_json,
    tags_json = excluded.tags_json,
    created_at_ms = excluded.created_at_ms
"""

_INSERT_CANDIDATE = f"""
INSERT INTO skill_candidates ({_CANDIDATES})
VALUES ({", ".join("?" * len(CANDIDATE_COLUMNS))})
ON CONFLICT(candidate_id) DO UPDATE SET
    skill_id = excluded.skill_id,
    version = excluded.version,
    status = excluded.status,
    decision = excluded.decision,
    name = excluded.name,
    description = excluded.description,
    body = excluded.body,
    triggers_json = excluded.triggers_json,
    scopes_json = excluded.scopes_json,
    feedback_json = excluded.feedback_json,
    evidence_json = excluded.evidence_json,
    merged_from = excluded.merged_from,
    reject_reason = excluded.reject_reason,
    created_at_ms = excluded.created_at_ms
"""

_INSERT_EVALUATION = f"""
INSERT INTO candidate_evaluations ({_EVALUATIONS})
VALUES ({", ".join("?" * len(EVALUATION_COLUMNS))})
ON CONFLICT(evaluation_id) DO UPDATE SET
    verdict = excluded.verdict,
    stages_json = excluded.stages_json,
    failed_stages_json = excluded.failed_stages_json,
    metrics_json = excluded.metrics_json,
    baseline_json = excluded.baseline_json,
    champion_version = excluded.champion_version,
    task_count = excluded.task_count,
    failures_json = excluded.failures_json,
    notes_json = excluded.notes_json,
    evaluated_at_ms = excluded.evaluated_at_ms
"""


class EvolutionRepository:
    """Write and read the three M7 record types over the shared SQLite connection."""

    def __init__(
        self,
        store: EventStore,
        *,
        skills: SkillRepository | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.store = store
        self.skills = skills or SkillRepository(store, clock=clock)
        self._clock = clock or SystemClock()

    @property
    def _connection(self) -> sqlite3.Connection:
        return self.store.index.connection

    # ------------------------------------------------------------------- feedback

    def record_feedback(
        self,
        item: FeedbackItem,
        *,
        session_id: str,
        operation_id: str | None = None,
    ) -> FeedbackItem:
        """Store one piece of feedback.

        The content is redacted at the write. Feedback is durable and re-enters
        prompts through the candidates it produces, so a secret pasted into a
        correction would otherwise outlive every downstream filter.
        """

        stored = item.model_copy(
            update={
                "content": redact(item.content),
                "created_at_ms": item.created_at_ms or self._clock.now_ms(),
            }
        )
        self.store.append_new(
            EventType.FEEDBACK_RECORDED,
            session_id=session_id,
            operation_id=operation_id,
            payload=stored.to_payload(),
        )
        self._index_feedback(stored, session_id=session_id)
        return stored

    def feedback(self, feedback_id: str) -> FeedbackItem | None:
        row = self._connection.execute(
            f"SELECT {_FEEDBACK} FROM feedback WHERE feedback_id = ?",
            (feedback_id,),
        ).fetchone()
        return None if row is None else feedback_from_row(row)

    def all_feedback(self, *, kind: str | None = None) -> list[FeedbackItem]:
        sql = f"SELECT {_FEEDBACK} FROM feedback"
        params: tuple[object, ...] = ()
        if kind is not None:
            sql += " WHERE kind = ?"
            params = (kind,)
        sql += " ORDER BY created_at_ms, feedback_id"
        rows = self._connection.execute(sql, params).fetchall()
        return [feedback_from_row(row) for row in rows]

    # ------------------------------------------------------------------ candidates

    def propose(
        self,
        candidate: SkillCandidate,
        *,
        session_id: str,
        operation_id: str | None = None,
    ) -> SkillCandidate:
        """Record a candidate and register its skill version at ``candidate`` status.

        Registration happens here rather than at promotion time because promotion is a
        lifecycle transition and a transition needs something to transition *from*.
        Registering at ``candidate`` keeps it out of every prompt -- only ``active`` is
        injectable -- while making it a real version the evaluator can measure.
        """

        stored = candidate.model_copy(
            update={
                "body": redact(candidate.body),
                "description": redact(candidate.description),
                "status": CandidateStatus.PROPOSED,
                "created_at_ms": candidate.created_at_ms or self._clock.now_ms(),
            }
        )
        self.store.append_new(
            EventType.SKILL_CANDIDATE_PROPOSED,
            session_id=session_id,
            operation_id=operation_id,
            payload=stored.to_payload(),
        )
        self._index_candidate(stored)
        if self.skills.get(stored.skill_id, stored.version) is None:
            self.skills.register(
                stored.to_skill_record(),
                session_id=session_id,
                operation_id=operation_id,
            )
        return stored

    def reject(
        self,
        candidate_id: str,
        reason: str,
        *,
        session_id: str,
        operation_id: str | None = None,
        detail: str | None = None,
        skill_id: str | None = None,
    ) -> None:
        """Record that a candidate was refused before evaluation.

        A rejection is written even when the candidate was never stored, which is the
        normal case for one that failed the schema check. The event is the audit trail
        for "we saw this and said no", and dropping it silently would make a refused
        proposal indistinguishable from one that was never made.
        """

        checked = parse_rejection(reason)
        existing = self.candidate(candidate_id)
        self.store.append_new(
            EventType.SKILL_CANDIDATE_REJECTED,
            session_id=session_id,
            operation_id=operation_id,
            payload={
                "candidate_id": candidate_id,
                "skill_id": skill_id or (None if existing is None else existing.skill_id),
                "reason": checked,
                "detail": detail,
            },
        )
        if existing is not None:
            self._index_candidate(
                existing.model_copy(
                    update={
                        "status": CandidateStatus.REJECTED,
                        "decision": CandidateDecision.REJECT,
                        "reject_reason": checked,
                    }
                )
            )

    def candidate(self, candidate_id: str) -> SkillCandidate | None:
        row = self._connection.execute(
            f"SELECT {_CANDIDATES} FROM skill_candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        return None if row is None else candidate_from_row(row)

    def require_candidate(self, candidate_id: str) -> SkillCandidate:
        found = self.candidate(candidate_id)
        if found is None:
            raise EventValidationError(
                "unknown candidate",
                details={"candidate_id": candidate_id},
            )
        return found

    def candidates(self, *, status: CandidateStatus | None = None) -> list[SkillCandidate]:
        sql = f"SELECT {_CANDIDATES} FROM skill_candidates"
        params: tuple[object, ...] = ()
        if status is not None:
            sql += " WHERE status = ?"
            params = (status.value,)
        sql += " ORDER BY created_at_ms, candidate_id"
        rows = self._connection.execute(sql, params).fetchall()
        return [candidate_from_row(row) for row in rows]

    def pending(self) -> list[SkillCandidate]:
        """The pending window: proposed, bound to evidence, not yet measured."""

        return self.candidates(status=CandidateStatus.PROPOSED)

    # ----------------------------------------------------------------- evaluations

    def record_evaluation(
        self,
        record: EvaluationRecord,
        *,
        session_id: str,
        operation_id: str | None = None,
    ) -> EvaluationRecord:
        """Store an evaluation and mark its candidate as measured.

        The candidate moves to ``evaluated`` whatever the verdict. Leaving a failed
        candidate in the pending window would make it look unexamined, and the next
        operator to run the pending list would measure it again for no reason.
        """

        stored = record.model_copy(
            update={"evaluated_at_ms": record.evaluated_at_ms or self._clock.now_ms()}
        )
        self.store.append_new(
            EventType.CANDIDATE_EVALUATED,
            session_id=session_id,
            operation_id=operation_id,
            payload=stored.to_payload(),
        )
        self._index_evaluation(stored)
        existing = self.candidate(stored.candidate_id)
        if existing is not None and existing.status is CandidateStatus.PROPOSED:
            self._index_candidate(existing.model_copy(update={"status": CandidateStatus.EVALUATED}))
        return stored

    def evaluation(self, evaluation_id: str) -> EvaluationRecord | None:
        row = self._connection.execute(
            f"SELECT {_EVALUATIONS} FROM candidate_evaluations WHERE evaluation_id = ?",
            (evaluation_id,),
        ).fetchone()
        return None if row is None else evaluation_from_row(row)

    def evaluations_for(self, candidate_id: str) -> list[EvaluationRecord]:
        """Every evaluation of one candidate, oldest first.

        A candidate can be measured more than once -- after a model change, or against
        a widened task set -- and keeping all of them is what lets an operator see that
        a promotion rested on the newest verdict rather than the most flattering one.

        Ties on the timestamp break on ``rowid``, which is insertion order and so log
        order, including after a rebuild. Breaking on ``evaluation_id`` instead would
        order two same-millisecond runs by an opaque string, and the newest verdict is
        the one that has to decide.
        """

        rows = self._connection.execute(
            f"SELECT {_EVALUATIONS} FROM candidate_evaluations WHERE candidate_id = ?"
            " ORDER BY evaluated_at_ms, rowid",
            (candidate_id,),
        ).fetchall()
        return [evaluation_from_row(row) for row in rows]

    def latest_evaluation(self, candidate_id: str) -> EvaluationRecord | None:
        found = self.evaluations_for(candidate_id)
        return found[-1] if found else None

    def mark_promoted(self, candidate_id: str) -> None:
        """Reflect a promotion in the candidate's projection.

        No event: :mod:`atlas_harness.evolution.champion` already wrote
        ``champion_promoted``, and a second event for the same fact would let a replay
        produce two promotions from one.
        """

        existing = self.candidate(candidate_id)
        if existing is not None:
            self._index_candidate(existing.model_copy(update={"status": CandidateStatus.PROMOTED}))

    # -------------------------------------------------------------------- rebuild

    def rebuild(self, session_id: str) -> int:
        """Throw away this session's rows and refill them from the log."""

        events = self.store.read_events(session_id)
        connection = self._connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.cursor()
            cursor.execute("DELETE FROM feedback WHERE session_id = ?", (session_id,))
            for event in events:
                payload = event.payload.model_dump(mode="python")
                if event.event_type is EventType.SKILL_CANDIDATE_PROPOSED:
                    cursor.execute(
                        "DELETE FROM skill_candidates WHERE candidate_id = ?",
                        (str(payload["candidate_id"]),),
                    )
                elif event.event_type is EventType.CANDIDATE_EVALUATED:
                    cursor.execute(
                        "DELETE FROM candidate_evaluations WHERE evaluation_id = ?",
                        (str(payload["evaluation_id"]),),
                    )
            connection.execute("COMMIT")
        except BaseException:
            self._rollback()
            raise
        return self._replay(events, session_id=session_id)

    def _replay(self, events: Iterable[Event], *, session_id: str) -> int:
        written = 0
        promoted: set[str] = set()
        for event in events:
            payload = event.payload.model_dump(mode="python")
            if event.event_type is EventType.FEEDBACK_RECORDED:
                self._index_feedback(feedback_from_payload(payload), session_id=session_id)
                written += 1
            elif event.event_type is EventType.SKILL_CANDIDATE_PROPOSED:
                self._index_candidate(candidate_from_payload(payload))
                written += 1
            elif event.event_type is EventType.SKILL_CANDIDATE_REJECTED:
                existing = self.candidate(str(payload["candidate_id"]))
                if existing is not None:
                    self._index_candidate(
                        existing.model_copy(
                            update={
                                "status": CandidateStatus.REJECTED,
                                "decision": CandidateDecision.REJECT,
                                "reject_reason": str(payload.get("reason") or "schema"),
                            }
                        )
                    )
            elif event.event_type is EventType.CANDIDATE_EVALUATED:
                record = evaluation_from_payload(payload)
                self._index_evaluation(record)
                written += 1
                existing = self.candidate(record.candidate_id)
                if existing is not None and existing.status is CandidateStatus.PROPOSED:
                    self._index_candidate(
                        existing.model_copy(update={"status": CandidateStatus.EVALUATED})
                    )
            elif event.event_type is EventType.CHAMPION_PROMOTED:
                candidate_id = payload.get("candidate_id")
                if candidate_id:
                    promoted.add(str(candidate_id))
        for candidate_id in promoted:
            self.mark_promoted(candidate_id)
        return written

    # --------------------------------------------------------------------- index

    def _index_feedback(self, item: FeedbackItem, *, session_id: str) -> None:
        self._write(
            _INSERT_FEEDBACK,
            (
                item.feedback_id,
                session_id,
                item.kind.value,
                item.content,
                item.source_task,
                item.source_session_id,
                item.tool_name,
                _dump(item.evidence_refs),
                _dump(item.tags),
                item.created_at_ms,
            ),
        )

    def _index_candidate(self, candidate: SkillCandidate) -> None:
        self._write(
            _INSERT_CANDIDATE,
            (
                candidate.candidate_id,
                candidate.skill_id,
                candidate.version,
                candidate.status.value,
                candidate.decision.value,
                candidate.name,
                candidate.description,
                candidate.body,
                _dump(candidate.triggers),
                _dump(candidate.required_scopes),
                _dump(candidate.feedback_refs),
                _dump(candidate.evidence_refs),
                candidate.merged_from,
                candidate.reject_reason,
                candidate.created_at_ms,
            ),
        )

    def _index_evaluation(self, record: EvaluationRecord) -> None:
        self._write(
            _INSERT_EVALUATION,
            (
                record.evaluation_id,
                record.candidate_id,
                record.skill_id,
                record.version,
                record.dataset,
                record.verdict.value,
                _dump(record.stages),
                _dump(record.failed_stages),
                json.dumps(record.metrics.model_dump(mode="python"), ensure_ascii=False),
                (
                    None
                    if record.baseline_metrics is None
                    else json.dumps(
                        record.baseline_metrics.model_dump(mode="python"), ensure_ascii=False
                    )
                ),
                record.champion_version,
                record.task_count,
                _dump(record.failures),
                _dump(record.notes),
                record.evaluated_at_ms,
            ),
        )

    def _write(self, sql: str, params: tuple[Any, ...]) -> None:
        connection = self._connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(sql, params)
            connection.execute("COMMIT")
        except BaseException:
            self._rollback()
            raise

    def _rollback(self) -> None:
        try:
            self._connection.execute("ROLLBACK")
        except sqlite3.Error:  # nothing was open
            pass


def _dump(values: Sequence[str]) -> str:
    return json.dumps(list(values), ensure_ascii=False)


def _load(value: Any) -> tuple[str, ...]:
    return tuple(str(item) for item in json.loads(str(value or "[]")))


def _optional(value: Any) -> str | None:
    return None if value is None else str(value)


def feedback_from_row(row: Sequence[Any]) -> FeedbackItem:
    return FeedbackItem(
        feedback_id=str(row[0]),
        kind=parse_feedback_kind(str(row[2])),
        content=str(row[3] or ""),
        source_task=_optional(row[4]),
        source_session_id=_optional(row[5]),
        tool_name=_optional(row[6]),
        evidence_refs=_load(row[7]),
        tags=_load(row[8]),
        created_at_ms=int(row[9] or 0),
    )


def feedback_from_payload(payload: dict[str, Any]) -> FeedbackItem:
    return FeedbackItem(
        feedback_id=str(payload["feedback_id"]),
        kind=parse_feedback_kind(str(payload.get("kind") or "correction")),
        content=str(payload.get("content") or ""),
        source_task=_optional(payload.get("source_task")),
        source_session_id=_optional(payload.get("source_session_id")),
        tool_name=_optional(payload.get("tool_name")),
        evidence_refs=tuple(str(item) for item in payload.get("evidence_refs") or ()),
        tags=tuple(str(item) for item in payload.get("tags") or ()),
        created_at_ms=int(payload.get("created_at_ms") or 0),
    )


def candidate_from_row(row: Sequence[Any]) -> SkillCandidate:
    return SkillCandidate(
        candidate_id=str(row[0]),
        skill_id=str(row[1]),
        version=str(row[2]),
        status=CandidateStatus(str(row[3])),
        decision=parse_decision(str(row[4])),
        name=_optional(row[5]),
        description=str(row[6] or ""),
        body=str(row[7] or ""),
        triggers=_load(row[8]),
        required_scopes=_load(row[9]),
        feedback_refs=_load(row[10]),
        evidence_refs=_load(row[11]),
        merged_from=_optional(row[12]),
        reject_reason=_optional(row[13]),
        created_at_ms=int(row[14] or 0),
    )


def candidate_from_payload(payload: dict[str, Any]) -> SkillCandidate:
    return SkillCandidate(
        candidate_id=str(payload["candidate_id"]),
        skill_id=str(payload["skill_id"]),
        version=str(payload.get("version") or "0.1.0"),
        status=CandidateStatus.PROPOSED,
        decision=parse_decision(str(payload.get("decision") or "add")),
        name=_optional(payload.get("name")),
        description=str(payload.get("description") or ""),
        body=str(payload.get("body") or ""),
        triggers=tuple(str(item) for item in payload.get("triggers") or ()),
        required_scopes=tuple(str(item) for item in payload.get("required_scopes") or ()),
        feedback_refs=tuple(str(item) for item in payload.get("feedback_refs") or ()),
        evidence_refs=tuple(str(item) for item in payload.get("evidence_refs") or ()),
        merged_from=_optional(payload.get("merged_from")),
        created_at_ms=int(payload.get("created_at_ms") or 0),
    )


def evaluation_from_row(row: Sequence[Any]) -> EvaluationRecord:
    return EvaluationRecord(
        evaluation_id=str(row[0]),
        candidate_id=str(row[1]),
        skill_id=str(row[2]),
        version=str(row[3]),
        dataset=str(row[4] or ""),
        verdict=parse_verdict(str(row[5])),
        stages=_load(row[6]),
        failed_stages=_load(row[7]),
        metrics=EvaluationMetrics(**json.loads(str(row[8] or "{}"))),
        baseline_metrics=(None if row[9] is None else EvaluationMetrics(**json.loads(str(row[9])))),
        champion_version=_optional(row[10]),
        task_count=int(row[11] or 0),
        failures=_load(row[12]),
        notes=_load(row[13]),
        evaluated_at_ms=int(row[14] or 0),
    )


def evaluation_from_payload(payload: dict[str, Any]) -> EvaluationRecord:
    baseline = payload.get("baseline_metrics")
    metrics = payload.get("metrics") or {}
    return EvaluationRecord(
        evaluation_id=str(payload["evaluation_id"]),
        candidate_id=str(payload["candidate_id"]),
        skill_id=str(payload["skill_id"]),
        version=str(payload.get("version") or "0.1.0"),
        dataset=str(payload.get("dataset") or ""),
        verdict=parse_verdict(str(payload.get("verdict") or EvaluationVerdict.FAIL.value)),
        stages=tuple(str(item) for item in payload.get("stages") or ()),
        failed_stages=tuple(str(item) for item in payload.get("failed_stages") or ()),
        metrics=EvaluationMetrics(**metrics),
        baseline_metrics=None if baseline is None else EvaluationMetrics(**baseline),
        champion_version=_optional(payload.get("champion_version")),
        task_count=int(payload.get("task_count") or 0),
        failures=tuple(str(item) for item in payload.get("failures") or ()),
        notes=tuple(str(item) for item in payload.get("notes") or ()),
        evaluated_at_ms=int(payload.get("evaluated_at_ms") or 0),
    )
