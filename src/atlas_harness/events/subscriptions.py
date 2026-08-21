"""Event subscriptions with explicit close and cancellation.

Delivery happens after an event is durable, so a failing subscriber is logged
and skipped instead of turning a committed event into an append error.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable

from atlas_harness.events.models import Event
from atlas_harness.kernel.errors import LifecycleError

LOGGER = logging.getLogger("atlas_harness.events.subscriptions")

EventHandler = Callable[[Event], None]


class Subscription:
    """Handle for a synchronous subscriber. Closing it stops delivery."""

    def __init__(self, bus: EventBus, handler: EventHandler, session_id: str | None) -> None:
        self._bus = bus
        self.handler = handler
        self.session_id = session_id
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def matches(self, event: Event) -> bool:
        return self.session_id is None or self.session_id == event.session_id

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._bus.detach(self)

    def __enter__(self) -> Subscription:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class EventStream:
    """Unbounded async iterator over published events; ends once closed."""

    def __init__(self, bus: EventBus, session_id: str | None) -> None:
        self._bus = bus
        self.session_id = session_id
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def matches(self, event: Event) -> bool:
        return self.session_id is None or self.session_id == event.session_id

    def offer(self, event: Event) -> None:
        if self._closed:
            return
        self._queue.put_nowait(event)

    def __aiter__(self) -> AsyncIterator[Event]:
        return self

    async def __anext__(self) -> Event:
        item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        return item

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._bus.detach_stream(self)
        self._queue.put_nowait(None)

    async def aclose(self) -> None:
        self.close()

    async def __aenter__(self) -> EventStream:
        return self

    async def __aexit__(self, *_: object) -> None:
        self.close()


class EventBus:
    """Fan out durable events to synchronous handlers and async streams."""

    def __init__(self) -> None:
        self._subscriptions: list[Subscription] = []
        self._streams: list[EventStream] = []
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def subscriber_count(self) -> int:
        return len(self._subscriptions) + len(self._streams)

    def subscribe(self, handler: EventHandler, *, session_id: str | None = None) -> Subscription:
        self._ensure_open()
        subscription = Subscription(self, handler, session_id)
        self._subscriptions.append(subscription)
        return subscription

    def stream(self, *, session_id: str | None = None) -> EventStream:
        self._ensure_open()
        stream = EventStream(self, session_id)
        self._streams.append(stream)
        return stream

    def publish(self, event: Event) -> None:
        if self._closed:
            LOGGER.debug("dropping event %s: bus is closed", event.event_id)
            return
        for subscription in list(self._subscriptions):
            if not subscription.matches(event):
                continue
            try:
                subscription.handler(event)
            except Exception:
                LOGGER.exception("event subscriber failed for event %s", event.event_id)
        for stream in list(self._streams):
            if stream.matches(event):
                stream.offer(event)

    def detach(self, subscription: Subscription) -> None:
        if subscription in self._subscriptions:
            self._subscriptions.remove(subscription)

    def detach_stream(self, stream: EventStream) -> None:
        if stream in self._streams:
            self._streams.remove(stream)

    def close(self) -> None:
        """Stop delivery and end every open stream."""

        self._closed = True
        for subscription in list(self._subscriptions):
            subscription.close()
        for stream in list(self._streams):
            stream.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise LifecycleError("event bus is closed")
