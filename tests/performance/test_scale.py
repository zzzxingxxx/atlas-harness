"""Scale properties that are too slow to check on every pull request.

Plan section 11.1 puts 大日志, 长 Session and 并发只读工具 at the end of a milestone
rather than in the five-command gate, because each of these costs seconds where a
unit test costs milliseconds. They live in their own directory behind the
``performance`` marker so the gate stays fast while the claims the design rests on
-- that folding is linear, that the log is the whole truth however long it gets,
and that concurrency neither loses nor duplicates an event -- still get checked
before a release.
"""

from __future__ import annotations

import asyncio
import json
import time
import tracemalloc
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict
from tests.conftest import TOOL_SESSION_ID, ExecutorFactory, StoreFactory

from atlas_harness.context.compaction import CompactionResult, Compactor
from atlas_harness.context.tokens import ContextBudget
from atlas_harness.events import Event, EventStore, EventType
from atlas_harness.events.models import DEFAULT_LANE
from atlas_harness.events.reducer import replay
from atlas_harness.events.store import INDEX_FILENAME, LOG_FILENAME, SESSIONS_DIRNAME
from atlas_harness.kernel.clock import FrozenClock
from atlas_harness.kernel.ids import IdFactory
from atlas_harness.model.protocol import ModelMessage
from atlas_harness.tools import RiskLevel, Tool, ToolContext, ToolManifest, ToolRegistry
from atlas_harness.tools.executor import ToolCall

pytestmark = pytest.mark.performance

LARGE_LOG_EVENTS = 6_400
"""Big enough that a quadratic fold would show, small enough that this file plus
the doubled log it also folds stays inside a few seconds of the ~15s budget."""

FOLD_ROUNDS = 3
"""Folds per measurement. The best of three keeps one GC pause from deciding a ratio."""

FOLD_RATIO_CEILING = 3.0
"""Doubling the input doubles a linear fold and quadruples a quadratic one, so the
ceiling sits between the two rather than close to either."""

LONG_SESSION_LANES = 6
LONG_SESSION_OPERATIONS_PER_LANE = 4
CONCURRENT_READ_CALLS = 48

BARRIER_TIMEOUT_S = 5.0
"""How long a read tool waits for its siblings before failing the batch. Only a
serialized executor ever reaches it, so a slow machine costs nothing here."""

HUGE_TOOL_OUTPUT_BYTES = 2_000_000
TRUNCATED_OUTPUT_BYTES = 32_000

LARGE_SESSION_ID = "ses_large"
DOUBLE_SESSION_ID = "ses_large_doubled"
LONG_SESSION_ID = "ses_long"
LONG_OPERATION_PREFIX = "op_long"


def _write_log(data_dir: Path, session_id: str, events: int) -> Path:
    """Write a log straight to disk instead of appending through the store.

    ``EventStore.append`` fsyncs every event, which costs milliseconds each and
    would spend this file's whole budget on writes rather than on the reading and
    folding these tests actually measure. The bytes are produced exactly as
    ``append`` produces them, so the store still has to accept them.
    """

    factory = IdFactory(FrozenClock(1_700_000_000_000))
    path = data_dir / SESSIONS_DIRNAME / session_id / LOG_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for seq in range(1, events + 1):
            if seq == 1:
                event = Event.create(
                    EventType.SESSION_CREATED,
                    session_id=session_id,
                    seq=seq,
                    payload={"title": "a long-lived session", "workspace_root": "/tmp/ws"},
                    factory=factory,
                )
            else:
                event = Event.create(
                    EventType.ASSISTANT_MESSAGE,
                    session_id=session_id,
                    seq=seq,
                    payload={"content": f"turn {seq}: " + "x" * 120},
                    factory=factory,
                )
            line = json.dumps(event.to_json_dict(), sort_keys=True, ensure_ascii=False)
            handle.write(line + "\n")
    return path


def _fold_seconds(events: Sequence[Event], session_id: str) -> float:
    timings: list[float] = []
    for _ in range(FOLD_ROUNDS):
        start = time.perf_counter()
        replay(events, session_id=session_id)
        timings.append(time.perf_counter() - start)
    return min(timings)


def _drop_index(data_dir: Path) -> None:
    for path in data_dir.glob(f"{INDEX_FILENAME}*"):
        path.unlink()


