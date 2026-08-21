"""Named fault points so crash and I/O failures can be tested deterministically."""

from __future__ import annotations

from dataclasses import dataclass


class FaultInjected(Exception):
    """Raised by an armed fault point to stand in for a crash or I/O failure."""


@dataclass
class _ArmedFault:
    error: BaseException
    remaining: int


class FaultInjector:
    """Arm named points and fail the next N calls that reach them.

    Production code calls :meth:`check` unconditionally; with nothing armed the
    call is a single dict truthiness test.
    """

    def __init__(self) -> None:
        self._armed: dict[str, _ArmedFault] = {}
        self._triggered: dict[str, int] = {}

    def arm(self, point: str, *, error: BaseException | None = None, times: int = 1) -> None:
        if times < 1:
            raise ValueError("times must be >= 1")
        self._armed[point] = _ArmedFault(
            error=error or FaultInjected(f"fault injected at {point}"),
            remaining=times,
        )

    def disarm(self, point: str) -> None:
        self._armed.pop(point, None)

    def clear(self) -> None:
        self._armed.clear()
        self._triggered.clear()

    def is_armed(self, point: str) -> bool:
        return point in self._armed

    def triggered(self, point: str) -> int:
        return self._triggered.get(point, 0)

    def check(self, point: str) -> None:
        if not self._armed:
            return
        armed = self._armed.get(point)
        if armed is None:
            return
        self._triggered[point] = self._triggered.get(point, 0) + 1
        armed.remaining -= 1
        if armed.remaining <= 0:
            del self._armed[point]
        raise armed.error
