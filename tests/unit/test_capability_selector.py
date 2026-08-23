"""Which memories and skills reach the model, and why the others did not.

The plan's completion condition for M6 is not "retrieval works" but "a trace can
explain the choice". So most of these tests assert on ``plan.skipped`` — a selector
that only reported its winners would make an empty capability slot unexplainable:
nothing matched, everything was unpermitted, and a zero budget would all look the
same from the outside.

Three of the plan's four test conditions are pinned here: an unpermitted skill never
enters the context, a skill's version and provenance survive into the record of the
injection, and an expired episodic memory is reported as expired rather than
injected. The fourth — stable ordering — lives with retrieval in
``test_memory.py``.
"""

from __future__ import annotations

import pytest

from atlas_harness.context.capability import (
    REASON_BUDGET,
    REASON_EXPIRED,
    REASON_NOT_PERMITTED,
    REASON_OVER_LIMIT,
    CapabilityPlan,
    CapabilitySelector,
)
from atlas_harness.context.compiler import Slot
from atlas_harness.events import EventStore, EventType
from atlas_harness.kernel.clock import FrozenClock
from atlas_harness.memory.models import DAY_MS
from atlas_harness.memory.repository import MemoryRepository
from atlas_harness.memory.retrieval import MemoryRetriever
from atlas_harness.model.protocol import Role
from atlas_harness.skills.models import SkillRecord, SkillStatus
from atlas_harness.skills.repository import SkillRepository
from atlas_harness.tools.manifest import (
    DEFAULT_SCOPES,
    SCOPE_FS_READ,
    SCOPE_NETWORK,
)

SESSION_ID = "ses_capability"
QUERY = "write the release notes for the changelog"


@pytest.fixture
def seeded_store(store: EventStore) -> EventStore:
    store.append_new(
        EventType.SESSION_CREATED,
        session_id=SESSION_ID,
        payload={"title": "capability", "workspace_root": "/tmp/ws"},
    )
    return store


@pytest.fixture
def memories(seeded_store: EventStore, clock: FrozenClock) -> MemoryRepository:
    return MemoryRepository(seeded_store, clock=clock)


@pytest.fixture
def skills(seeded_store: EventStore, clock: FrozenClock) -> SkillRepository:
    return SkillRepository(seeded_store, clock=clock)


def activate(
    skills: SkillRepository,
    record: SkillRecord,
) -> SkillRecord:
    """Register a skill and walk it all the way to ``active``.

    Deliberately not a shortcut: the promotion path is draft -> candidate -> active
    because the missing draft -> active edge is the evaluation gate, and a helper
    that wrote ``active`` directly would let these tests pass while that gate was
    broken.
    """

    skills.register(record, session_id=SESSION_ID)
    skills.set_status(record.skill_id, record.version, SkillStatus.CANDIDATE, session_id=SESSION_ID)
    return skills.set_status(
        record.skill_id,
        record.version,
        SkillStatus.ACTIVE,
        session_id=SESSION_ID,
        evaluation_ref="eval-1",
    )