@dataclass(frozen=True)
class LargeLog:
    data_dir: Path
    session_id: str
    events: tuple[Event, ...]


@pytest.fixture
def large_log(store_factory: StoreFactory, tmp_path: Path) -> LargeLog:
    data_dir = tmp_path / "large"
    _write_log(data_dir, LARGE_SESSION_ID, LARGE_LOG_EVENTS)
    store = store_factory(data_dir)
    events = tuple(store.read_events(LARGE_SESSION_ID))
    # Windows will not let the index file be deleted while any handle is open, and
    # one of the tests below deletes it deliberately.
    store.close()
    return LargeLog(data_dir=data_dir, session_id=LARGE_SESSION_ID, events=events)


def test_a_log_of_thousands_of_events_folds_to_the_length_the_log_records(
    large_log: LargeLog,
) -> None:
    state = replay(large_log.events, session_id=large_log.session_id)

    assert len(large_log.events) == LARGE_LOG_EVENTS
    assert state.event_count == LARGE_LOG_EVENTS
    assert state.last_seq == LARGE_LOG_EVENTS
    assert len(state.messages) == LARGE_LOG_EVENTS - 1


def test_folding_a_large_log_stays_linear_rather_than_quadratic(
    store_factory: StoreFactory, tmp_path: Path
) -> None:
    single_dir = tmp_path / "single"
    double_dir = tmp_path / "double"
    _write_log(single_dir, LARGE_SESSION_ID, LARGE_LOG_EVENTS)
    _write_log(double_dir, DOUBLE_SESSION_ID, LARGE_LOG_EVENTS * 2)
    single = store_factory(single_dir).read_events(LARGE_SESSION_ID)
    double = store_factory(double_dir).read_events(DOUBLE_SESSION_ID)

    ratio = _fold_seconds(double, DOUBLE_SESSION_ID) / _fold_seconds(single, LARGE_SESSION_ID)

    # Wall clock is noisy on a shared machine, so this ceiling is deliberately far
    # from the ~2.0 a linear fold measures. It exists to catch a fold that became
    # quadratic, which lands near 4.0, not to police constant factors.
    assert ratio < FOLD_RATIO_CEILING


def test_a_large_log_can_be_streamed_instead_of_being_held_whole(
    store_factory: StoreFactory, large_log: LargeLog
) -> None:
    """``iter_events`` is what lets a long session be read on a small machine."""

    store = store_factory(large_log.data_dir)

    tracemalloc.start()
    materialised = store.read_events(large_log.session_id)
    _, list_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    del materialised

    tracemalloc.start()
    seen = 0
    last_seq = 0
    for event in store.iter_events(large_log.session_id):
        seen += 1
        last_seq = event.seq
    _, stream_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert seen == LARGE_LOG_EVENTS
    assert last_seq == LARGE_LOG_EVENTS
    assert stream_peak < list_peak / 2


def test_a_large_log_folds_to_the_same_hash_after_the_index_is_deleted(
    store_factory: StoreFactory, large_log: LargeLog
) -> None:
    """The index is derived state. Thousands of events later it still has to be."""

    store = store_factory(large_log.data_dir)
    expected = store.load_state(large_log.session_id).state_hash()
    store.close()

    _drop_index(large_log.data_dir)

    rebuilt = store_factory(large_log.data_dir).load_state(large_log.session_id)

    assert rebuilt.state_hash() == expected
    assert rebuilt.event_count == LARGE_LOG_EVENTS


@dataclass(frozen=True)
class LongSession:
    store: EventStore
    data_dir: Path
    events: tuple[Event, ...]
    transcript: tuple[ModelMessage, ...]
    compacted_input: tuple[ModelMessage, ...]
    compaction: CompactionResult


