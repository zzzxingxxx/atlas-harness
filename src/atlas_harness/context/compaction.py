"""Structured compaction: replace the prompt, keep the record.

Compaction rewrites what the *model* sees. It never deletes an event, a diff or
an artifact — those stay in the log, and the summary that takes their place in
the prompt carries references back to them. That asymmetry is the whole point:
the context is a cache of the log, so the cache can be rebuilt from the record
but never the reverse.

The summary is derived from the log rather than asked of the model. A model
summarizing its own truncated transcript is one more chance to lose the blocker
that mattered, and it costs a request; folding events is deterministic and free.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from atlas_harness.context.tokens import ContextBudget, ContextPressure
from atlas_harness.events.models import (
    COMPACTION_REASONS,
    ContextCompacted,
    ContextCompactPending,
    Event,
    EventType,
)
from atlas_harness.events.reducer import OperationState, SessionState
from atlas_harness.events.store import EventStore
from atlas_harness.kernel.errors import EventValidationError
from atlas_harness.model.protocol import ModelMessage, Role
from atlas_harness.tools.redaction import redact

REASON_MANUAL = "manual"
"""An operator ran ``atlas compact`` or the model called the compact tool."""

REASON_THRESHOLD = "threshold"
"""The automatic mark was crossed at an iteration boundary."""

REASON_OVERFLOW = "overflow"
"""The force mark was crossed; evidence is reduced to references."""

MAX_SUMMARY_ITEMS = 12
"""Per-list ceiling. A summary that grows without bound defeats its purpose."""

MAX_ITEM_CHARS = 400
"""One entry is a reminder, not a transcript. Longer text is cut."""


def _clip(text: str, limit: int = MAX_ITEM_CHARS) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[: limit - 1]}…"


def _tail(values: Sequence[str], limit: int = MAX_SUMMARY_ITEMS) -> list[str]:
    """Keep the most recent entries, de-duplicated, order preserved."""

    seen: set[str] = set()
    kept: list[str] = []
    for value in reversed(values):
        if not value or value in seen:
            continue
        seen.add(value)
        kept.append(value)
        if len(kept) >= limit:
            break
    kept.reverse()
    return kept


class CompactionSummary(BaseModel):
    """The structured object that replaces the trimmed part of the prompt.

    Every field the plan names is present even when empty, so a reader never has
    to tell "nothing was found" apart from "the key was dropped".
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    current_objective: str = ""
    task_progress: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    tool_lessons: list[str] = Field(default_factory=list)
    failed_paths: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.current_objective and not any(
            (
                self.task_progress,
                self.blockers,
                self.next_actions,
                self.decisions,
                self.tool_lessons,
                self.failed_paths,
                self.evidence_refs,
                self.open_questions,
            )
        )

    def as_text(self) -> str:
        """Render as the single message that stands in for what was dropped.

        Plain labelled sections rather than JSON: the model reads this as
        context, and prose survives a re-tokenization more gracefully than a
        structure it might try to parse or imitate.
        """

        sections: list[str] = ["[compacted context]"]
        if self.current_objective:
            sections.append(f"Current objective: {self.current_objective}")
        for label, values in (
            ("Progress", self.task_progress),
            ("Blockers", self.blockers),
            ("Next actions", self.next_actions),
            ("Decisions", self.decisions),
            ("Tool lessons", self.tool_lessons),
            ("Failed paths", self.failed_paths),
            ("Evidence", self.evidence_refs),
            ("Open questions", self.open_questions),
        ):
            if values:
                body = "\n".join(f"  - {value}" for value in values)
                sections.append(f"{label}:\n{body}")
        return "\n".join(sections)

    def as_message(self) -> ModelMessage:
        return ModelMessage(role=Role.USER, content=self.as_text())


