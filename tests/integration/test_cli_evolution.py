"""The M7 loop through the real commands: feedback, propose, evaluate, promote, rollback.

The unit tests pin what each stage decides. This file pins the thing an operator
actually depends on: that the five commands compose, that the gate holds when they
are driven from a shell rather than from Python, and that a candidate which has not
passed cannot reach a prompt no matter which order the commands are run in.

Everything is real except the provider. The fake adapter needs no key and no
network, so the acceptance path runs on a fresh checkout -- and because the scripted
adapter decides what each evaluation task answers, the *verdict* is scriptable too.
That is what makes both halves of the gate testable: a candidate that passes the
fixed sets and one that breaks them differ only in what the adapter says.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from atlas_harness.config import Settings
from atlas_harness.evals.datasets import REGRESSION_DATASET, SECURITY_DATASET
from atlas_harness.events import EventStore
from atlas_harness.model.catalog import register_provider, unregister_provider
from atlas_harness.model.protocol import ModelEvent, ModelRequest, Role
from atlas_harness.model.providers.fake import FakeAdapter, text_turn, tool_call_turn
from atlas_harness.transport.cli import app

runner = CliRunner()

TASK = "op_release_notes"
CORRECTION = "always group the changelog entries by area before writing release notes"

PASSING = "provider_pass"
LEAKY = "provider_leak"


def _turns(answer: str, *, with_tool: bool) -> list[list[ModelEvent]]:
    """One script: a list of turns, each turn a list of events."""

    if not with_tool:
        return [text_turn(answer)]
    return [tool_call_turn("read_file", {"path": "a.txt"}, call_id="c1"), text_turn(answer)]


def _answer_for(prompt: str, *, leak: bool) -> tuple[str, bool]:
    """Decide what the scripted model says for one evaluation task.

    The fixed sets are matched on their prompts rather than their ids because the
    adapter only ever sees the prompt. ``leak`` flips the one answer that turns a
    passing candidate into a failing one, which is how the same harness exercises
    both sides of the gate.
    """

    lowered = prompt.lower()
    if "/etc/passwd" in lowered or "secret" in lowered or "api key" in lowered:
        # The security set expects a decline, and a decline is what it gets --
        # unless we are deliberately building the candidate that violates it.
        #
        # The violating answer complies in plain prose rather than emitting a
        # secret-shaped string. Redaction runs at the durable boundary, so an
        # ``sk-`` token would be rewritten to ``[redacted]`` before scoring ever
        # sees it -- and ``[redacted]`` reads as a refusal, which would make the
        # leak *pass* the security set. The harness is right to rescue it; that
        # just makes it the wrong lever for producing a failing candidate.
        if leak:
            return ("root:x:0:0:root:/root:/bin/bash is the first line", False)
        return ("I cannot read outside the workspace.", False)
    if "a.txt" in lowered:
        return ("a.txt says the answer is 42", True)
    if "release notes" in lowered:
        return ("the changelog is grouped by area", False)
    return ("done", False)


def _register(name: str, *, leak: bool) -> None:
    def factory(settings: Settings) -> FakeAdapter:
        return _PromptAwareAdapter(settings, leak=leak)

    register_provider(name, factory)


class _PromptAwareAdapter(FakeAdapter):
    """A fake whose script is chosen per request from the prompt it was handed.

    ``FakeAdapter`` replays a fixed script, which is the right thing for a single
    run and the wrong thing here: one ``skill-evaluate`` drives five tasks through
    five sessions, and a fixed script cannot answer a security task and a file-reading
    task differently. Re-scripting per request keeps the adapter deterministic while
    letting the fixed sets mean what they say.
    """

    def __init__(self, settings: Settings, *, leak: bool) -> None:
        super().__init__(model=settings.model_name, provider="scripted")
        self.leak = leak

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        prompt = "\n".join(message.content for message in request.messages)
        answer, with_tool = _answer_for(prompt, leak=self.leak)
        already_used_tool = any(message.role is Role.TOOL for message in request.messages)
        self._script = _turns(answer, with_tool=with_tool and not already_used_tool)
        return super().stream(request)


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A workspace, an empty log beside it, and two scripted providers."""

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.txt").write_text("the answer is 42\n", encoding="utf-8")
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ATLAS_MODEL_PROVIDER", PASSING)
    monkeypatch.setenv("ATLAS_MODEL_NAME", "fake-model")
    _register(PASSING, leak=False)
    _register(LEAKY, leak=True)
    try:
        yield workspace
    finally:
        unregister_provider(PASSING)
        unregister_provider(LEAKY)


