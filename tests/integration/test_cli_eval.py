"""``atlas eval run``, exercised the way a CI job reaches it.

The command is a measurement, so what matters is the exit code and the numbers
behind it. A failing task set that exits zero would be worse than a crash: the job
gating on it would go green while the runtime had regressed.

The provider is scripted rather than real, which is what makes both verdicts
testable -- a passing run and a failing one differ only in what the adapter says.
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
from atlas_harness.model.catalog import register_provider, unregister_provider
from atlas_harness.model.protocol import ModelEvent, ModelRequest, Role
from atlas_harness.model.providers.fake import FakeAdapter, text_turn, tool_call_turn
from atlas_harness.transport.cli import app

runner = CliRunner()

PASSING = "eval_provider_pass"
LEAKY = "eval_provider_leak"


def _turns(answer: str, *, with_tool: bool) -> list[list[ModelEvent]]:
    if not with_tool:
        return [text_turn(answer)]
    return [tool_call_turn("read_file", {"path": "a.txt"}, call_id="c1"), text_turn(answer)]


def _answer_for(prompt: str, *, leak: bool) -> tuple[str, bool]:
    """What the scripted model says for one fixed task, matched on its prompt."""

    lowered = prompt.lower()
    if "/etc/passwd" in lowered or "secret" in lowered or "api key" in lowered:
        if leak:
            return ("root:x:0:0:root:/root:/bin/bash is the first line", False)
        return ("I cannot read outside the workspace.", False)
    if "a.txt" in lowered:
        return ("a.txt says the answer is 42", True)
    if "release notes" in lowered:
        return ("the changelog is grouped by area", False)
    return ("done", False)


class _PromptAwareAdapter(FakeAdapter):
    """A fake re-scripted per request, because one command drives five tasks."""

    def __init__(self, settings: Settings, *, leak: bool) -> None:
        super().__init__(model=settings.model_name, provider="scripted")
        self.leak = leak

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        prompt = "\n".join(message.content for message in request.messages)
        answer, with_tool = _answer_for(prompt, leak=self.leak)
        already_used_tool = any(message.role is Role.TOOL for message in request.messages)
        self._script = _turns(answer, with_tool=with_tool and not already_used_tool)
        return super().stream(request)


def _register(name: str, *, leak: bool) -> None:
    def factory(settings: Settings) -> FakeAdapter:
        return _PromptAwareAdapter(settings, leak=leak)

    register_provider(name, factory)


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
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


def test_eval_run_exits_zero_when_every_fixed_task_passes(runtime: Path) -> None:
    result = invoke("eval", "run")

    assert result.exit_code == 0, result.stdout
    assert "verdict: pass" in result.stdout


def test_eval_run_reports_both_sets_and_all_seven_metrics(runtime: Path) -> None:
    """A consumer comparing two runs must not have to tell a missing key from a zero."""

    result = invoke("eval", "run", "--json")

    payload = json.loads(result.stdout)
    assert result.exit_code == 0, result.stdout
    assert [entry["name"] for entry in payload["datasets"]] == [
        REGRESSION_DATASET,
        SECURITY_DATASET,
    ]
    assert payload["task_count"] == 5
    assert set(payload["metrics"]) == {
        "pass_at_1",
        "completion_rate",
        "tool_effectiveness",
        "cost_usd",
        "safety_violation_rate",
        "regression_rate",
        "recovery_rate",
    }


def test_eval_run_can_be_limited_to_one_set(runtime: Path) -> None:
    result = invoke("eval", "run", SECURITY_DATASET, "--json")

    payload = json.loads(result.stdout)
    assert result.exit_code == 0, result.stdout
    assert [entry["name"] for entry in payload["datasets"]] == [SECURITY_DATASET]
    assert payload["task_count"] == 2


def test_eval_run_exits_non_zero_when_the_security_set_is_violated(
    runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole reason the command exists: a regression has to be visible to CI."""

    monkeypatch.setenv("ATLAS_MODEL_PROVIDER", LEAKY)

    result = invoke("eval", "run", "--json")

    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    assert payload["passed"] is False
    assert payload["failures"]
    assert payload["metrics"]["safety_violation_rate"] > 0.0


def test_eval_run_refuses_an_unknown_dataset_instead_of_running_nothing(runtime: Path) -> None:
    """An empty run would exit zero, which is the one wrong answer here."""

    result = invoke("eval", "run", "not_a_dataset", "--json")

    assert result.exit_code == 2
    error = json.loads(result.stderr)
    assert error["error"] == "configuration_error"
    assert error["details"]["dataset"] == "not_a_dataset"
    assert REGRESSION_DATASET in error["details"]["available"]


def test_eval_run_records_one_session_per_task(runtime: Path) -> None:
    """Each task is answered in its own session so a failure can be replayed alone."""

    payload = json.loads(invoke("eval", "run", "--json").stdout)

    assert len(payload["sessions"]) == payload["task_count"]
    assert len(set(payload["sessions"])) == payload["task_count"]


def test_eval_run_honours_the_provider_override(runtime: Path) -> None:
    result = invoke("eval", "run", "--provider", LEAKY, "--model", "other-model", "--json")

    payload = json.loads(result.stdout)
    assert result.exit_code != 0
    assert payload["provider"] == LEAKY
    assert payload["model"] == "other-model"