def selector(
    *,
    memories: MemoryRepository | None = None,
    skills: SkillRepository | None = None,
    clock: FrozenClock | None = None,
    **kwargs: object,
) -> CapabilitySelector:
    retriever = None if memories is None else MemoryRetriever(memories, clock=clock)
    return CapabilitySelector(retriever=retriever, skills=skills, **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# the empty cases stay distinguishable
# --------------------------------------------------------------------------- #


def test_a_selector_with_no_stores_produces_an_empty_but_valid_plan() -> None:
    plan = CapabilitySelector().select(QUERY)

    assert plan.selected == ()
    assert plan.skipped == ()
    assert plan.query == QUERY
    assert plan.tokens_used == 0


def test_the_granted_scopes_are_recorded_on_the_plan() -> None:
    """An audit of a past injection has to know what the grant was at the time."""

    plan = CapabilitySelector(granted_scopes=frozenset({SCOPE_FS_READ})).select(QUERY)

    assert plan.granted_scopes == (SCOPE_FS_READ,)


# --------------------------------------------------------------------------- #
# permission is not a ranking signal
# --------------------------------------------------------------------------- #


def test_an_unpermitted_skill_never_enters_the_context(skills: SkillRepository) -> None:
    """The plan's second test condition.

    ``net:request`` is outside ``DEFAULT_SCOPES`` because the network is off by
    default, so this is the real grant a run would have, not a contrived one.
    """

    activate(
        skills,
        SkillRecord(
            skill_id="publish-notes",
            version="1.0.0",
            description="publish the release notes to the changelog feed",
            body="POST the notes",
            required_scopes=(SCOPE_NETWORK,),
            triggers=("release notes",),
        ),
    )

    plan = selector(skills=skills).select(QUERY)

    assert plan.skill_labels == ()
    skip = next(item for item in plan.skipped if item.ref_id.startswith("publish-notes"))
    assert skip.reason == REASON_NOT_PERMITTED
    assert SCOPE_NETWORK in (skip.detail or "")


def test_an_unpermitted_skill_is_rejected_even_with_the_best_possible_score(
    skills: SkillRepository,
) -> None:
    """A trigger match scores 10.0, above any text score. Permission still wins:
    injecting it would only buy a tool call the policy is certain to refuse."""

    activate(
        skills,
        SkillRecord(
            skill_id="net-skill",
            version="1.0.0",
            triggers=("release notes",),
            required_scopes=(SCOPE_NETWORK,),
        ),
    )
    activate(
        skills,
        SkillRecord(
            skill_id="local-skill",
            version="1.0.0",
            description="draft the release notes locally",
            required_scopes=(SCOPE_FS_READ,),
        ),
    )

    plan = selector(skills=skills).select(QUERY)

    assert "net-skill" not in {choice.ref_id for choice in plan.selected}
    assert "local-skill" in {choice.ref_id for choice in plan.selected}


def test_a_skill_within_the_grant_is_permitted(skills: SkillRepository) -> None:
    activate(
        skills,
        SkillRecord(
            skill_id="read-notes",
            version="1.0.0",
            triggers=("release notes",),
            required_scopes=tuple(sorted(DEFAULT_SCOPES)),
        ),
    )

    plan = selector(skills=skills).select(QUERY)

    assert plan.skill_labels == ("read-notes@1.0.0",)


# --------------------------------------------------------------------------- #
# a skill that has not passed evaluation is not a candidate at all
# --------------------------------------------------------------------------- #


def test_a_candidate_skill_is_not_injected(skills: SkillRepository) -> None:
    """Not because it ranks badly — because only active versions are effective.
    This is the cross-milestone invariant: a candidate must not influence a run
    before it has passed evaluation."""

    record = SkillRecord(
        skill_id="draft-notes",
        version="0.1.0",
        description="the release notes changelog draft",
        triggers=("release notes",),
    )
    skills.register(record, session_id=SESSION_ID)
    skills.set_status("draft-notes", "0.1.0", SkillStatus.CANDIDATE, session_id=SESSION_ID)

    plan = selector(skills=skills).select(QUERY)

    assert plan.selected == ()


def test_deprecating_a_skill_takes_it_out_of_the_context(skills: SkillRepository) -> None:
    """``INJECTABLE_SKILL_STATUSES`` is ``{"active"}`` alone, so deprecation is a
    live control: it stops the version reaching a prompt without retiring it, and
    the ``deprecated -> active`` edge puts it back."""

    record = SkillRecord(
        skill_id="old-notes",
        version="1.0.0",
        triggers=("release notes",),
    )
    activate(skills, record)
    assert selector(skills=skills).select(QUERY).skill_labels == ("old-notes@1.0.0",)

    skills.set_status("old-notes", "1.0.0", SkillStatus.DEPRECATED, session_id=SESSION_ID)

    assert selector(skills=skills).select(QUERY).skill_labels == ()


# --------------------------------------------------------------------------- #
# version, source and evidence survive the injection
# --------------------------------------------------------------------------- #


def test_the_choice_carries_the_version_source_and_evidence(skills: SkillRepository) -> None:
    """The plan's third test condition. Without these on the record of the
    injection, "which version of this skill produced that answer" is unanswerable
    after the next promotion."""

    activate(
        skills,
        SkillRecord(
            skill_id="release-notes",
            version="2.1.0",
            triggers=("release notes",),
            source_path="/skills/release-notes.yaml",
            source_task="op_42",
            evidence_refs=("docs/releases/README.md", "eval-42"),
        ),
    )

    plan = selector(skills=skills).select(QUERY)

    choice = plan.selected[0]
    assert choice.version == "2.1.0"
    assert choice.source_path == "/skills/release-notes.yaml"
    assert choice.source_task == "op_42"
    assert choice.evidence_refs == ("docs/releases/README.md", "eval-42")
    payload = choice.to_payload()
    assert payload["version"] == "2.1.0"
    assert payload["evidence_refs"] == ["docs/releases/README.md", "eval-42"]


def test_a_skill_is_named_with_its_version_and_a_memory_is_not(
    skills: SkillRepository, memories: MemoryRepository, clock: FrozenClock
) -> None:
    activate(skills, SkillRecord(skill_id="notes", version="3.0.0", triggers=("release notes",)))
    record = memories.remember(
        "the changelog lives in docs/releases",
        session_id=SESSION_ID,
        layer="semantic",
    )

    explained = selector(skills=skills, memories=memories, clock=clock).select(QUERY).explain()

    refs = {item["kind"]: item["ref"] for item in explained["selected"]}  # type: ignore[index,union-attr]
    assert refs["skill"] == "notes@3.0.0"
    assert refs["memory"] == record.memory_id


def test_the_injected_text_states_the_version_and_the_required_scopes(
    skills: SkillRepository,
) -> None:
    """The model is the other audience for provenance: it should be able to say
    which skill it followed without the harness translating for it."""

    activate(
        skills,
        SkillRecord(
            skill_id="release-notes",
            version="2.1.0",
            triggers=("release notes",),
            required_scopes=(SCOPE_FS_READ,),
            evidence_refs=("docs/releases/README.md",),
        ),
    )

    content = selector(skills=skills).select(QUERY).selected[0].content

    assert "[skill:release-notes v2.1.0]" in content
    assert SCOPE_FS_READ in content
    assert "docs/releases/README.md" in content


# --------------------------------------------------------------------------- #
# expiry
# --------------------------------------------------------------------------- #


def test_an_expired_episodic_memory_is_reported_not_injected(
    memories: MemoryRepository, clock: FrozenClock
) -> None:
    """The plan's fourth test condition. Reported matters as much as not injected:
    an operator who expected that observation needs to see that it aged out rather
    than that it failed to match."""

    stale = memories.remember(
        "the changelog was empty last friday",
        session_id=SESSION_ID,
        layer="episodic",
    )
    clock.advance(14 * DAY_MS + 1)

    plan = selector(memories=memories, clock=clock).select(QUERY)

    assert plan.memory_ids == ()
    skip = next(item for item in plan.skipped if item.ref_id == stale.memory_id)
    assert skip.reason == REASON_EXPIRED
    assert skip.detail == "episodic"


def test_an_expired_memory_does_not_consume_a_slot(
    memories: MemoryRepository, clock: FrozenClock
) -> None:
    """Filtered before ranking, so it cannot displace an eligible record from a
    limit-N result. Down-weighting instead would leave exactly this hole."""

    memories.remember("changelog observation", session_id=SESSION_ID, layer="episodic")
    clock.advance(14 * DAY_MS + 1)
    for index in range(2):
        memories.remember(
            "the changelog release notes live in docs",
            session_id=SESSION_ID,
            layer="semantic",
            memory_id=f"mem_live_{index}",
        )

    plan = selector(memories=memories, clock=clock, max_memories=2).select(QUERY)

    assert set(plan.memory_ids) == {"mem_live_0", "mem_live_1"}


def test_a_durable_memory_outlives_the_episodic_window(
    memories: MemoryRepository, clock: FrozenClock
) -> None:
    memories.remember(
        "the changelog lives in docs/releases",
        session_id=SESSION_ID,
        layer="semantic",
        memory_id="mem_durable",
    )
    clock.advance(365 * DAY_MS)

    plan = selector(memories=memories, clock=clock).select(QUERY)

    assert plan.memory_ids == ("mem_durable",)


# --------------------------------------------------------------------------- #
# limits and budget
# --------------------------------------------------------------------------- #


def test_the_skill_limit_is_recorded_as_over_limit_not_dropped(
    skills: SkillRepository,
) -> None:
    for index in range(3):
        activate(
            skills,
            SkillRecord(
                skill_id=f"skill-{index}",
                version="1.0.0",
                triggers=("release notes",),
            ),
        )

    plan = selector(skills=skills, max_skills=2).select(QUERY)

    assert len(plan.skill_labels) == 2
    assert [item.reason for item in plan.skipped] == [REASON_OVER_LIMIT]


def test_the_memory_limit_is_recorded_as_over_limit(memories: MemoryRepository) -> None:
    for index in range(4):
        memories.remember(
            "the changelog release notes live in docs",
            session_id=SESSION_ID,
            layer="semantic",
            memory_id=f"mem_{index}",
        )

    plan = selector(memories=memories, max_memories=2).select(QUERY)

    assert len(plan.memory_ids) == 2
    assert [item.reason for item in plan.skipped] == [REASON_OVER_LIMIT, REASON_OVER_LIMIT]


def test_a_skill_outranks_a_memory_when_only_one_fits(
    skills: SkillRepository, memories: MemoryRepository, clock: FrozenClock
) -> None:
    """A skill is how to do the task, a memory is a fact about it. A prompt with
    room for one is better off holding the instruction."""

    activate(skills, SkillRecord(skill_id="notes", version="1.0.0", triggers=("release notes",)))
    memories.remember("the changelog lives in docs", session_id=SESSION_ID, layer="semantic")

    plan = selector(skills=skills, memories=memories, clock=clock, token_budget=12).select(QUERY)

    assert plan.skill_labels == ("notes@1.0.0",)
    assert plan.memory_ids == ()
    assert [item.reason for item in plan.skipped] == [REASON_BUDGET]


def test_the_budget_skip_says_what_did_not_fit(
    skills: SkillRepository, memories: MemoryRepository, clock: FrozenClock
) -> None:
    activate(skills, SkillRecord(skill_id="notes", version="1.0.0", triggers=("release notes",)))
    memories.remember("the changelog lives in docs", session_id=SESSION_ID, layer="semantic")

    plan = selector(skills=skills, memories=memories, clock=clock, token_budget=12).select(QUERY)

    skip = plan.skipped[0]
    assert "tokens" in (skip.detail or "")
    assert "left" in (skip.detail or "")


def test_a_budget_of_one_token_selects_nothing_rather_than_overflowing(
    skills: SkillRepository,
) -> None:
    activate(skills, SkillRecord(skill_id="notes", version="1.0.0", triggers=("release notes",)))

    plan = selector(skills=skills, token_budget=1).select(QUERY)

    assert plan.selected == ()
    assert plan.tokens_used == 0


def test_tokens_used_never_exceeds_the_budget(
    skills: SkillRepository, memories: MemoryRepository, clock: FrozenClock
) -> None:
    """A tight budget sheds the items it cannot afford; it does not empty the slot.
    ``skill-0`` is small and sorts first among equal trigger scores, so first-fit
    takes it and then refuses the rest."""

    activate(
        skills,
        SkillRecord(skill_id="skill-0", version="1.0.0", triggers=("release notes",)),
    )
    for index in (1, 2):
        activate(
            skills,
            SkillRecord(
                skill_id=f"skill-{index}",
                version="1.0.0",
                body="follow the release notes procedure " * 20,
                triggers=("release notes",),
            ),
        )
    for index in range(5):
        memories.remember(
            "the changelog release notes live in docs " * 10,
            session_id=SESSION_ID,
            layer="semantic",
            memory_id=f"mem_{index}",
        )

    plan = selector(skills=skills, memories=memories, clock=clock, token_budget=60).select(QUERY)

    assert plan.skill_labels == ("skill-0@1.0.0",)
    assert plan.tokens_used <= 60
    assert {skip.reason for skip in plan.skipped} == {REASON_BUDGET}


# --------------------------------------------------------------------------- #
# rendering into the capability slot
# --------------------------------------------------------------------------- #


def test_the_plan_renders_into_the_capability_slot_only(
    skills: SkillRepository, memories: MemoryRepository, clock: FrozenClock
) -> None:
    activate(skills, SkillRecord(skill_id="notes", version="1.0.0", triggers=("release notes",)))
    memories.remember("the changelog lives in docs", session_id=SESSION_ID, layer="semantic")

    items = selector(skills=skills, memories=memories, clock=clock).select(QUERY).items()

    assert items
    assert {item.slot for item in items} == {Slot.CAPABILITY}
    assert {item.role for item in items} == {Role.SYSTEM}


def test_the_selector_score_travels_with_the_item_so_the_compiler_sheds_the_same_one(
    skills: SkillRepository,
) -> None:
    """Two orderings exist in the compiler; this one is relevance. Carrying the
    score through means a prompt budget tighter than the capability budget drops
    the item this selector would have dropped next."""

    activate(
        skills, SkillRecord(skill_id="triggered", version="1.0.0", triggers=("release notes",))
    )
    activate(
        skills,
        SkillRecord(
            skill_id="text-only",
            version="1.0.0",
            description="the changelog release notes",
        ),
    )

    items = selector(skills=skills).select(QUERY).items()

    assert [item.key for item in items] == ["skill:triggered", "skill:text-only"]
    assert items[0].relevance > items[1].relevance


def test_the_item_key_namespaces_the_kind(memories: MemoryRepository, clock: FrozenClock) -> None:
    """A skill and a memory could share an id; the compiler dedupes on this key."""

    record = memories.remember(
        "the changelog lives in docs", session_id=SESSION_ID, layer="semantic"
    )

    items = selector(memories=memories, clock=clock).select(QUERY).items()

    assert items[0].key == f"memory:{record.memory_id}"


# --------------------------------------------------------------------------- #
# the payload written to the event
# --------------------------------------------------------------------------- #


def test_the_payload_holds_both_halves_of_the_decision(
    skills: SkillRepository, memories: MemoryRepository, clock: FrozenClock
) -> None:
    activate(
        skills,
        SkillRecord(
            skill_id="net-skill",
            version="1.0.0",
            triggers=("release notes",),
            required_scopes=(SCOPE_NETWORK,),
        ),
    )
    activate(skills, SkillRecord(skill_id="notes", version="1.0.0", triggers=("release notes",)))
    memories.remember("the changelog lives in docs", session_id=SESSION_ID, layer="semantic")

    plan = selector(skills=skills, memories=memories, clock=clock).select(QUERY)
    payload = plan.to_payload(iteration=3)

    assert payload["iteration"] == 3
    assert payload["query"] == QUERY
    assert {choice["kind"] for choice in payload["selected"]} == {"skill", "memory"}  # type: ignore[index,union-attr]
    assert [skip["reason"] for skip in payload["skipped"]] == [REASON_NOT_PERMITTED]  # type: ignore[index,union-attr]
    assert payload["granted_scopes"] == sorted(DEFAULT_SCOPES)


def test_an_empty_plan_still_carries_every_key() -> None:
    """A consumer should not need to tell "no capabilities" apart from "the writer
    omitted the field"."""

    payload = CapabilityPlan().to_payload()

    assert set(payload) == {
        "query",
        "iteration",
        "token_budget",
        "tokens_used",
        "granted_scopes",
        "selected",
        "skipped",
    }