@pytest.fixture
def long_session(store_factory: StoreFactory, tmp_path: Path) -> LongSession:
    store = store_factory()
    store.append_new(
        EventType.SESSION_CREATED,
        session_id=LONG_SESSION_ID,
        payload={"title": "many lanes, one log", "workspace_root": str(tmp_path / "ws")},
    )
    lanes = [f"lane_{index}" for index in range(LONG_SESSION_LANES)]
    for lane in lanes:
        store.append_new(
            EventType.LANE_CREATED,
            session_id=LONG_SESSION_ID,
            payload={"lane": lane, "parent_lane": DEFAULT_LANE, "reason": "fan out"},
        )

    compactor = Compactor(store, budget=ContextBudget(limit_tokens=1_000))
    transcript = [ModelMessage.system("keep the objective in the fixed slot")]
    halfway = LONG_SESSION_LANES * LONG_SESSION_OPERATIONS_PER_LANE // 2
    compaction: CompactionResult | None = None
    compacted_input: tuple[ModelMessage, ...] = ()
    finished = 0

    for lane in lanes:
        for index in range(LONG_SESSION_OPERATIONS_PER_LANE):
            operation_id = f"{LONG_OPERATION_PREFIX}_{lane}_{index}"
            for event_type, payload in (
                (EventType.OPERATION_STARTED, {"name": f"{lane} step {index}"}),
                (EventType.MODEL_REQUESTED, {"model": "stub"}),
                (EventType.ASSISTANT_MESSAGE, {"content": f"{lane} answered step {index}"}),
                (EventType.OPERATION_FINISHED, {"result": "ok"}),
            ):
                store.append_new(
                    event_type,
                    session_id=LONG_SESSION_ID,
                    payload=payload,
                    lane_id=lane,
                    operation_id=operation_id,
                )
            transcript.append(ModelMessage.user(f"{lane} step {index}"))
            finished += 1
            if finished == halfway:
                compacted_input = tuple(transcript)
                compaction = compactor.compact(
                    LONG_SESSION_ID,
                    operation_id=operation_id,
                    messages=list(compacted_input),
                    used_tokens=900,
                    keep_recent=4,
                    lane_id=lane,
                )

    assert compaction is not None
    return LongSession(
        store=store,
        data_dir=store.data_dir,
        events=tuple(store.read_events(LONG_SESSION_ID)),
        transcript=tuple(transcript),
        compacted_input=compacted_input,
        compaction=compaction,
    )


def test_a_long_session_across_many_lanes_keeps_every_operation_in_one_log(
    long_session: LongSession,
) -> None:
    state = replay(long_session.events, session_id=LONG_SESSION_ID)
    operations = LONG_SESSION_LANES * LONG_SESSION_OPERATIONS_PER_LANE

    assert [event.seq for event in long_session.events] == list(
        range(1, len(long_session.events) + 1)
    )
    assert state.event_count == len(long_session.events)
    assert len(state.operations) == operations
    assert len(state.lanes) == LONG_SESSION_LANES + 1
    assert all(operation.status == "finished" for operation in state.operations.values())


def test_compaction_shrinks_the_prompt_and_leaves_the_record_alone(
    long_session: LongSession,
) -> None:
    """Compaction is not deletion, and at this length the difference is the point.

    A long session is survivable because the prompt sent to the model shrinks while
    the log keeps every turn. Asserting only the first half would let a change that
    trimmed the projection pass, which would make replay lossy.
    """

    result = long_session.compaction
    state = replay(long_session.events, session_id=LONG_SESSION_ID)
    answers = [
        event for event in long_session.events if event.event_type is EventType.ASSISTANT_MESSAGE
    ]

    assert result.recorded
    assert result.replaced_messages > 0
    assert len(result.messages) < len(long_session.compacted_input)
    assert state.compactions == 1
    assert len(state.messages) == len(answers)


def test_a_long_session_still_folds_to_the_same_hash_from_the_log_alone(
    store_factory: StoreFactory, long_session: LongSession
) -> None:
    expected = long_session.store.load_state(LONG_SESSION_ID).state_hash()
    long_session.store.close()

    _drop_index(long_session.data_dir)

    assert store_factory(long_session.data_dir).load_state(LONG_SESSION_ID).state_hash() == expected


class NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass
class ConcurrencyTracker:
    expected_reads: int
    read_active: int = 0
    write_active: int = 0
    max_read_active: int = 0
    max_write_active: int = 0
    reads_beside_a_write: int = 0
    all_reads_in_flight: asyncio.Event = field(default_factory=asyncio.Event)

    def enter(self, risk: RiskLevel) -> None:
        if risk is RiskLevel.READ:
            self.read_active += 1
            self.max_read_active = max(self.max_read_active, self.read_active)
            return
        self.write_active += 1
        self.max_write_active = max(self.max_write_active, self.write_active)
        self.reads_beside_a_write = max(self.reads_beside_a_write, self.read_active)

    def leave(self, risk: RiskLevel) -> None:
        if risk is RiskLevel.READ:
            self.read_active -= 1
            return
        self.write_active -= 1

    async def wait_for_every_read(self) -> None:
        """Hold each read until all of them are inside a tool at the same moment.

        A barrier rather than a sleep: an executor that serialized these calls can
        never clear it, and one that runs them together clears it as fast as the
        machine allows, so the assertion below is not a race against wall clock.
        """

        if self.read_active >= self.expected_reads:
            self.all_reads_in_flight.set()
        await asyncio.wait_for(self.all_reads_in_flight.wait(), timeout=BARRIER_TIMEOUT_S)


class TrackedTool(Tool):
    input_model = NoArgs

    def __init__(self, name: str, risk: RiskLevel, tracker: ConcurrencyTracker) -> None:
        self.manifest = ToolManifest(
            name=name,
            version="1.0.0",
            description="record how many calls were in flight together",
            input_schema=NoArgs.model_json_schema(),
            risk=risk,
            requires_approval=False,
        )
        self.tracker = tracker

    async def run(self, args: NoArgs, context: ToolContext) -> str:
        self.tracker.enter(self.manifest.risk)
        try:
            if self.manifest.risk is RiskLevel.READ:
                await self.tracker.wait_for_every_read()
            return self.manifest.name
        finally:
            self.tracker.leave(self.manifest.risk)


class HugeOutputTool(Tool):
    manifest = ToolManifest(
        name="huge_output",
        version="1.0.0",
        description="return far more than a log should carry",
        input_schema=NoArgs.model_json_schema(),
        risk=RiskLevel.READ,
        max_output_bytes=TRUNCATED_OUTPUT_BYTES,
    )
    input_model = NoArgs

    async def run(self, args: NoArgs, context: ToolContext) -> str:
        return "z" * HUGE_TOOL_OUTPUT_BYTES


async def test_many_read_calls_run_together_and_leave_a_gap_free_log(
    executor_factory: ExecutorFactory, tool_store: EventStore
) -> None:
    tracker = ConcurrencyTracker(expected_reads=CONCURRENT_READ_CALLS)
    reader = TrackedTool("parallel_read", RiskLevel.READ, tracker)
    writer = TrackedTool("serial_write", RiskLevel.WRITE, tracker)
    executor = executor_factory(registry=ToolRegistry([reader, writer]))
    calls = [ToolCall(tool_name="parallel_read") for _ in range(CONCURRENT_READ_CALLS)]
    calls.append(ToolCall(tool_name="serial_write"))

    outcomes = await executor.execute_many(calls, session_id=TOOL_SESSION_ID)

    events = tool_store.read_events(TOOL_SESSION_ID)
    results = [event for event in events if event.event_type is EventType.TOOL_RESULT]

    assert reader.manifest.can_run_in_parallel
    assert not writer.manifest.can_run_in_parallel
    assert len(outcomes) == len(calls)
    assert all(outcome.success for outcome in outcomes)
    assert len(results) == len(calls)
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    assert tracker.max_read_active == CONCURRENT_READ_CALLS
    assert tracker.max_write_active == 1
    assert tracker.reads_beside_a_write == 0
    assert replay(events, session_id=TOOL_SESSION_ID).event_count == len(events)


async def test_a_huge_tool_output_is_truncated_before_it_reaches_the_log(
    executor_factory: ExecutorFactory, tool_store: EventStore
) -> None:
    executor = executor_factory(
        registry=ToolRegistry([HugeOutputTool()]), max_output_bytes=TRUNCATED_OUTPUT_BYTES
    )

    outcome = await executor.execute(ToolCall(tool_name="huge_output"), session_id=TOOL_SESSION_ID)

    assert outcome.success
    assert outcome.truncated
    assert isinstance(outcome.output, str)
    assert len(outcome.output) < HUGE_TOOL_OUTPUT_BYTES // 8
    assert tool_store.log_path(TOOL_SESSION_ID).stat().st_size < HUGE_TOOL_OUTPUT_BYTES // 8
