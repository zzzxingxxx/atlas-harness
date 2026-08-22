"""Queue durability: what the log says must match what the loop will consume."""

from __future__ import annotations

from typing import Any

import pytest
from tests.conftest import DEMO_OPERATION_ID, DEMO_SESSION_ID

from atlas_harness.agent.queues import (
    CONSUMPTION_ORDER,
    MAX_MESSAGE_CHARS,
    QueueManager,
    QueueName,
    QueueRequest,
)
from atlas_harness.events import EventStore, EventType


@pytest.fixture
def queues(seeded: tuple[EventStore, str]) -> QueueManager:
    store, session_id = seeded
    return QueueManager(store, session_id=session_id, operation_id=DEMO_OPERATION_ID)


def payloads(store: EventStore, event_type: EventType) -> list[dict[str, Any]]:
    """Read payloads back as plain dicts, the shape the JSONL line actually holds."""

    return [
        event.payload.model_dump(mode="json")
        for event in store.read_events(DEMO_SESSION_ID)
        if event.event_type is event_type
    ]


def test_next_run_is_not_in_the_consumption_order() -> None:
    assert CONSUMPTION_ORDER == (QueueName.STEER, QueueName.FOLLOW_UP)
    assert QueueName.NEXT_RUN not in CONSUMPTION_ORDER


def test_enqueue_records_an_event_and_shows_up_as_pending(
    queues: QueueManager, seeded: tuple[EventStore, str]
) -> None:
    store, _ = seeded
    message = queues.enqueue(QueueName.STEER, "prefer the smaller file")

    assert queues.pending(QueueName.STEER) == (message,)
    assert queues.snapshot().steer == 1
    written = payloads(store, EventType.QUEUE_MESSAGE_ENQUEUED)
    assert written == [
        {
            "queue": "steer",
            "message_id": message.message_id,
            "content": "prefer the smaller file",
            "source": "user",
        }
    ]


def test_enqueue_redacts_and_truncates_before_it_reaches_the_log(
    queues: QueueManager, seeded: tuple[EventStore, str]
) -> None:
    store, _ = seeded
    queues.enqueue(QueueName.STEER, "token sk-" + "a" * 40)
    queues.enqueue(QueueName.FOLLOW_UP, "x" * (MAX_MESSAGE_CHARS + 500))

    secret, long = payloads(store, EventType.QUEUE_MESSAGE_ENQUEUED)
    assert "sk-" + "a" * 40 not in str(secret["content"])
    assert len(str(long["content"])) <= MAX_MESSAGE_CHARS


def test_consume_drains_fifo_and_records_each_message(
    queues: QueueManager, seeded: tuple[EventStore, str]
) -> None:
    store, _ = seeded
    queues.enqueue(QueueName.STEER, "first")
    queues.enqueue(QueueName.STEER, "second")

    drained = queues.consume(QueueName.STEER, iteration=3)

    assert [message.content for message in drained] == ["first", "second"]
    assert queues.pending(QueueName.STEER) == ()
    consumed = payloads(store, EventType.QUEUE_MESSAGE_CONSUMED)
    assert [entry["message_id"] for entry in consumed] == [
        message.message_id for message in drained
    ]
    assert {entry["iteration"] for entry in consumed} == {3}


def test_consume_writes_its_event_before_the_message_leaves_the_deque(
    queues: QueueManager, seeded: tuple[EventStore, str]
) -> None:
    """A crash mid-drain must replay as consumed, never as silently lost."""

    store, _ = seeded
    queues.enqueue(QueueName.STEER, "one")
    queues.enqueue(QueueName.STEER, "two")
    seen: list[int] = []
    original = store.append_new

    def spy(event_type: EventType, **kwargs: object) -> object:
        if event_type is EventType.QUEUE_MESSAGE_CONSUMED:
            seen.append(len(queues.pending(QueueName.STEER)))
        return original(event_type, **kwargs)  # type: ignore[arg-type]

    store.append_new = spy  # type: ignore[method-assign]
    queues.consume(QueueName.STEER)

    assert seen == [2, 1]


