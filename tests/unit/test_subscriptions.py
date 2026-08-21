import asyncio
import logging

import pytest

from atlas_harness.events import Event, EventBus, EventType
from atlas_harness.kernel import LifecycleError


def make_event(session_id: str = "ses_a", seq: int = 1) -> Event:
    return Event.create(
        EventType.ASSISTANT_MESSAGE,
        session_id=session_id,
        seq=seq,
        payload={"content": f"m{seq}"},
    )


def test_subscriber_receives_published_events() -> None:
    bus = EventBus()
    seen: list[str] = []
    bus.subscribe(lambda event: seen.append(event.event_id))

    first = make_event()
    bus.publish(first)

    assert seen == [first.event_id]
    assert bus.subscriber_count == 1


def test_subscription_can_filter_by_session() -> None:
    bus = EventBus()
    seen: list[str] = []
    bus.subscribe(lambda event: seen.append(event.session_id), session_id="ses_a")

    bus.publish(make_event("ses_a"))
    bus.publish(make_event("ses_b"))

    assert seen == ["ses_a"]


def test_closing_a_subscription_stops_delivery() -> None:
    bus = EventBus()
    seen: list[str] = []
    subscription = bus.subscribe(lambda event: seen.append(event.event_id))

    subscription.close()
    subscription.close()
    bus.publish(make_event())

    assert seen == []
    assert subscription.closed
    assert bus.subscriber_count == 0


def test_subscription_context_manager() -> None:
    bus = EventBus()
    seen: list[str] = []

    with bus.subscribe(lambda event: seen.append(event.event_id)):
        bus.publish(make_event())
    bus.publish(make_event(seq=2))

    assert len(seen) == 1


def test_failing_subscriber_does_not_break_publishing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bus = EventBus()
    seen: list[str] = []

    def boom(event: Event) -> None:
        raise RuntimeError("subscriber exploded")

    bus.subscribe(boom)
    bus.subscribe(lambda event: seen.append(event.event_id))

    with caplog.at_level(logging.ERROR):
        bus.publish(make_event())

    assert len(seen) == 1
    assert "event subscriber failed" in caplog.text


def test_closing_the_bus_ends_subscriptions() -> None:
    bus = EventBus()
    subscription = bus.subscribe(lambda event: None)

    bus.close()

    assert bus.closed
    assert subscription.closed
    assert bus.subscriber_count == 0
    with pytest.raises(LifecycleError):
        bus.subscribe(lambda event: None)
    with pytest.raises(LifecycleError):
        bus.stream()


def test_publishing_to_a_closed_bus_is_ignored() -> None:
    bus = EventBus()
    bus.close()

    bus.publish(make_event())


async def test_stream_yields_events_until_closed() -> None:
    bus = EventBus()
    stream = bus.stream()
    first = make_event(seq=1)
    second = make_event(seq=2)

    bus.publish(first)
    bus.publish(second)
    stream.close()

    received = [event.event_id async for event in stream]

    assert received == [first.event_id, second.event_id]
    assert stream.closed
    assert bus.subscriber_count == 0


async def test_stream_filters_by_session() -> None:
    bus = EventBus()
    async with bus.stream(session_id="ses_a") as stream:
        bus.publish(make_event("ses_b"))
        bus.publish(make_event("ses_a"))
        stream.close()
        received = [event.session_id async for event in stream]

    assert received == ["ses_a"]


async def test_stream_waits_for_the_next_event() -> None:
    bus = EventBus()
    stream = bus.stream()
    event = make_event()

    async def publish_later() -> None:
        await asyncio.sleep(0)
        bus.publish(event)

    task = asyncio.create_task(publish_later())
    received = await anext(aiter(stream))
    await task
    await stream.aclose()

    assert received.event_id == event.event_id


async def test_closing_the_bus_ends_open_streams() -> None:
    bus = EventBus()
    stream = bus.stream()

    bus.close()

    assert [event async for event in stream] == []
