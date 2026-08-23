"""Memory records, their provenance, and retrieval that ranks the same way twice.

Two of the plan's four M6 test conditions live here: the same task must produce a
stable retrieval ordering, and an expired episodic memory must never be injected as
a long-term fact. Both are properties of retrieval rather than of the selector, so
they are pinned against the repository directly.

The third property these tests defend is the one that makes the other two
believable: the event log is the record and the tables are projections. Every
assertion that a row changed is paired with one about the event behind it.
"""

from __future__ import annotations

import pytest

from atlas_harness.events import EventStore, EventType
from atlas_harness.kernel.clock import FrozenClock
from atlas_harness.kernel.errors import EventValidationError
from atlas_harness.memory.models import (
    DAY_MS,
    HOUR_MS,
    LONG_TERM_LAYERS,
    MemoryLayer,
    MemoryRecord,
    expiry_for,
    parse_layer,
)
from atlas_harness.memory.repository import MemoryRepository
from atlas_harness.memory.retrieval import (
    MemoryRetriever,
    build_match_query,
    score_for,
)

SESSION_ID = "ses_memory"


@pytest.fixture
def repository(store: EventStore, clock: FrozenClock) -> MemoryRepository:
    store.append_new(
        EventType.SESSION_CREATED,
        session_id=SESSION_ID,
        payload={"title": "memory", "workspace_root": "/tmp/ws"},
    )
    return MemoryRepository(store, clock=clock)


@pytest.fixture
def retriever(repository: MemoryRepository, clock: FrozenClock) -> MemoryRetriever:
    return MemoryRetriever(repository, clock=clock)


# --------------------------------------------------------------------------- #
# layers and expiry
# --------------------------------------------------------------------------- #


def test_only_semantic_and_procedural_may_be_read_as_standing_fact() -> None:
    """The layer split is the whole reason the four layers exist separately."""

    assert LONG_TERM_LAYERS == {MemoryLayer.SEMANTIC, MemoryLayer.PROCEDURAL}
    assert MemoryLayer.EPISODIC.is_long_term is False
    assert MemoryLayer.WORKING.is_long_term is False


@pytest.mark.parametrize(
    ("layer", "expected"),
    [
        (MemoryLayer.WORKING, HOUR_MS),
        (MemoryLayer.EPISODIC, 14 * DAY_MS),
        (MemoryLayer.SEMANTIC, None),
        (MemoryLayer.PROCEDURAL, None),
    ],
)
def test_each_layer_carries_its_own_default_lifetime(
    layer: MemoryLayer, expected: int | None
) -> None:
    assert expiry_for(layer, 1_000) == (None if expected is None else 1_000 + expected)


def test_an_explicit_ttl_overrides_the_layer_default() -> None:
    assert expiry_for(MemoryLayer.SEMANTIC, 1_000, ttl_ms=500) == 1_500


def test_a_record_without_an_expiry_never_expires_on_its_own() -> None:
    """Removing a durable record is a management action, not a side effect of time."""

    record = MemoryRecord(memory_id="mem_1", layer=MemoryLayer.SEMANTIC)

    assert record.is_expired(2**62) is False


def test_expiry_is_read_off_the_record_not_inferred_from_age() -> None:
    record = MemoryRecord(memory_id="mem_1", created_at_ms=0, expires_at_ms=100)

    assert record.is_expired(99) is False
    assert record.is_expired(100) is True


def test_an_unknown_layer_is_refused() -> None:
    with pytest.raises(EventValidationError) as excinfo:
        parse_layer("long_term")

    assert excinfo.value.details["supported"] == [
        "episodic",
        "procedural",
        "semantic",
        "working",
    ]


def test_the_rendered_text_carries_the_layer_and_the_provenance() -> None:
    """A model must be able to tell an observation from a project fact."""

    text = MemoryRecord(
        memory_id="mem_1",
        layer=MemoryLayer.EPISODIC,
        content="the build broke on windows",
        confidence=0.4,
        source_task="op_7",
        evidence_refs=("src/a.py",),
    ).as_context_text()

    assert "[memory:episodic confidence=0.40]" in text
    assert "source task: op_7" in text
    assert "evidence: src/a.py" in text


# --------------------------------------------------------------------------- #
# writing: the event is the record
# --------------------------------------------------------------------------- #


def test_remembering_writes_the_event_before_the_row(repository: MemoryRepository) -> None:
    record = repository.remember(
        "release notes live in docs/releases",
        session_id=SESSION_ID,
        layer=MemoryLayer.SEMANTIC,
        source_task="op_1",
        evidence_refs=("docs/releases/README.md",),
    )

    event = repository.store.read_events(SESSION_ID)[-1]
    assert event.event_type is EventType.MEMORY_STORED
    payload = event.payload.model_dump(mode="json")
    assert payload["memory_id"] == record.memory_id
    assert payload["layer"] == "semantic"
    assert payload["evidence_refs"] == ["docs/releases/README.md"]
    # The row is a projection of that event, not a second source of truth.
    assert repository.get(record.memory_id) == record