def test_consume_all_drains_steer_before_follow_up(queues: QueueManager) -> None:
    queues.enqueue(QueueName.FOLLOW_UP, "follow")
    queues.enqueue(QueueName.STEER, "steer")

    drained = queues.consume_all()

    assert [message.content for message in drained] == ["steer", "follow"]


def test_consume_all_leaves_next_run_untouched(queues: QueueManager) -> None:
    queues.enqueue(QueueName.NEXT_RUN, "later")

    assert queues.consume_all() == ()
    assert queues.snapshot().next_run == 1
    assert queues.has_pending(QueueName.NEXT_RUN)
    assert not queues.has_pending(*CONSUMPTION_ORDER)


def test_consuming_an_empty_queue_writes_nothing(
    queues: QueueManager, seeded: tuple[EventStore, str]
) -> None:
    store, _ = seeded

    assert queues.consume(QueueName.STEER) == ()
    assert payloads(store, EventType.QUEUE_MESSAGE_CONSUMED) == []


def test_hydrate_restores_only_unconsumed_messages(
    queues: QueueManager, seeded: tuple[EventStore, str]
) -> None:
    store, session_id = seeded
    queues.enqueue(QueueName.STEER, "already handled")
    queues.consume(QueueName.STEER)
    survivor = queues.enqueue(QueueName.FOLLOW_UP, "still waiting")
    queues.enqueue(QueueName.NEXT_RUN, "for the next run")

    recovered = QueueManager(store, session_id=session_id, operation_id=DEMO_OPERATION_ID)
    recovered.hydrate(store.load_state(session_id))

    assert recovered.pending(QueueName.STEER) == ()
    assert [message.message_id for message in recovered.pending(QueueName.FOLLOW_UP)] == [
        survivor.message_id
    ]
    assert recovered.snapshot().model_dump() == {"steer": 0, "follow_up": 1, "next_run": 1}


def test_hydrate_clears_stale_in_memory_state(
    queues: QueueManager, seeded: tuple[EventStore, str]
) -> None:
    store, session_id = seeded
    queues.enqueue(QueueName.STEER, "kept")
    other = QueueManager(store, session_id=session_id, operation_id=DEMO_OPERATION_ID)
    other._pending[QueueName.STEER].append(queues.pending(QueueName.STEER)[0])
    other._pending[QueueName.NEXT_RUN].append(queues.pending(QueueName.STEER)[0])

    other.hydrate(store.load_state(session_id))

    assert other.snapshot().model_dump() == {"steer": 1, "follow_up": 0, "next_run": 0}


def test_hydrate_skips_a_queue_name_this_build_does_not_know(
    seeded: tuple[EventStore, str],
) -> None:
    store, session_id = seeded
    store.append_new(
        EventType.QUEUE_MESSAGE_ENQUEUED,
        session_id=session_id,
        operation_id=DEMO_OPERATION_ID,
        payload={"queue": "from_the_future", "message_id": "msg_x", "content": "?"},
    )
    queues = QueueManager(store, session_id=session_id, operation_id=DEMO_OPERATION_ID)

    queues.hydrate(store.load_state(session_id))

    assert queues.snapshot().total == 0


def test_hydrate_without_an_operation_id_finds_nothing(
    seeded: tuple[EventStore, str],
) -> None:
    store, session_id = seeded
    queues = QueueManager(store, session_id=session_id)
    queues.enqueue(QueueName.STEER, "loose message")

    queues.hydrate(store.load_state(session_id))

    assert queues.snapshot().total == 0


def test_snapshot_total_adds_the_three_queues(queues: QueueManager) -> None:
    queues.enqueue(QueueName.STEER, "a")
    queues.enqueue(QueueName.FOLLOW_UP, "b")
    queues.enqueue(QueueName.NEXT_RUN, "c")

    assert queues.snapshot().total == 3


def test_queue_request_defaults_to_steer_and_rejects_empty_content() -> None:
    assert QueueRequest(content="go").queue is QueueName.STEER
    with pytest.raises(ValueError):
        QueueRequest(content="")