class CompactionResult(BaseModel):
    """What one compaction did, for the caller and for the event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: str
    summary: CompactionSummary
    messages: tuple[ModelMessage, ...] = ()
    replaced_messages: int = 0
    used_tokens: int = 0
    limit_tokens: int = 0
    freed_tokens: int = 0
    recorded: bool = True
    """False when the transcript was already at its floor and no event was written.

    The caller needs to tell "compacted" from "there was nothing left to compact":
    the second case must not be retried every iteration, and must not leave the log
    claiming a compaction that freed nothing."""

    @property
    def ratio(self) -> float:
        return 0.0 if self.limit_tokens == 0 else self.used_tokens / self.limit_tokens


class Compactor:
    """Build compaction summaries from the log and record them as events.

    The store is the only dependency: everything the summary needs is already
    persisted, so compaction never has to be told what happened.
    """

    def __init__(self, store: EventStore, *, budget: ContextBudget | None = None) -> None:
        self._store = store
        self._budget = budget or ContextBudget()

    @property
    def store(self) -> EventStore:
        return self._store

    @property
    def budget(self) -> ContextBudget:
        return self._budget

    # ----------------------------------------------------------------- summary

    def summarize(
        self,
        session_id: str,
        *,
        operation_id: str | None = None,
        objective: str = "",
        state: SessionState | None = None,
    ) -> CompactionSummary:
        """Fold one session's events into the structured summary.

        Scoped to an operation when one is given, so a compaction inside a lane
        does not pull in another lane's blockers.
        """

        projected = state if state is not None else self._store.load_state(session_id)
        events = self._store.read_events(session_id)
        if operation_id is not None:
            events = [event for event in events if event.operation_id == operation_id]

        progress: list[str] = []
        blockers: list[str] = []
        decisions: list[str] = []
        lessons: list[str] = []
        failed: list[str] = []
        evidence: list[str] = []
        questions: list[str] = []

        for event in events:
            payload = event.payload.model_dump(mode="python")
            kind = event.event_type
            if kind is EventType.TOOL_RESULT:
                self._fold_tool_result(payload, progress, lessons, failed, evidence)
            elif kind is EventType.ARTIFACT_STORED:
                reference = self._artifact_reference(payload)
                if reference:
                    evidence.append(reference)
            elif kind is EventType.PROVIDER_ERROR:
                blockers.append(
                    _clip(f"provider {payload.get('error_code')}: {payload.get('error')}")
                )
            elif kind is EventType.OPERATION_SUSPENDED:
                blockers.append(_clip(f"suspended: {payload.get('reason')}"))
                for call_id in payload.get("pending_tool_call_ids") or []:
                    questions.append(_clip(f"confirm tool call {call_id}?"))
            elif kind is EventType.APPROVAL_REQUESTED:
                questions.append(_clip(f"approval pending: {payload.get('reason') or 'unnamed'}"))
            elif kind is EventType.OPERATION_FAILED:
                blockers.append(_clip(f"operation failed: {payload.get('error')}"))
            elif kind is EventType.CONTEXT_COMPACTED:
                # Carry a previous compaction's objective forward so a second
                # compaction does not lose what the first one established.
                decisions.append(_clip(f"compacted earlier ({payload.get('reason')})"))

        operation = self._operation(projected, operation_id)
        return CompactionSummary(
            current_objective=_clip(objective or self._objective(projected, operation)),
            task_progress=_tail(progress),
            blockers=_tail(blockers),
            next_actions=_tail(self._next_actions(projected, operation)),
            decisions=_tail(decisions),
            tool_lessons=_tail(lessons),
            failed_paths=_tail(failed),
            evidence_refs=_tail(evidence),
            open_questions=_tail(questions),
        )

    def _fold_tool_result(
        self,
        payload: dict[str, object],
        progress: list[str],
        lessons: list[str],
        failed: list[str],
        evidence: list[str],
    ) -> None:
        """Turn one tool result into progress, or into a lesson if it failed."""

        tool_name = str(payload.get("tool_name") or "tool")
        success = bool(payload.get("success"))
        if success:
            progress.append(_clip(f"{tool_name} succeeded"))
            for path in self._paths_in(payload.get("output")):
                evidence.append(_clip(path))
            return
        error = payload.get("error") or payload.get("error_code") or "failed"
        lessons.append(_clip(f"{tool_name} failed: {error}"))
        details = payload.get("details")
        if isinstance(details, dict):
            failed_path = details.get("path")
            if isinstance(failed_path, str) and failed_path:
                failed.append(_clip(failed_path))

    def _paths_in(self, output: object) -> list[str]:
        """Pull file references out of a tool output without knowing the tool.

        Only the conventional ``path`` / ``paths`` keys are read. Guessing more
        aggressively would put arbitrary model-influenced strings into the
        evidence slot.
        """

        if not isinstance(output, dict):
            return []
        found: list[str] = []
        single = output.get("path")
        if isinstance(single, str) and single:
            found.append(single)
        many = output.get("paths")
        if isinstance(many, list):
            found.extend(item for item in many if isinstance(item, str) and item)
        matches = output.get("matches")
        if isinstance(matches, list):
            for match in matches:
                if isinstance(match, dict):
                    where = match.get("path")
                    if isinstance(where, str) and where:
                        found.append(where)
        return found

    def _artifact_reference(self, payload: dict[str, object]) -> str:
        artifact_id = payload.get("artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id:
            return ""
        kind = payload.get("kind") or "artifact"
        size = payload.get("size") or 0
        return _clip(f"{kind} {artifact_id} ({size} bytes)")

    def _operation(self, state: SessionState, operation_id: str | None) -> OperationState | None:
        if operation_id is not None:
            return state.operations.get(operation_id)
        unfinished = state.unfinished_operation_ids
        if unfinished:
            return state.operations.get(unfinished[0])
        return None

    def _objective(self, state: SessionState, operation: OperationState | None) -> str:
        if operation is not None and operation.name:
            return f"{operation.name} ({operation.operation_id})"
        return state.title or f"session {state.session_id}"

    def _next_actions(self, state: SessionState, operation: OperationState | None) -> list[str]:
        """What the log says is still owed. Not a guess about intent."""

        actions: list[str] = []
        if operation is None:
            return actions
        for call_id in operation.pending_tool_call_ids:
            actions.append(_clip(f"resolve pending tool call {call_id}"))
        for call in operation.tool_calls.values():
            if call.status == "started":
                actions.append(_clip(f"finish {call.tool_name} ({call.call_id})"))
        for queue in ("steer", "follow_up"):
            for message in operation.pending_queue_messages(queue):
                actions.append(_clip(f"[{queue}] {redact(message.content)}"))
        return actions

    # ------------------------------------------------------------------ events

    def mark_pending(
        self,
        session_id: str,
        *,
        operation_id: str,
        used_tokens: int,
        lane_id: str | None = None,
        iteration: int | None = None,
    ) -> Event:
        """Announce that the soft threshold was crossed, before compacting."""

        return self._store.append_new(
            EventType.CONTEXT_COMPACT_PENDING,
            session_id=session_id,
            operation_id=operation_id,
            lane_id=lane_id or self._store.load_state(session_id).current_lane_id,
            payload=ContextCompactPending(
                used_tokens=used_tokens,
                limit_tokens=self._budget.limit_tokens,
                ratio=round(self._budget.ratio(used_tokens), 4),
                iteration=iteration,
            ),
        )

    def compact(
        self,
        session_id: str,
        *,
        operation_id: str,
        messages: Sequence[ModelMessage],
        used_tokens: int,
        reason: str = REASON_THRESHOLD,
        keep_recent: int = 4,
        objective: str = "",
        lane_id: str | None = None,
        iteration: int | None = None,
        require_replacement: bool = False,
    ) -> CompactionResult:
        """Replace the middle of the transcript with a structured summary.

        The system messages and the most recent ``keep_recent`` turns survive
        verbatim; everything between them becomes one summary message. Recent
        turns are kept because the model needs the thread it was pulling on, and
        the system messages because the fixed slot is never displaced.
        """

        if reason not in COMPACTION_REASONS:
            raise EventValidationError(
                "unknown compaction reason",
                details={"reason": reason, "supported": sorted(COMPACTION_REASONS)},
            )
        summary = self.summarize(session_id, operation_id=operation_id, objective=objective)
        rebuilt, replaced = self._rebuild(messages, summary, keep_recent=keep_recent)
        freed = max(0, len(messages) - len(rebuilt))
        if require_replacement and not replaced:
            # The transcript is already at its floor: system messages plus the tail
            # the caller asked to keep, with nothing in between to summarize. An
            # automatic trigger records nothing here, because pressure stays high
            # and an event per iteration would fill the log with compactions that
            # freed no tokens. A manual trigger still gets its event -- an operator
            # asked, and the answer "there was nothing to do" belongs in the record.
            return CompactionResult(
                reason=reason,
                summary=summary,
                messages=tuple(rebuilt),
                replaced_messages=0,
                used_tokens=used_tokens,
                limit_tokens=self._budget.limit_tokens,
                freed_tokens=0,
                recorded=False,
            )
        self._store.append_new(
            EventType.CONTEXT_COMPACTED,
            session_id=session_id,
            operation_id=operation_id,
            lane_id=lane_id or self._store.load_state(session_id).current_lane_id,
            payload=ContextCompacted(
                reason=reason,
                used_tokens=used_tokens,
                limit_tokens=self._budget.limit_tokens,
                ratio=round(self._budget.ratio(used_tokens), 4),
                freed_tokens=freed,
                replaced_messages=replaced,
                iteration=iteration,
                # Listed rather than splatted: a ``**dict[str, object]`` would put
                # every one of these past the type checker, and this payload is
                # exactly the place a silently-dropped field would go unnoticed.
                current_objective=summary.current_objective,
                task_progress=list(summary.task_progress),
                blockers=list(summary.blockers),
                next_actions=list(summary.next_actions),
                decisions=list(summary.decisions),
                tool_lessons=list(summary.tool_lessons),
                failed_paths=list(summary.failed_paths),
                evidence_refs=list(summary.evidence_refs),
                open_questions=list(summary.open_questions),
            ),
        )
        return CompactionResult(
            reason=reason,
            summary=summary,
            messages=tuple(rebuilt),
            replaced_messages=replaced,
            used_tokens=used_tokens,
            limit_tokens=self._budget.limit_tokens,
            freed_tokens=freed,
        )

    def _rebuild(
        self,
        messages: Sequence[ModelMessage],
        summary: CompactionSummary,
        *,
        keep_recent: int,
    ) -> tuple[list[ModelMessage], int]:
        """Assemble system messages + summary + the recent tail.

        The tail is extended backwards past any ``tool`` message whose assistant
        call would otherwise be cut away: a tool result without its request is
        invalid for several providers, so the split point moves rather than
        producing a transcript that cannot be sent.
        """

        system = [message for message in messages if message.role is Role.SYSTEM]
        body = [message for message in messages if message.role is not Role.SYSTEM]
        if not body:
            return list(messages), 0

        split = max(0, len(body) - max(0, keep_recent))
        split = self._safe_split(body, split)
        replaced = split
        tail = body[split:]
        if replaced == 0:
            return list(messages), 0
        return [*system, summary.as_message(), *tail], replaced

    def _safe_split(self, body: Sequence[ModelMessage], split: int) -> int:
        """Move a split point back until the tail owns its tool calls."""

        index = min(split, len(body))
        while index > 0 and body[index:] and body[index].role is Role.TOOL:
            index -= 1
        return index


def compaction_reason_for(pressure: ContextPressure) -> str:
    """Map a pressure level onto the reason recorded in the event."""

    if pressure is ContextPressure.FORCE:
        return REASON_OVERFLOW
    return REASON_THRESHOLD


def summary_from_event(payload: dict[str, object]) -> CompactionSummary:
    """Rebuild a summary from a persisted ``context_compacted`` payload."""

    fields = CompactionSummary.model_fields
    known = {key: value for key, value in payload.items() if key in fields}
    return CompactionSummary.model_validate(known)