def test_a_secret_never_reaches_the_stored_content(repository: MemoryRepository) -> None:
    """A memory outlives the run, so it outlives every downstream filter too."""

    record = repository.remember(
        "use api_key=sk-abcdefghij0123456789",
        session_id=SESSION_ID,
    )

    assert "sk-abcdefghij0123456789" not in record.content
    assert "[redacted]" in record.content
    stored = repository.store.read_events(SESSION_ID)[-1].payload.model_dump(mode="json")
    assert "sk-abcdefghij0123456789" not in stored["content"]


def test_the_layer_default_decides_the_expiry_that_is_written(
    repository: MemoryRepository, clock: FrozenClock
) -> None:
    episodic = repository.remember("ran the suite", session_id=SESSION_ID, layer="episodic")
    semantic = repository.remember("tests live in tests/", session_id=SESSION_ID, layer="semantic")

    assert episodic.expires_at_ms == clock.now_ms() + 14 * DAY_MS
    assert semantic.expires_at_ms is None


# --------------------------------------------------------------------------- #
# expiry is not deletion
# --------------------------------------------------------------------------- #


def test_expiring_keeps_the_row_and_the_original_event(repository: MemoryRepository) -> None:
    """The plan wants an explicit command, an audit record and a backup before a
    real delete. Expiry is none of those, so it removes retrievability only."""

    record = repository.remember("scratch state", session_id=SESSION_ID)

    expired = repository.expire(record.memory_id, session_id=SESSION_ID, reason="manual")

    assert expired is not None
    events = repository.store.read_events(SESSION_ID)
    assert events[-1].event_type is EventType.MEMORY_EXPIRED
    assert events[-1].payload.model_dump(mode="json")["reason"] == "manual"
    # The record itself is still there, and so is the event that created it.
    assert repository.get(record.memory_id) is not None
    assert any(event.event_type is EventType.MEMORY_STORED for event in events)


def test_expiring_removes_the_record_from_retrieval(
    repository: MemoryRepository, retriever: MemoryRetriever
) -> None:
    record = repository.remember(
        "release notes live in docs", session_id=SESSION_ID, layer="semantic"
    )
    assert [hit.record.memory_id for hit in retriever.search("release notes")] == [record.memory_id]

    repository.expire(record.memory_id, session_id=SESSION_ID)

    assert retriever.search("release notes") == []


def test_expiring_an_unknown_record_is_not_an_error(repository: MemoryRepository) -> None:
    assert repository.expire("mem_missing", session_id=SESSION_ID) is None


def test_a_sweep_retires_exactly_what_the_clock_has_passed(
    repository: MemoryRepository, clock: FrozenClock
) -> None:
    working = repository.remember("scratch", session_id=SESSION_ID, layer="working")
    durable = repository.remember("project fact", session_id=SESSION_ID, layer="semantic")
    clock.advance(HOUR_MS + 1)

    swept = repository.sweep(session_id=SESSION_ID)

    assert swept == [working.memory_id]
    assert durable.memory_id not in swept


# --------------------------------------------------------------------------- #
# retrieval
# --------------------------------------------------------------------------- #


def test_prose_that_looks_like_fts_syntax_is_quoted_not_executed(
    repository: MemoryRepository, retriever: MemoryRetriever
) -> None:
    """A bare AND or * in a task description is FTS5 syntax, not a search term.

    Quoting is what defuses it: the operator survives as a literal term rather
    than being stripped, so the query still means what the user typed.
    """

    match = build_match_query("fix AND ship the * release")

    assert '"AND"' in match
    assert '"*"' in match
    assert " OR " in match
    # And the quoted form is something FTS5 will actually run.
    repository.remember("ship the release", session_id=SESSION_ID, layer="semantic")
    assert [hit.record.content for hit in retriever.search("fix AND ship the * release")] == [
        "ship the release"
    ]


def test_an_empty_query_retrieves_nothing(retriever: MemoryRetriever) -> None:
    assert build_match_query("   ") == ""
    assert retriever.search("   ") == []


def test_the_same_task_gets_the_same_ordering_every_time(
    repository: MemoryRepository, retriever: MemoryRetriever
) -> None:
    """The plan's first test condition. BM25 alone does not give this: two equal
    scores can come back either way round, so the id breaks the tie."""

    for index in range(6):
        repository.remember(
            "the release notes live in docs and mention the changelog",
            session_id=SESSION_ID,
            layer="semantic",
            memory_id=f"mem_fixed_{index}",
        )

    runs = [
        [hit.record.memory_id for hit in retriever.search("write the release notes")]
        for _ in range(5)
    ]

    assert len(runs[0]) > 1
    assert all(run == runs[0] for run in runs)


def test_ties_are_broken_by_the_memory_id_so_the_tail_is_stable(
    repository: MemoryRepository, retriever: MemoryRetriever
) -> None:
    for memory_id in ("mem_c", "mem_a", "mem_b"):
        repository.remember(
            "identical content about the changelog",
            session_id=SESSION_ID,
            layer="semantic",
            memory_id=memory_id,
        )

    found = [hit.record.memory_id for hit in retriever.search("changelog")]

    assert found == sorted(found)


