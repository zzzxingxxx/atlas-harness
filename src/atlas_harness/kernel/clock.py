"""Injectable clocks keep future event and replay tests deterministic."""

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now_ms(self) -> int: ...


class SystemClock:
    """Wall clock used by the running application."""

    def now_ms(self) -> int:
        return int(datetime.now(UTC).timestamp() * 1000)


class FrozenClock:
    """Mutable deterministic clock for tests and fault-injection scenarios."""

    def __init__(self, initial_ms: int = 0) -> None:
        self._now_ms = initial_ms

    def now_ms(self) -> int:
        return self._now_ms

    def advance(self, milliseconds: int) -> None:
        if milliseconds < 0:
            raise ValueError("milliseconds must be non-negative")
        self._now_ms += milliseconds
