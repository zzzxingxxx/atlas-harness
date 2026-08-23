"""Memory and Skill reaching a real model request, and the record of that choice.

The unit tests pin the selector's decisions. This file pins the consequence: what
the adapter actually received, and what the event log says about why. Those are the
two things an operator can check after the fact, so they are checked here against a
real ``AgentLoop`` rather than against the selector alone.

The asymmetry worth stating up front is that injection is a property of the
*request*, not of the conversation. ``_request_messages`` builds the injected
messages into the ``ModelRequest`` and never into ``RunState``, so a skill appears
in every request it is relevant to and in none of the transcript. Several tests
below exist only to keep that true.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from tests.conftest import TOOL_SESSION_ID

from atlas_harness.agent import AgentLoop, StopCause
from atlas_harness.context.capability import (
    REASON_EXPIRED,
    REASON_NOT_PERMITTED,
    CapabilitySelector,
)
from atlas_harness.events import Event, EventStore, EventType
from atlas_harness.memory.models import DAY_MS
from atlas_harness.memory.repository import MemoryRepository
from atlas_harness.memory.retrieval import MemoryRetriever
from atlas_harness.model.protocol import Role
from atlas_harness.model.providers.fake import FakeAdapter, text_turn, tool_call_turn
from atlas_harness.skills.models import SkillRecord, SkillStatus
from atlas_harness.skills.repository import SkillRepository
from atlas_harness.tools import default_registry
from atlas_harness.tools.executor import ToolExecutor
from atlas_harness.tools.manifest import SCOPE_NETWORK

OPERATION_ID = "op_capability"
TASK = "write the release notes for the changelog"


@pytest.fixture
def store(tool_store: EventStore) -> EventStore:
    return tool_store


@pytest.fixture
def memories(store: EventStore, clock: Any) -> MemoryRepository:
    return MemoryRepository(store, clock=clock)


@pytest.fixture
def skills(store: EventStore, clock: Any) -> SkillRepository:
    return SkillRepository(store, clock=clock)


def start_operation(store: EventStore) -> str:
    store.append_new(
        EventType.OPERATION_STARTED,
        session_id=TOOL_SESSION_ID,
        operation_id=OPERATION_ID,
        payload={"name": "agent_run"},
    )
    return OPERATION_ID


def activate(skills: SkillRepository, record: SkillRecord) -> SkillRecord:
    """Register and promote through candidate, the way a real operator must."""

    skills.register(record, session_id=TOOL_SESSION_ID)
    skills.set_status(
        record.skill_id, record.version, SkillStatus.CANDIDATE, session_id=TOOL_SESSION_ID
    )
    return skills.set_status(
        record.skill_id,
        record.version,
        SkillStatus.ACTIVE,
        session_id=TOOL_SESSION_ID,
        evaluation_ref="eval-7",
    )


def build_loop(
    store: EventStore,
    executor: ToolExecutor,
    adapter: FakeAdapter,
    *,
    memories: MemoryRepository | None = None,
    skills: SkillRepository | None = None,
    clock: Any = None,
    **selector_kwargs: Any,
) -> AgentLoop:
    selector = None
    if memories is not None or skills is not None:
        selector = CapabilitySelector(
            retriever=None if memories is None else MemoryRetriever(memories, clock=clock),
            skills=skills,
            **selector_kwargs,
        )
    return AgentLoop(
        adapter=adapter,
        registry=default_registry(),
        executor=executor,
        store=store,
        model="fake-model",
        provider="fake",
        capabilities=selector,
    )


def payloads_of(events: Sequence[Event], event_type: EventType) -> list[dict[str, Any]]:
    return [
        event.payload.model_dump(mode="json") for event in events if event.event_type is event_type
    ]


def system_texts(adapter: FakeAdapter) -> list[str]:
    return [
        message.content for message in adapter.requests[-1].messages if message.role is Role.SYSTEM
    ]


# --------------------------------------------------------------------------- #
# the selection reaches the request
# --------------------------------------------------------------------------- #


async def test_a_relevant_skill_and_memory_reach_the_model(
    store: EventStore,
    executor: ToolExecutor,
    memories: MemoryRepository,
    skills: SkillRepository,
    clock: Any,
) -> None:
    activate(
        skills,
        SkillRecord(
            skill_id="release-notes",
            version="1.2.0",
            description="how to write release notes",
            body="summarize the changelog by section",
            triggers=("release notes",),
        ),
    )
    memories.remember(
        "the changelog lives in docs/releases",
        session_id=TOOL_SESSION_ID,
        layer="semantic",
        confidence=0.9,
    )
    adapter = FakeAdapter(script=[text_turn("done")])
    loop = build_loop(store, executor, adapter, memories=memories, skills=skills, clock=clock)
    start_operation(store)

    result = await loop.run(TASK, session_id=TOOL_SESSION_ID, operation_id=OPERATION_ID)

    assert result.stop_cause is StopCause.COMPLETED
    joined = "\n".join(system_texts(adapter))
    assert "[skill:release-notes v1.2.0]" in joined
    assert "docs/releases" in joined


async def test_the_injection_follows_the_system_prompt(
    store: EventStore, executor: ToolExecutor, skills: SkillRepository, clock: Any
) -> None:
    """Capabilities are instruction, so they sit with the instructions.

    Placed after the transcript they would read as the newest turn, which is the
    one position that changes their meaning.
    """

    activate(
        skills,
        SkillRecord(skill_id="notes", version="1.0.0", triggers=("release notes",)),
    )
    adapter = FakeAdapter(script=[text_turn("done")])
    loop = build_loop(store, executor, adapter, skills=skills, clock=clock)
    start_operation(store)

    await loop.run(TASK, session_id=TOOL_SESSION_ID, operation_id=OPERATION_ID)

    messages = adapter.requests[-1].messages
    assert messages[0].content == loop.system_prompt
    assert "[skill:notes v1.0.0]" in messages[1].content
    assert messages[-1].role is Role.USER


async def test_injection_never_enters_the_transcript(
    store: EventStore, executor: ToolExecutor, skills: SkillRepository, clock: Any
) -> None:
    """Retrieval runs per request against the current turn. Appending its output to
    the conversation would accumulate stale capabilities and grow the prompt every
    pass, and an injected skill is not something the conversation said."""

    activate(
        skills,
        SkillRecord(skill_id="notes", version="1.0.0", triggers=("release notes",)),
    )
    adapter = FakeAdapter(
        script=[
            tool_call_turn("read_file", {"path": "a.txt"}, call_id="c1"),
            text_turn("done"),
        ]
    )
    loop = build_loop(store, executor, adapter, skills=skills, clock=clock)
    start_operation(store)

    await loop.run(TASK, session_id=TOOL_SESSION_ID, operation_id=OPERATION_ID)

    state = store.load_state(TOOL_SESSION_ID)
    assert "[skill:notes" not in "\n".join(state.messages)
    # But every request still carried it, including the second one.
    assert len(adapter.requests) == 2
    for request in adapter.requests:
        assert any("[skill:notes v1.0.0]" in message.content for message in request.messages)


async def test_no_selector_means_the_prompt_is_exactly_what_m5_built(
    store: EventStore, executor: ToolExecutor
) -> None:
    """``ATLAS_INJECT_CAPABILITIES=false`` is the control an evaluation of the
    retrieval itself needs, so it has to produce the pre-M6 prompt exactly."""

    adapter = FakeAdapter(script=[text_turn("done")])
    loop = build_loop(store, executor, adapter)
    start_operation(store)

    await loop.run(TASK, session_id=TOOL_SESSION_ID, operation_id=OPERATION_ID)

    assert loop.capabilities is None
    assert system_texts(adapter) == [loop.system_prompt]
    assert "capability_injected" not in [
        event.event_type.value for event in store.read_events(TOOL_SESSION_ID)
    ]


# --------------------------------------------------------------------------- #
# the record of the choice
# --------------------------------------------------------------------------- #


async def test_every_injection_is_recorded_with_its_query_and_grant(
    store: EventStore, executor: ToolExecutor, skills: SkillRepository, clock: Any
) -> None:
    activate(
        skills,
        SkillRecord(
            skill_id="release-notes",
            version="1.2.0",
            triggers=("release notes",),
            source_task="op_seed",
            evidence_refs=("eval-7",),
        ),
    )
    adapter = FakeAdapter(script=[text_turn("done")])
    loop = build_loop(store, executor, adapter, skills=skills, clock=clock)
    start_operation(store)

    await loop.run(TASK, session_id=TOOL_SESSION_ID, operation_id=OPERATION_ID)

    injected = payloads_of(store.read_events(TOOL_SESSION_ID), EventType.CAPABILITY_INJECTED)
    assert len(injected) == 1
    assert injected[0]["query"] == TASK
    assert injected[0]["granted_scopes"] == sorted(executor.policy.granted_scopes)
    choice = injected[0]["selected"][0]
    assert choice["kind"] == "skill"
    assert choice["ref_id"] == "release-notes"
    assert choice["version"] == "1.2.0"
    assert choice["source_task"] == "op_seed"
    assert choice["evidence_refs"] == ["eval-7"]


async def test_an_unpermitted_skill_is_recorded_as_rejected_not_omitted(
    store: EventStore, executor: ToolExecutor, skills: SkillRepository, clock: Any
) -> None:
    """The plan's second condition, end to end.

    The prompt must not contain it and the log must explain why. An absent event
    would make "no skill matched" and "the skill needs a scope you have not
    granted" indistinguishable, and only one of those is actionable.
    """

    activate(
        skills,
        SkillRecord(
            skill_id="publish-notes",
            version="1.0.0",
            triggers=("release notes",),
            required_scopes=(SCOPE_NETWORK,),
        ),
    )
    adapter = FakeAdapter(script=[text_turn("done")])
    loop = build_loop(store, executor, adapter, skills=skills, clock=clock)
    start_operation(store)

    await loop.run(TASK, session_id=TOOL_SESSION_ID, operation_id=OPERATION_ID)

    assert "publish-notes" not in "\n".join(system_texts(adapter))
    injected = payloads_of(store.read_events(TOOL_SESSION_ID), EventType.CAPABILITY_INJECTED)
    assert injected and injected[0]["selected"] == []
    skipped = injected[0]["skipped"]
    assert [item["reason"] for item in skipped] == [REASON_NOT_PERMITTED]
    assert SCOPE_NETWORK in skipped[0]["detail"]


async def test_an_expired_episodic_memory_is_reported_not_injected(
    store: EventStore, executor: ToolExecutor, memories: MemoryRepository, clock: Any
) -> None:
    """The plan's fourth condition, end to end. Expiry is a rejection with a name,
    not a low score: a stale observation must not reach the model as a fact, and
    the log has to say it was considered."""

    memories.remember(
        "the changelog was empty last week",
        session_id=TOOL_SESSION_ID,
        layer="episodic",
        memory_id="mem_stale",
    )
    clock.advance(14 * DAY_MS + 1)
    memories.remember(
        "the changelog lives in docs/releases",
        session_id=TOOL_SESSION_ID,
        layer="semantic",
        memory_id="mem_durable",
    )
    adapter = FakeAdapter(script=[text_turn("done")])
    loop = build_loop(store, executor, adapter, memories=memories, clock=clock)
    start_operation(store)

    await loop.run(TASK, session_id=TOOL_SESSION_ID, operation_id=OPERATION_ID)

    joined = "\n".join(system_texts(adapter))
    assert "docs/releases" in joined
    assert "empty last week" not in joined
    injected = payloads_of(store.read_events(TOOL_SESSION_ID), EventType.CAPABILITY_INJECTED)
    assert [item["ref_id"] for item in injected[0]["selected"]] == ["mem_durable"]
    stale = next(item for item in injected[0]["skipped"] if item["ref_id"] == "mem_stale")
    assert stale["reason"] == REASON_EXPIRED
    assert stale["detail"] == "episodic"


async def test_nothing_considered_means_nothing_recorded(
    store: EventStore, executor: ToolExecutor, memories: MemoryRepository, clock: Any
) -> None:
    """An empty store has nothing to explain. Writing an event per iteration saying
    so would bury the injections that do carry information."""

    adapter = FakeAdapter(script=[text_turn("done")])
    loop = build_loop(store, executor, adapter, memories=memories, clock=clock)
    start_operation(store)

    await loop.run(TASK, session_id=TOOL_SESSION_ID, operation_id=OPERATION_ID)

    assert payloads_of(store.read_events(TOOL_SESSION_ID), EventType.CAPABILITY_INJECTED) == []


# --------------------------------------------------------------------------- #
# the projection and the completion condition
# --------------------------------------------------------------------------- #


async def test_the_replayed_state_can_answer_what_was_injected(
    store: EventStore,
    executor: ToolExecutor,
    memories: MemoryRepository,
    skills: SkillRepository,
    clock: Any,
) -> None:
    """The completion condition is that a trace explains the selection, and a trace
    reads the log. So the projection has to survive a full replay."""

    activate(
        skills,
        SkillRecord(skill_id="release-notes", version="1.2.0", triggers=("release notes",)),
    )
    memories.remember(
        "the changelog lives in docs/releases",
        session_id=TOOL_SESSION_ID,
        layer="semantic",
        memory_id="mem_durable",
    )
    adapter = FakeAdapter(script=[text_turn("done")])
    loop = build_loop(store, executor, adapter, memories=memories, skills=skills, clock=clock)
    start_operation(store)

    await loop.run(TASK, session_id=TOOL_SESSION_ID, operation_id=OPERATION_ID)

    state = store.load_state(TOOL_SESSION_ID)
    assert "mem_durable" in state.live_memory_ids
    assert state.skill_statuses["release-notes@1.2.0"] == "active"
    assert state.capability_injections == 1

    operation = state.operations[OPERATION_ID]
    assert operation.injected_skill_versions == ["release-notes@1.2.0"]
    assert operation.injected_memory_ids == ["mem_durable"]
    assert state.state_hash() == store.load_state(TOOL_SESSION_ID).state_hash()


async def test_a_fixed_task_finds_the_skill_and_explains_the_choice(
    store: EventStore,
    executor: ToolExecutor,
    memories: MemoryRepository,
    skills: SkillRepository,
    clock: Any,
) -> None:
    """M6's completion condition: a fixed task retrieves the relevant skill, and the
    trace explains where the selection came from.

    Discrimination here is in the *ordering*, not in membership. The MATCH query
    ORs every token, so an unrelated skill still matches on a common word like
    "the" and occupies a slot when there is room; BM25 and the trigger prefilter
    are what keep it below the skill the task is actually about. So the assertion
    is that the relevant skill ranks first, not that the others are absent -- an
    absence assertion here would pass for the wrong reason.
    """

    activate(
        skills,
        SkillRecord(
            skill_id="release-notes",
            version="1.2.0",
            description="write the release notes from the changelog",
            body="group the changelog entries by area",
            triggers=("release notes",),
            evidence_refs=("eval-7",),
        ),
    )
    activate(
        skills,
        SkillRecord(
            skill_id="rotate-keys",
            version="1.0.0",
            description="rotate the signing credentials",
            triggers=("rotate keys",),
        ),
    )
    activate(
        skills,
        SkillRecord(
            skill_id="publish-notes",
            version="1.0.0",
            description="publish the release notes to the feed",
            triggers=("release notes",),
            required_scopes=(SCOPE_NETWORK,),
        ),
    )
    memories.remember(
        "the changelog lives in docs/releases",
        session_id=TOOL_SESSION_ID,
        layer="semantic",
        confidence=0.9,
        memory_id="mem_durable",
    )
    adapter = FakeAdapter(script=[text_turn("notes written")])
    loop = build_loop(store, executor, adapter, memories=memories, skills=skills, clock=clock)
    start_operation(store)

    result = await loop.run(TASK, session_id=TOOL_SESSION_ID, operation_id=OPERATION_ID)

    assert result.stop_cause is StopCause.COMPLETED

    # The relevant skill was found, and the unpermitted one is absent for a reason
    # the log states rather than because it happened to rank low.
    joined = "\n".join(system_texts(adapter))
    assert "[skill:release-notes v1.2.0]" in joined
    assert "publish-notes" not in joined

    injected = payloads_of(store.read_events(TOOL_SESSION_ID), EventType.CAPABILITY_INJECTED)
    assert len(injected) == 1
    ranked = [item["ref_id"] for item in injected[0]["selected"]]
    assert ranked[0] == "release-notes"
    assert "mem_durable" in ranked
    reasons = {item["ref_id"]: item["reason"] for item in injected[0]["skipped"]}
    assert reasons["publish-notes@1.0.0"] == REASON_NOT_PERMITTED