def test_the_layer_weight_outranks_a_better_text_match(
    repository: MemoryRepository, retriever: MemoryRetriever
) -> None:
    """A procedural memory that matched as well as an episodic one is the more
    useful of the two, so the ordering is deliberate rather than left to BM25."""

    repository.remember(
        "changelog",
        session_id=SESSION_ID,
        layer="episodic",
        confidence=1.0,
        memory_id="mem_episodic",
    )
    repository.remember(
        "changelog",
        session_id=SESSION_ID,
        layer="procedural",
        confidence=1.0,
        memory_id="mem_procedural",
    )

    found = [hit.record.memory_id for hit in retriever.search("changelog")]

    assert found[0] == "mem_procedural"


def test_confidence_scales_the_score() -> None:
    low = score_for(-2.0, MemoryLayer.SEMANTIC, 0.0)
    high = score_for(-2.0, MemoryLayer.SEMANTIC, 1.0)

    assert high > low


def test_the_score_reads_higher_is_better_despite_bm25_being_negative() -> None:
    """SQLite's bm25() is negative and more-negative is better. The sign flip
    happens once, here, so no downstream comparison means its own opposite."""

    better = score_for(-4.0, MemoryLayer.SEMANTIC, 0.5)
    worse = score_for(-1.0, MemoryLayer.SEMANTIC, 0.5)

    assert better > worse


def test_an_expired_episodic_memory_is_never_retrieved(
    repository: MemoryRepository, retriever: MemoryRetriever, clock: FrozenClock
) -> None:
    """The plan's fourth test condition. Expiry is checked before ranking, so a
    stale observation cannot even displace an eligible record from a limit-N set."""

    repository.remember(
        "the changelog was empty last friday",
        session_id=SESSION_ID,
        layer="episodic",
        memory_id="mem_stale",
    )
    clock.advance(14 * DAY_MS + 1)
    repository.remember(
        "the changelog lives in docs",
        session_id=SESSION_ID,
        layer="semantic",
        memory_id="mem_durable",
    )

    found = [hit.record.memory_id for hit in retriever.search("changelog")]

    assert found == ["mem_durable"]
    assert [record.memory_id for record in retriever.expired()] == ["mem_stale"]


def test_an_expired_record_is_filtered_before_the_limit_applies(
    repository: MemoryRepository, retriever: MemoryRetriever, clock: FrozenClock
) -> None:
    repository.remember(
        "changelog observation", session_id=SESSION_ID, layer="episodic", memory_id="mem_stale"
    )
    clock.advance(14 * DAY_MS + 1)
    for index in range(3):
        repository.remember(
            "changelog fact",
            session_id=SESSION_ID,
            layer="semantic",
            memory_id=f"mem_live_{index}",
        )

    found = [hit.record.memory_id for hit in retriever.search("changelog", limit=3)]

    assert len(found) == 3
    assert "mem_stale" not in found


def test_long_term_only_refuses_the_observation_layers(
    repository: MemoryRepository, retriever: MemoryRetriever
) -> None:
    repository.remember("changelog run", session_id=SESSION_ID, layer="episodic")
    durable = repository.remember("changelog path", session_id=SESSION_ID, layer="semantic")

    found = retriever.search("changelog", long_term_only=True)

    assert [hit.record.memory_id for hit in found] == [durable.memory_id]


def test_a_layer_filter_restricts_the_result(
    repository: MemoryRepository, retriever: MemoryRetriever
) -> None:
    repository.remember("changelog how-to", session_id=SESSION_ID, layer="procedural")
    semantic = repository.remember("changelog path", session_id=SESSION_ID, layer="semantic")

    found = retriever.search("changelog", layers=frozenset({MemoryLayer.SEMANTIC}))

    assert [hit.record.memory_id for hit in found] == [semantic.memory_id]


def test_retrievable_excludes_what_the_clock_has_retired(
    repository: MemoryRepository, clock: FrozenClock
) -> None:
    working = repository.remember("scratch", session_id=SESSION_ID, layer="working")
    clock.advance(HOUR_MS + 1)

    assert working.memory_id not in {record.memory_id for record in repository.retrievable()}


# --------------------------------------------------------------------------- #
# the table is a projection
# --------------------------------------------------------------------------- #


def test_the_rows_can_be_thrown_away_and_rebuilt_from_the_log(
    repository: MemoryRepository, retriever: MemoryRetriever
) -> None:
    kept = repository.remember("changelog path", session_id=SESSION_ID, layer="semantic")
    dropped = repository.remember("scratch note", session_id=SESSION_ID, layer="semantic")
    repository.expire(dropped.memory_id, session_id=SESSION_ID, reason="manual")

    count = repository.rebuild(SESSION_ID)

    assert count == 2
    # Both records come back...
    assert repository.get(kept.memory_id) is not None
    assert repository.get(dropped.memory_id) is not None
    # ...but the expiry event is replayed too, so only one is retrievable.
    found = {hit.record.memory_id for hit in retriever.search("changelog path note")}
    assert found == {kept.memory_id}
