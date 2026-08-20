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