def invoke(*args: str) -> Any:
    return runner.invoke(app, list(args))


def payload_of(*args: str) -> Any:
    result = invoke(*args, "--json")
    assert result.exit_code == 0, result.stdout
    return json.loads(result.stdout)


def record_feedback(*, task: str = TASK, content: str = CORRECTION) -> str:
    payload = payload_of(
        "feedback",
        "--record",
        content,
        "--kind",
        "correction",
        "--task",
        task,
        "--evidence",
        "ses_prior",
    )
    return str(payload["recorded"]["feedback_id"])


def propose() -> dict[str, Any]:
    payload = payload_of("skill-propose")
    accepted = [item for item in payload["outcomes"] if item["accepted"]]
    assert accepted, payload
    candidate: dict[str, Any] = accepted[0]["candidate"]
    return candidate


def event_types(tmp_path: Path) -> list[str]:
    with EventStore(tmp_path / "runtime") as store:
        return [
            event.event_type.value
            for session_id in store.list_session_ids()
            for event in store.read_events(session_id)
        ]


def session_events(tmp_path: Path, session_id: str) -> list[Any]:
    with EventStore(tmp_path / "runtime") as store:
        return list(store.read_events(session_id))


# --------------------------------------------------------------------------- #
# feedback is recorded and nothing else happens
# --------------------------------------------------------------------------- #


def test_recording_feedback_creates_no_candidate(runtime: Path) -> None:
    """The first half of the pending window: a correction is not a capability.

    An operator correcting the agent has to be able to do so without that correction
    silently becoming something the model is told next turn.
    """

    record_feedback()

    assert payload_of("candidates") == []
    assert payload_of("skills") == []


def test_feedback_is_listed_with_the_evidence_it_carries(runtime: Path) -> None:
    feedback_id = record_feedback()

    payload = payload_of("feedback")

    item = next(entry for entry in payload["items"] if entry["feedback_id"] == feedback_id)
    assert item["source_task"] == TASK
    assert item["evidence_refs"] == ["ses_prior"]


def test_a_secret_in_feedback_is_not_stored_verbatim(runtime: Path) -> None:
    """Feedback is durable and re-enters prompts through the candidate body, so the
    redaction has to happen at the write, not at the print."""

    record_feedback(content="the key sk-live-abcdefghijklmnop is what broke the deploy")

    printed = invoke("feedback").stdout
    assert "sk-live-abcdefghijklmnop" not in printed
    assert "sk-live-abcdefghijklmnop" not in json.dumps(payload_of("feedback"))


# --------------------------------------------------------------------------- #
# a proposal is a candidate, and a candidate is not injectable
# --------------------------------------------------------------------------- #


def test_a_proposal_is_registered_as_a_candidate(runtime: Path) -> None:
    record_feedback()

    candidate = propose()

    listed = payload_of("candidates")
    assert [item["candidate_id"] for item in listed] == [candidate["candidate_id"]]
    assert listed[0]["status"] == "proposed"
    assert candidate["evidence_refs"], "a candidate must name where it came from"


def test_a_proposed_version_is_not_active(runtime: Path) -> None:
    """The plan's first test condition, through the CLI: an unevaluated candidate is
    not in the effective set. ``skills`` lists what would be injected."""

    record_feedback()
    candidate = propose()

    skills = payload_of("skills")
    statuses = {item["version"]: item["status"] for item in skills}
    assert statuses.get(candidate["version"]) == "candidate"
    assert "active" not in statuses.values()


def test_proposing_from_nothing_is_refused_not_invented(runtime: Path) -> None:
    result = invoke("skill-propose", "--json")

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["considered"] == 0
    assert payload_of("candidates") == []


# --------------------------------------------------------------------------- #
# the evaluation gate
# --------------------------------------------------------------------------- #


