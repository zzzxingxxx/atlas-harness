"""Deterministic-friendly identifiers used by the event kernel."""

from __future__ import annotations

import hashlib
import re
import uuid

from atlas_harness.kernel.clock import Clock, SystemClock
from atlas_harness.kernel.errors import EventValidationError

SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
"""Session ids become path segments, so the character set stays restrictive."""


def new_id(prefix: str = "") -> str:
    """Return a globally unique, URL-safe identifier."""

    value = uuid.uuid4().hex
    return f"{prefix}_{value}" if prefix else value


def idempotency_key(*parts: object) -> str:
    """Build a stable key from caller supplied values."""

    encoded = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_session_id(session_id: str) -> str:
    """Reject ids that could escape the data directory or break the index."""

    if not SESSION_ID_PATTERN.match(session_id):
        raise EventValidationError(
            "invalid session id",
            details={"session_id": session_id, "pattern": SESSION_ID_PATTERN.pattern},
        )
    return session_id


class IdFactory:
    """Generate event metadata while allowing an injectable clock in tests."""

    def __init__(self, clock: Clock | None = None) -> None:
        self.clock: Clock = clock or SystemClock()

    def event_id(self) -> str:
        return new_id("evt")

    def session_id(self) -> str:
        return new_id("ses")

    def lane_id(self) -> str:
        return new_id("lane")

    def operation_id(self) -> str:
        return new_id("op")

    def timestamp_ms(self) -> int:
        return self.clock.now_ms()

    def idempotency_key(self, *parts: object) -> str:
        return idempotency_key(*parts)
