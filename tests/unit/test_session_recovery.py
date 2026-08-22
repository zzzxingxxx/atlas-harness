"""Snapshots and the recovery triage rule.

The tests that matter here are the ones pinning :func:`classify_tool_call`. Its
ordering is M4's completion condition, and the tempting simplification — trust the
``idempotent`` flag — is wrong for every builtin write tool.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from atlas_harness.events import EventStore, EventType
from atlas_harness.events.reducer import ToolCallState
from atlas_harness.kernel.errors import RecoveryError
from atlas_harness.session import (
    CONFIRM,
    REPLAY,
    RESTORE,
    RecoveryService,
    classify_tool_call,
)

StoreFactory = Callable[..., EventStore]

SESSION_ID = "ses_recover"
OPERATION_ID = "op_recover"


def _call(
    *,
    status: str = "started",
    risk: str | None = None,
    idempotent: bool = False,
    confirmed: bool = False,
) -> ToolCallState:
    return ToolCallState(
        call_id="call-1",
        tool_name="probe",
        status=status,
        risk=risk,
        idempotent=idempotent,
        confirmed=confirmed,
        idempotency_key="key-1",
    )


# ------------------------------------------------------------------ triage rule


@pytest.mark.parametrize("status", ["succeeded", "failed"])
def test_a_settled_call_is_restored_however_dangerous_it_was(status: str) -> None:
    """The completion condition, at unit level.

    A recorded ``tool_result`` settles the call. Risk does not get a vote, because
    the side effect provably already happened.
    """

    decision = classify_tool_call(_call(status=status, risk="write", idempotent=False))

    assert decision.action == RESTORE
    assert decision.needs_confirmation is False


def test_idempotent_read_is_replayable() -> None:
    decision = classify_tool_call(_call(risk="read", idempotent=True))

    assert decision.action == REPLAY


@pytest.mark.parametrize(
    ("risk", "idempotent", "expected_reason"),
    [
        ("write", True, "idempotent but write risk"),
        ("write", False, "non-idempotent write call"),
        ("network", False, "non-idempotent network call"),
        ("read", False, "non-idempotent read call"),
    ],
)
def test_unsettled_calls_that_cannot_be_proven_safe_need_a_human(
    risk: str, idempotent: bool, expected_reason: str
) -> None:
    """``write_file`` declares itself idempotent and still lands here.

    That combination is the trap M4 exists to avoid: "idempotent" says the tool
    tolerates a repeat, not that the harness knows whether the first attempt landed.
    """

    decision = classify_tool_call(_call(risk=risk, idempotent=idempotent))

    assert decision.action == CONFIRM
    assert decision.needs_confirmation is True
    assert expected_reason in decision.reason


def test_a_call_with_no_declared_risk_is_never_guessed_at() -> None:
    decision = classify_tool_call(_call(risk=None, idempotent=True))

    assert decision.action == CONFIRM
    assert "refusing to guess" in decision.reason


def test_confirmation_unblocks_an_unsettled_call() -> None:
    decision = classify_tool_call(_call(risk="write", confirmed=True))

    assert decision.action == REPLAY
    assert "a human confirmed" in decision.reason


def test_confirmation_cannot_resurrect_a_settled_call() -> None:
    """Ordering check: the settled branch runs before the confirmed branch.

    Confirming a call that already recorded a result must not turn into a re-run,
    so this stays RESTORE even with the human's authorization present.
    """

    decision = classify_tool_call(_call(status="succeeded", risk="write", confirmed=True))

    assert decision.action == RESTORE


# ------------------------------------------------------------------- snapshots


@pytest.fixture
def recovery(store: EventStore) -> RecoveryService:
    store.append_new(
        EventType.SESSION_CREATED,
        session_id=SESSION_ID,
        payload={"title": "recover"},
    )
    store.append_new(
        EventType.OPERATION_STARTED,
        session_id=SESSION_ID,
        operation_id=OPERATION_ID,
        payload={"name": "chat"},
    )
    return RecoveryService(store, snapshot_every=3)


def test_snapshot_round_trips_through_the_file(recovery: RecoveryService) -> None:
    record = recovery.create_snapshot(SESSION_ID)

    loaded = recovery.load_snapshot(SESSION_ID, record.snapshot_id)

    assert loaded is not None
    assert loaded.last_seq == record.last_seq
    assert loaded.checksum == record.checksum
    assert loaded.state_hash == record.state_hash


def test_snapshot_is_announced_by_an_event(recovery: RecoveryService) -> None:
    record = recovery.create_snapshot(SESSION_ID)

    state = recovery.store.load_state(SESSION_ID)
    latest = state.latest_snapshot()
    assert latest is not None
    assert latest.snapshot_id == record.snapshot_id
    assert latest.last_seq == record.last_seq


def test_maybe_snapshot_waits_for_enough_events(recovery: RecoveryService) -> None:
    assert recovery.maybe_snapshot(SESSION_ID) is None  # 2 events, threshold 3

    recovery.store.append_new(
        EventType.ASSISTANT_MESSAGE,
        session_id=SESSION_ID,
        operation_id=OPERATION_ID,
        payload={"content": "hi"},
    )

    assert recovery.maybe_snapshot(SESSION_ID) is not None


def test_maybe_snapshot_counts_from_the_previous_snapshot(recovery: RecoveryService) -> None:
    first = recovery.create_snapshot(SESSION_ID)

    assert recovery.maybe_snapshot(SESSION_ID) is None
    assert first.last_seq == 2


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda body: "{not json", id="unparseable"),
        pytest.param(
            lambda body: json.dumps({**json.loads(body), "session_id": "ses_other"}),
            id="wrong-session",
        ),
        pytest.param(
            lambda body: json.dumps({**json.loads(body), "schema_version": 99}),
            id="unsupported-schema",
        ),
        pytest.param(
            lambda body: json.dumps({**json.loads(body), "last_seq": -1}),
            id="negative-seq",
        ),
    ],
)
def test_an_untrustworthy_snapshot_is_declined_not_repaired(
    recovery: RecoveryService, mutate: Callable[[str], str]
) -> None:
    """A bad snapshot costs replay time, never correctness.

    ``load_snapshot`` returns None and recovery falls back to the whole log, which
    is the one source that is always authoritative.
    """

    record = recovery.create_snapshot(SESSION_ID)
    record.path.write_text(mutate(record.path.read_text(encoding="utf-8")), encoding="utf-8")

    assert recovery.load_snapshot(SESSION_ID, record.snapshot_id) is None


def test_a_missing_snapshot_file_is_declined(recovery: RecoveryService) -> None:
    record = recovery.create_snapshot(SESSION_ID)
    record.path.unlink()

    assert recovery.load_snapshot(SESSION_ID, record.snapshot_id) is None


def test_plan_falls_back_to_full_replay_when_the_snapshot_is_unusable(
    recovery: RecoveryService,
) -> None:
    record = recovery.create_snapshot(SESSION_ID)
    record.path.unlink()

    plan = recovery.plan(SESSION_ID)

    assert plan.snapshot_id is None
    assert plan.resumed_from_seq == 0
    assert plan.last_valid_seq == 3


# -------------------------------------------------------------------- planning


def test_plan_refuses_an_unknown_session(recovery: RecoveryService) -> None:
    with pytest.raises(RecoveryError) as excinfo:
        recovery.plan("ses_missing")

    assert excinfo.value.details["session_id"] == "ses_missing"


def test_plan_reports_an_unanswered_model_request(recovery: RecoveryService) -> None:
    recovery.store.append_new(
        EventType.MODEL_REQUESTED,
        session_id=SESSION_ID,
        operation_id=OPERATION_ID,
        payload={"provider": "stub", "model": "stub-1", "prompt": "hi"},
    )

    plan = recovery.plan(SESSION_ID)

    assert [op.operation_id for op in plan.operations] == [OPERATION_ID]
    assert plan.operations[0].model_request_incomplete is True
    assert plan.needs_confirmation is False


def test_command_points_at_inspect_when_nothing_is_open(store: EventStore) -> None:
    store.append_new(EventType.SESSION_CREATED, session_id=SESSION_ID, payload={"title": "x"})

    plan = RecoveryService(store).plan(SESSION_ID)

    assert plan.operations == []
    assert plan.command() == f"atlas inspect {SESSION_ID}"


def test_command_lists_every_call_that_owes_a_confirmation(recovery: RecoveryService) -> None:
    recovery.store.append_new(
        EventType.TOOL_STARTED,
        session_id=SESSION_ID,
        operation_id=OPERATION_ID,
        payload={"tool_name": "write_file", "call_id": "c9", "risk": "write", "idempotent": True},
    )

    plan = recovery.plan(SESSION_ID)

    assert plan.command() == f"atlas resume {SESSION_ID} --confirm c9"


# --------------------------------------------------- suspend / resume / abort


def _start_write_call(store: EventStore, call_id: str = "c1") -> None:
    store.append_new(
        EventType.TOOL_STARTED,
        session_id=SESSION_ID,
        operation_id=OPERATION_ID,
        payload={
            "tool_name": "write_file",
            "call_id": call_id,
            "risk": "write",
            "idempotent": True,
            "idempotency_key": f"key-{call_id}",
        },
    )


def test_suspend_from_plan_records_the_reason_per_call(recovery: RecoveryService) -> None:
    _start_write_call(recovery.store)

    written = recovery.suspend_from_plan(recovery.plan(SESSION_ID))

    assert len(written) == 1
    payload = written[0].payload.model_dump(mode="json")
    assert payload["pending_tool_call_ids"] == ["c1"]
    assert "c1:" in (payload["detail"] or "")
    state = recovery.store.load_state(SESSION_ID)
    assert state.operations[OPERATION_ID].status == "suspended"


def test_suspend_from_plan_is_idempotent(recovery: RecoveryService) -> None:
    """A second startup must not stack another suspension on the same operation."""

    _start_write_call(recovery.store)
    recovery.suspend_from_plan(recovery.plan(SESSION_ID))

    assert recovery.suspend_from_plan(recovery.plan(SESSION_ID)) == []


def test_resume_without_the_confirmation_leaves_it_suspended(recovery: RecoveryService) -> None:
    _start_write_call(recovery.store)

    plan = recovery.resume(SESSION_ID)

    assert plan.needs_confirmation is True
    assert plan.operations[0].status == "suspended"


def test_resume_with_the_confirmation_clears_the_block(recovery: RecoveryService) -> None:
    _start_write_call(recovery.store)
    recovery.suspend_from_plan(recovery.plan(SESSION_ID))

    plan = recovery.resume(SESSION_ID, confirm=["c1"])

    assert plan.needs_confirmation is False
    assert plan.operations[0].decisions[0].action == REPLAY


def test_a_confirmation_survives_a_second_crash(recovery: RecoveryService) -> None:
    """The authorization is folded onto the call, not just onto the resume event.

    Otherwise a crash right after the resume would ask the same question forever.
    """

    _start_write_call(recovery.store)
    recovery.resume(SESSION_ID, confirm=["c1"])

    replanned = recovery.plan(SESSION_ID)

    assert replanned.needs_confirmation is False
    assert replanned.state.operations[OPERATION_ID].tool_calls["c1"].confirmed is True


def test_partially_answering_is_refused_rather_than_half_applied(
    recovery: RecoveryService,
) -> None:
    """Confirming one of two calls is an error, not a partial success.

    The operation cannot resume while `c2` is unanswered, so `c1`'s confirmation would
    have nowhere to be recorded — it only becomes durable via ``operation_resumed``.
    Accepting the call would look like it worked and then forget.
    """

    _start_write_call(recovery.store, "c1")
    _start_write_call(recovery.store, "c2")
    before = recovery.store.load_state(SESSION_ID).last_seq

    with pytest.raises(RecoveryError) as excinfo:
        recovery.resume(SESSION_ID, confirm=["c1"])

    assert excinfo.value.details["confirmed_tool_call_ids"] == ["c1"]
    assert excinfo.value.details["missing_tool_call_ids"] == ["c2"]
    assert recovery.store.load_state(SESSION_ID).last_seq == before, "refusal wrote nothing"


def test_confirming_every_pending_call_resumes_the_operation(recovery: RecoveryService) -> None:
    _start_write_call(recovery.store, "c1")
    _start_write_call(recovery.store, "c2")

    plan = recovery.resume(SESSION_ID, confirm=["c1", "c2"])

    assert plan.needs_confirmation is False
    calls = plan.state.operations[OPERATION_ID].tool_calls
    assert [calls["c1"].confirmed, calls["c2"].confirmed] == [True, True]


def test_resume_rejects_a_confirmation_for_an_unknown_call(recovery: RecoveryService) -> None:
    _start_write_call(recovery.store)

    with pytest.raises(RecoveryError) as excinfo:
        recovery.resume(SESSION_ID, confirm=["nope"])

    assert excinfo.value.details["unknown_tool_call_ids"] == ["nope"]


def test_abort_closes_every_unfinished_operation(recovery: RecoveryService) -> None:
    _start_write_call(recovery.store)

    written = recovery.abort(SESSION_ID, reason="operator gave up")

    assert [event.operation_id for event in written] == [OPERATION_ID]
    state = recovery.store.load_state(SESSION_ID)
    assert state.operations[OPERATION_ID].status == "aborted"
    assert recovery.plan(SESSION_ID).operations == []