def test_promoting_before_evaluating_is_refused(runtime: Path) -> None:
    """The plan's second condition. This is the whole point of the pending window:
    the only path to ``active`` runs through a passing verdict."""

    record_feedback()
    candidate = propose()

    result = invoke("skill-promote", candidate["candidate_id"], "--json")

    assert result.exit_code != 0
    assert "evaluat" in result.stderr.lower()
    assert payload_of("candidates")[0]["status"] == "proposed"


def test_an_evaluation_records_all_seven_metrics(runtime: Path) -> None:
    """The plan names seven metrics. A consumer comparing a candidate against a
    champion must not have to tell a missing key from a real zero."""

    record_feedback()
    candidate = propose()

    payload = payload_of("skill-evaluate", candidate["candidate_id"])

    metrics = payload["evaluation"]["metrics"]
    assert set(metrics) == {
        "pass_at_1",
        "completion_rate",
        "tool_effectiveness",
        "cost_usd",
        "safety_violation_rate",
        "regression_rate",
        "recovery_rate",
    }
    assert payload["evaluation"]["dataset"] == f"{REGRESSION_DATASET}+{SECURITY_DATASET}"
    assert payload["evaluation"]["task_count"] == 5


def test_a_passing_evaluation_can_then_be_promoted(runtime: Path) -> None:
    record_feedback()
    candidate = propose()

    evaluation = payload_of("skill-evaluate", candidate["candidate_id"])
    assert evaluation["evaluation"]["verdict"] == "pass"

    promoted = payload_of("skill-promote", candidate["candidate_id"], "--reason", "measured")

    assert promoted["skill"]["status"] == "active"
    assert promoted["skill"]["version"] == candidate["version"]
    assert payload_of("candidates")[0]["status"] == "promoted"


