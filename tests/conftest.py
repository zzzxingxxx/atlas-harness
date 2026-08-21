"""Shared fixtures for the AtlasHarness test suites.

Every EventStore opened through these fixtures is closed before the temporary
directory is torn down; on Windows an open SQLite handle blocks the cleanup.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from atlas_harness.events import Event, EventBus, EventStore, EventType
from atlas_harness.kernel import FaultInjector, FrozenClock
from atlas_harness.policy import (
    ApprovalGate,
    FixedApprovalGate,
    NetworkPolicy,
    PathPolicy,
    PolicyEngine,
)
from atlas_harness.tools import ToolRegistry, default_registry
from atlas_harness.tools.executor import ToolExecutor

DEMO_SESSION_ID = "ses_demo"
DEMO_OPERATION_ID = "op_demo"
TOOL_SESSION_ID = "ses_tools"

StoreFactory = Callable[..., EventStore]


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(1_700_000_000_000)


@pytest.fixture
def faults() -> FaultInjector:
    return FaultInjector()


@pytest.fixture
def store_factory(
    tmp_path: Path, clock: FrozenClock, faults: FaultInjector
) -> Iterator[StoreFactory]:
    opened: list[EventStore] = []

    def open_store(
        data_dir: Path | None = None,
        *,
        bus: EventBus | None = None,
        injector: FaultInjector | None = None,
    ) -> EventStore:
        store = EventStore(
            data_dir or tmp_path,
            clock=clock,
            faults=injector or faults,
            bus=bus,
        )
        opened.append(store)
        return store

    yield open_store
    for store in opened:
        store.close()


@pytest.fixture
def store(store_factory: StoreFactory) -> EventStore:
    return store_factory()


def seed_session(
    store: EventStore,
    session_id: str = DEMO_SESSION_ID,
    *,
    operation_id: str = DEMO_OPERATION_ID,
) -> list[Event]:
    """Append the canonical seven-event session used across the suites."""

    store.append_new(
        EventType.SESSION_CREATED,
        session_id=session_id,
        payload={"title": "demo", "workspace_root": "/tmp/ws"},
    )
    store.append_new(
        EventType.OPERATION_STARTED,
        session_id=session_id,
        operation_id=operation_id,
        payload={"name": "chat"},
    )
    store.append_new(
        EventType.MODEL_REQUESTED,
        session_id=session_id,
        operation_id=operation_id,
        payload={"provider": "stub", "model": "stub-1", "prompt": "hi"},
    )
    store.append_new(
        EventType.TOOL_STARTED,
        session_id=session_id,
        operation_id=operation_id,
        payload={"tool_name": "fs_read", "call_id": "c1", "arguments": {"path": "a.txt"}},
    )
    store.append_new(
        EventType.TOOL_RESULT,
        session_id=session_id,
        operation_id=operation_id,
        payload={"tool_name": "fs_read", "call_id": "c1", "success": True, "output": "ok"},
    )
    store.append_new(
        EventType.ASSISTANT_MESSAGE,
        session_id=session_id,
        operation_id=operation_id,
        payload={"content": "hello 世界"},
    )
    store.append_new(
        EventType.OPERATION_FINISHED,
        session_id=session_id,
        operation_id=operation_id,
        payload={"result": "done"},
    )
    return store.read_events(session_id)


@pytest.fixture
def seed() -> Callable[..., list[Event]]:
    """Expose :func:`seed_session` without importing conftest directly."""

    return seed_session


@pytest.fixture
def seeded(store: EventStore) -> tuple[EventStore, str]:
    seed_session(store)
    return store, DEMO_SESSION_ID


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A workspace root that is a sibling of the event log, never its parent."""

    root = tmp_path / "ws"
    root.mkdir()
    return root


@pytest.fixture
def paths(workspace: Path) -> PathPolicy:
    return PathPolicy(workspace, max_read_bytes=64_000)


@pytest.fixture
def policy(paths: PathPolicy, clock: FrozenClock) -> PolicyEngine:
    return PolicyEngine(paths=paths, network=NetworkPolicy(clock=clock))


ExecutorFactory = Callable[..., ToolExecutor]


@pytest.fixture
def tool_store(store_factory: StoreFactory, workspace: Path) -> EventStore:
    """An event store with the tool session already created."""

    store = store_factory()
    store.append_new(
        EventType.SESSION_CREATED,
        session_id=TOOL_SESSION_ID,
        payload={"title": "tools", "workspace_root": str(workspace)},
    )
    return store


@pytest.fixture
def executor_factory(
    tool_store: EventStore,
    policy: PolicyEngine,
    clock: FrozenClock,
    faults: FaultInjector,
) -> ExecutorFactory:
    def build(
        *,
        approve: bool = True,
        registry: ToolRegistry | None = None,
        approvals: ApprovalGate | None = None,
        max_output_bytes: int = 131_072,
    ) -> ToolExecutor:
        return ToolExecutor(
            registry=registry if registry is not None else default_registry(),
            policy=policy,
            store=tool_store,
            approvals=approvals or FixedApprovalGate(approve, reason="test", approver="test"),
            clock=clock,
            faults=faults,
            max_output_bytes=max_output_bytes,
        )

    return build


@pytest.fixture
def executor(executor_factory: ExecutorFactory) -> ToolExecutor:
    return executor_factory()
