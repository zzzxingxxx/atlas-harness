"""Lifecycle and cancellation primitives for the process and future operations."""

import asyncio
from enum import StrEnum

from atlas_harness.kernel.errors import LifecycleError


class LifecycleState(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    CLOSING = "closing"
    CLOSED = "closed"


class Lifecycle:
    """Small state machine shared by app services and graceful shutdown."""

    def __init__(self) -> None:
        self._state = LifecycleState.CREATED
        self._cancel_event = asyncio.Event()

    @property
    def state(self) -> LifecycleState:
        return self._state

    @property
    def cancelled(self) -> bool:
        return self._cancel_event.is_set()

    async def start(self) -> None:
        if self._state is not LifecycleState.CREATED:
            raise LifecycleError(f"cannot start lifecycle in state {self._state}")
        self._state = LifecycleState.RUNNING

    async def request_cancel(self) -> None:
        if self._state in {LifecycleState.CLOSED, LifecycleState.CLOSING}:
            return
        self._cancel_event.set()

    async def wait_cancelled(self) -> None:
        await self._cancel_event.wait()

    async def close(self) -> None:
        if self._state is LifecycleState.CLOSED:
            return
        if self._state is LifecycleState.CREATED:
            raise LifecycleError("cannot close a lifecycle that has not started")
        self._state = LifecycleState.CLOSING
        self._cancel_event.set()
        self._state = LifecycleState.CLOSED

    async def __aenter__(self) -> "Lifecycle":
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()
