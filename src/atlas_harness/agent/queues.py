"""Three persisted queues that carry work between agent-loop iterations.

A running operation is not a closed box: an operator can steer it, a tool can
ask for a follow-up turn, and a caller can line up the next run before this one
finishes. Each of those arrives on its own queue so the loop can drain them in a
fixed order instead of racing.

Every enqueue and every consume is an event, so a recovered session knows
exactly which messages were already folded into a model request and which are
still waiting. The queues hold no state the log does not.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from atlas_harness.events import DEFAULT_LANE, EventStore, EventType
from atlas_harness.events.reducer import OperationState, SessionState
from atlas_harness.kernel.ids import new_id
from atlas_harness.tools.redaction import redact, truncate_text

MAX_MESSAGE_CHARS = 8_192
"""Queue content is replayed into a model request, so it gets its own budget."""


class QueueName(StrEnum):
    """The three queues, named after when their content is allowed to land."""

    STEER = "steer"
    """Operator guidance for the turn that is about to be built."""

    FOLLOW_UP = "follow_up"
    """Work the runtime discovered mid-operation and owes the model."""

    NEXT_RUN = "next_run"
    """Deferred input: read only after the current operation finishes."""


CONSUMPTION_ORDER: tuple[QueueName, ...] = (QueueName.STEER, QueueName.FOLLOW_UP)
"""Drained before each model request. ``next_run`` is deliberately absent."""


class QueuedMessage(BaseModel):
    """One durable queue entry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: str
    queue: QueueName
    content: str = ""
    source: str = "user"

    def as_payload(self) -> dict[str, Any]:
        return {
            "queue": self.queue.value,
            "message_id": self.message_id,
            "content": self.content,
            "source": self.source,
        }


class QueueSnapshot(BaseModel):
    """Pending counts per queue, used by the CLI and by loop diagnostics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    steer: int = 0
    follow_up: int = 0
    next_run: int = 0

    @property
    def total(self) -> int:
        return self.steer + self.follow_up + self.next_run


class QueueManager:
    """Durable fan-in for one operation.

    In-memory deques mirror the log so the loop does not replay the whole
    session between iterations; :meth:`hydrate` rebuilds them after recovery.
    """

    def __init__(
        self,
        store: EventStore,
        *,
        session_id: str,
        operation_id: str | None = None,
        lane_id: str = DEFAULT_LANE,
    ) -> None:
        self.store = store
        self.session_id = session_id
        self.operation_id = operation_id
        self.lane_id = lane_id
        self._pending: dict[QueueName, deque[QueuedMessage]] = {name: deque() for name in QueueName}

    def enqueue(
        self,
        queue: QueueName,
        content: str,
        *,
        source: str = "user",
        message_id: str | None = None,
    ) -> QueuedMessage:
        """Record one message and make it visible to the next drain."""

        text, _ = truncate_text(redact(content), MAX_MESSAGE_CHARS)
        message = QueuedMessage(
            message_id=message_id or new_id("msg"),
            queue=queue,
            content=text,
            source=source,
        )
        self._append(EventType.QUEUE_MESSAGE_ENQUEUED, message.as_payload())
        self._pending[queue].append(message)
        return message

    def pending(self, queue: QueueName) -> tuple[QueuedMessage, ...]:
        """Peek without consuming. Useful for diagnostics and for the CLI."""

        return tuple(self._pending[queue])

    def has_pending(self, *queues: QueueName) -> bool:
        names = queues or tuple(QueueName)
        return any(self._pending[name] for name in names)

    def consume(
        self, queue: QueueName, *, iteration: int | None = None
    ) -> tuple[QueuedMessage, ...]:
        """Drain one queue and record each message as consumed.

        The event is written before the message leaves the deque, so a crash
        mid-drain replays as consumed rather than as silently lost.
        """

        drained: list[QueuedMessage] = []
        waiting = self._pending[queue]
        while waiting:
            message = waiting[0]
            self._append(
                EventType.QUEUE_MESSAGE_CONSUMED,
                {
                    "queue": queue.value,
                    "message_id": message.message_id,
                    "iteration": iteration,
                },
            )
            waiting.popleft()
            drained.append(message)
        return tuple(drained)

    def consume_all(
        self,
        queues: Iterable[QueueName] = CONSUMPTION_ORDER,
        *,
        iteration: int | None = None,
    ) -> tuple[QueuedMessage, ...]:
        """Drain several queues in the given order, steer first by default."""

        drained: list[QueuedMessage] = []
        for queue in queues:
            drained.extend(self.consume(queue, iteration=iteration))
        return tuple(drained)

    def hydrate(self, state: SessionState) -> None:
        """Rebuild the deques from a replayed projection after recovery."""

        for waiting in self._pending.values():
            waiting.clear()
        operation = self._operation(state)
        if operation is None:
            return
        for queued in operation.queue_messages.values():
            if queued.consumed:
                continue
            try:
                queue = QueueName(queued.queue)
            except ValueError:  # a queue this build does not know; leave it pending in the log
                continue
            self._pending[queue].append(
                QueuedMessage(
                    message_id=queued.message_id,
                    queue=queue,
                    content=queued.content,
                    source=queued.source,
                )
            )

    def snapshot(self) -> QueueSnapshot:
        return QueueSnapshot(
            steer=len(self._pending[QueueName.STEER]),
            follow_up=len(self._pending[QueueName.FOLLOW_UP]),
            next_run=len(self._pending[QueueName.NEXT_RUN]),
        )

    def _operation(self, state: SessionState) -> OperationState | None:
        if self.operation_id is not None:
            return state.operations.get(self.operation_id)
        return None

    def _append(self, event_type: EventType, payload: dict[str, Any]) -> None:
        self.store.append_new(
            event_type,
            session_id=self.session_id,
            payload=payload,
            lane_id=self.lane_id,
            operation_id=self.operation_id,
        )


class QueueRequest(BaseModel):
    """A queue write requested from outside a running loop, e.g. by the CLI."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    queue: QueueName = QueueName.STEER
    content: str = Field(min_length=1)
    source: str = "user"
