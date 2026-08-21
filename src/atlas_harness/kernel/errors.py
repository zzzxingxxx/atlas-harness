"""Stable, serializable error types used by the application boundary."""

from typing import Any


class AtlasError(Exception):
    """Base error with a stable machine-readable code and exit code."""

    code = "atlas_error"
    exit_code = 1

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": self.code,
            "message": self.message,
            "details": self.details,
        }


class ConfigurationError(AtlasError):
    code = "configuration_error"
    exit_code = 2


class LifecycleError(AtlasError):
    code = "lifecycle_error"
    exit_code = 3


class BudgetExceededError(AtlasError):
    code = "budget_exceeded"
    exit_code = 4


class CancellationError(AtlasError):
    code = "cancelled"
    exit_code = 130


class EventValidationError(AtlasError):
    code = "event_validation_error"
    exit_code = 5


class EventStoreError(AtlasError):
    code = "event_store_error"
    exit_code = 6


class RecoveryError(AtlasError):
    code = "recovery_error"
    exit_code = 7


class EventLogCorruptionError(EventStoreError):
    """The append-only log cannot be trusted; automatic recovery must stop.

    ``details`` always carries ``last_valid_seq`` so an operator can locate the
    exact boundary between trusted and untrusted history.
    """

    code = "event_log_corruption"
    exit_code = 8


class SessionNotFoundError(AtlasError):
    code = "session_not_found"
    exit_code = 9
