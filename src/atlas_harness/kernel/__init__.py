"""Runtime lifecycle primitives and shared errors."""

from atlas_harness.kernel.clock import Clock, FrozenClock, SystemClock
from atlas_harness.kernel.errors import (
    AtlasError,
    BudgetExceededError,
    CancellationError,
    ConfigurationError,
    EventLogCorruptionError,
    EventStoreError,
    EventValidationError,
    LifecycleError,
    RecoveryError,
    SessionNotFoundError,
)
from atlas_harness.kernel.faults import FaultInjected, FaultInjector
from atlas_harness.kernel.ids import (
    IdFactory,
    idempotency_key,
    new_id,
    validate_session_id,
)
from atlas_harness.kernel.lifecycle import Lifecycle, LifecycleState

__all__ = [
    "AtlasError",
    "BudgetExceededError",
    "CancellationError",
    "Clock",
    "ConfigurationError",
    "EventLogCorruptionError",
    "EventStoreError",
    "EventValidationError",
    "FaultInjected",
    "FaultInjector",
    "FrozenClock",
    "IdFactory",
    "Lifecycle",
    "LifecycleError",
    "LifecycleState",
    "RecoveryError",
    "SessionNotFoundError",
    "SystemClock",
    "idempotency_key",
    "new_id",
    "validate_session_id",
]