def test_a_failing_candidate_cannot_be_promoted(
    runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plan's second test condition and its fourth, in one run.

    The leaky provider answers the security tasks instead of declining them, so the
    candidate fails the fixed sets on its own evidence -- no judge involved -- and the
    promotion is then refused. A new skill that degrades the security set does not
    become effective.
    """

    monkeypatch.setenv("ATLAS_MODEL_PROVIDER", LEAKY)
    record_feedback()
    candidate = propose()

    failing = invoke("skill-evaluate", candidate["candidate_id"], "--json")
    assert failing.exit_code != 0, "a failing verdict has to be visible to CI"
    verdict = json.loads(failing.stdout)["evaluation"]
    assert verdict["verdict"] == "fail"
    assert verdict["metrics"]["safety_violation_rate"] > 0.0
    assert verdict["failures"]

    refused = invoke("skill-promote", candidate["candidate_id"], "--json")

    assert refused.exit_code != 0
    assert "active" not in [item["status"] for item in payload_of("skills")]


def test_a_failed_candidate_is_marked_examined_not_pending(
    runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leaving it pending would make it look unexamined, and the next operator to
    work the pending list would pay to measure it again."""

    monkeypatch.setenv("ATLAS_MODEL_PROVIDER", LEAKY)
    record_feedback()
    candidate = propose()
    invoke("skill-evaluate", candidate["candidate_id"], "--json")

    assert payload_of("candidates")[0]["status"] == "evaluated"
    assert payload_of("candidates", "--status", "proposed") == []


def test_evaluating_an_unknown_candidate_is_refused(runtime: Path) -> None:
    result = invoke("skill-evaluate", "cand_absent", "--json")

    assert result.exit_code != 0
    assert json.loads(result.stderr)["error"]


# --------------------------------------------------------------------------- #
# promotion reaches the prompt; rollback takes it back
# --------------------------------------------------------------------------- #


def test_a_promoted_skill_reaches_the_next_run(runtime: Path, tmp_path: Path) -> None:
    """The consequence of promotion, which is the only reason the gate matters: an
    active version is injected, and the log names the version that was read."""

    record_feedback()
    candidate = propose()
    payload_of("skill-evaluate", candidate["candidate_id"])
    payload_of("skill-promote", candidate["candidate_id"])

    run = payload_of("run", "write the release notes")

    injected = [
        event
        for event in session_events(tmp_path, run["session_id"])
        if event.event_type.value == "capability_injected"
    ]
    assert injected, "a promoted skill has to reach the capability slot"
    chosen = [
        selection["ref_id"]
        for event in injected
        for selection in event.payload.model_dump(mode="python")["selected"]
        if selection["kind"] == "skill"
    ]
    assert candidate["skill_id"] in chosen


def test_a_promotion_can_be_rolled_back_to_a_named_version(runtime: Path) -> None:
    """The plan's third test condition. Two versions have to reach ``active`` for a
    rollback to have a target, so the second is proposed against the first's skill."""

    record_feedback()
    first = propose()
    payload_of("skill-evaluate", first["candidate_id"])
    payload_of("skill-promote", first["candidate_id"])

    record_feedback(content="also mention the migration steps in the release notes")
    second = payload_of("skill-propose", "--skill", first["skill_id"])
    candidate = next(item["candidate"] for item in second["outcomes"] if item["accepted"])
    assert candidate["version"] != first["version"]
    payload_of("skill-evaluate", candidate["candidate_id"])
    payload_of("skill-promote", candidate["candidate_id"])

    rolled = payload_of("skill-rollback", first["skill_id"], "--to", first["version"])

    assert rolled["skill"]["version"] == first["version"]
    assert rolled["skill"]["status"] == "active"
    effective = {
        item["version"]: item["status"]
        for item in payload_of("skills")
        if item["status"] == "active"
    }
    assert list(effective) == [first["version"]]


def test_rolling_back_to_a_version_that_never_existed_is_refused(runtime: Path) -> None:
    """A version the library never had has no evaluation behind it, so making it
    effective would be a promotion that skipped the gate."""

    record_feedback()
    candidate = propose()
    payload_of("skill-evaluate", candidate["candidate_id"])
    payload_of("skill-promote", candidate["candidate_id"])

    result = invoke("skill-rollback", candidate["skill_id"], "--to", "9.9.9", "--json")

    assert result.exit_code != 0
    details = json.loads(result.stderr)["details"]
    assert details["to_version"] == "9.9.9"
    assert details["skill_id"] == candidate["skill_id"]
    # The rollback targets are the deprecated versions, and the only version this
    # library has is the active one -- so there is nowhere to go back to at all.
    assert details["available"] == []
    assert payload_of("skills")[0]["status"] == "active"


# --------------------------------------------------------------------------- #
# the completion condition, and the log behind it
# --------------------------------------------------------------------------- #


def test_the_whole_loop_runs_and_the_log_explains_every_step(runtime: Path, tmp_path: Path) -> None:
    """M7's completion condition: feedback -> candidate -> evaluate -> promote ->
    rollback, driven through the commands, with each step recorded as an event."""

    record_feedback()
    first = propose()
    payload_of("skill-evaluate", first["candidate_id"])
    payload_of("skill-promote", first["candidate_id"])
    record_feedback(content="also mention the migration steps in the release notes")
    second = payload_of("skill-propose", "--skill", first["skill_id"])
    candidate = next(item["candidate"] for item in second["outcomes"] if item["accepted"])
    payload_of("skill-evaluate", candidate["candidate_id"])
    payload_of("skill-promote", candidate["candidate_id"])
    payload_of("skill-rollback", first["skill_id"], "--to", first["version"])

    recorded = event_types(tmp_path)
    for required in (
        "feedback_recorded",
        "skill_candidate_proposed",
        "candidate_evaluated",
        "champion_promoted",
        "champion_rolled_back",
    ):
        assert required in recorded, f"{required} missing from {sorted(set(recorded))}"


def test_the_evaluation_left_the_candidate_inactive_in_the_library(runtime: Path) -> None:
    """A shadow run makes the candidate effective for the duration of a task and for
    nothing else. If it wrote an activation, a crash mid-evaluation would leave an
    unmeasured version serving requests."""

    record_feedback()
    candidate = propose()

    payload_of("skill-evaluate", candidate["candidate_id"])

    statuses = {item["version"]: item["status"] for item in payload_of("skills")}
    assert statuses[candidate["version"]] == "candidate"
