"""Turning per-task outcomes into the seven numbers the plan requires.

The plan names the metrics: pass@1, completion rate, tool effectiveness, cost,
safety violation rate, regression rate and recovery rate. They are computed here, in
one place, so a candidate and the champion it is compared against are always measured
the same way. Two evaluations scored by different code are not a comparison, and the
promotion decision *is* a comparison.

Rates are fractions of the tasks that actually ran. A rate over zero tasks is ``0.0``
rather than an error, but ``task_count`` travels beside it so a consumer can tell
"nothing failed" from "nothing ran" -- those are identical in the number alone, and
only one of them means the candidate is safe.

Outcomes come from :mod:`atlas_harness.evals.replay`; this module never defines its
own, so there is exactly one shape a score can have.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from pydantic import BaseModel, ConfigDict, Field

from atlas_harness.evals.datasets import SECURITY_DATASET
from atlas_harness.evals.replay import TaskOutcome
from atlas_harness.events.models import EvaluationMetrics


class DatasetReport(BaseModel):
    """One dataset's outcomes, kept separate so the sets stay readable.

    Pooling the security set into the regression set would let a candidate offset a
    refusal it should have made against extra tasks it happened to pass, and the
    plan's condition is that the security set specifically must not degrade.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    outcomes: tuple[TaskOutcome, ...] = Field(default_factory=tuple)

    @property
    def task_count(self) -> int:
        return len(self.outcomes)

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(outcome.task_id for outcome in self.outcomes if not outcome.passed)

    @property
    def passed(self) -> bool:
        return bool(self.outcomes) and not self.failures

    @property
    def safety_violations(self) -> tuple[str, ...]:
        return tuple(outcome.task_id for outcome in self.outcomes if outcome.safety_violation)


class EvaluationReport(BaseModel):
    """Every dataset's outcomes plus the metrics derived from all of them."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    datasets: tuple[DatasetReport, ...] = Field(default_factory=tuple)
    metrics: EvaluationMetrics = Field(default_factory=EvaluationMetrics)

    @property
    def outcomes(self) -> tuple[TaskOutcome, ...]:
        return tuple(outcome for report in self.datasets for outcome in report.outcomes)

    @property
    def task_count(self) -> int:
        return len(self.outcomes)

    @property
    def failures(self) -> tuple[str, ...]:
        """Task ids that did not pass, in dataset order, so a report names what
        broke rather than only how much did."""

        return tuple(task_id for report in self.datasets for task_id in report.failures)

    @property
    def dataset_names(self) -> tuple[str, ...]:
        return tuple(report.name for report in self.datasets)

    @property
    def passed(self) -> bool:
        """Every task in every set passed. An empty report is not a pass: it would
        otherwise be the cheapest way to promote a candidate."""

        return bool(self.outcomes) and not self.failures

    def dataset(self, name: str) -> DatasetReport | None:
        return next((report for report in self.datasets if report.name == name), None)


def _rate(count: int, total: int) -> float:
    return 0.0 if total <= 0 else count / total


def metrics_from_report(
    outcomes: Sequence[TaskOutcome],
    *,
    baseline: EvaluationMetrics | None = None,
) -> EvaluationMetrics:
    """Compute the seven plan metrics over one run's outcomes.

    ``baseline`` affects only the regression rate, the one metric that cannot be read
    off a single run: a task failing now is a regression only if it used to pass.
    With nothing to compare against, the rate is the plain failure rate on the
    non-security sets, which is the honest reading of "there is no baseline".
    """

    total = len(outcomes)
    if total == 0:
        return EvaluationMetrics()

    passed = sum(1 for outcome in outcomes if outcome.passed)
    violations = sum(1 for outcome in outcomes if outcome.safety_violation)
    completed = sum(1 for outcome in outcomes if outcome.passed or outcome.refused)
    with_tools = [outcome for outcome in outcomes if outcome.tools_expected]
    satisfied = sum(1 for outcome in with_tools if outcome.tools_satisfied)
    disrupted = [outcome for outcome in outcomes if outcome.had_tool_failure]
    recovered = sum(1 for outcome in disrupted if outcome.recovered)
    regression_tasks = [outcome for outcome in outcomes if outcome.dataset != SECURITY_DATASET]
    regressed = sum(1 for outcome in regression_tasks if not outcome.passed)

    regression_rate = _rate(regressed, len(regression_tasks))
    if baseline is not None and baseline.pass_at_1 <= 0.0:
        # Nothing passed before, so nothing here can be a *re*gression.
        regression_rate = 0.0

    return EvaluationMetrics(
        pass_at_1=_rate(passed, total),
        completion_rate=_rate(completed, total),
        # No task required a tool means nothing was measured, so the neutral answer is
        # 1.0: reporting 0.0 would read as "every tool call failed".
        tool_effectiveness=1.0 if not with_tools else _rate(satisfied, len(with_tools)),
        cost_usd=round(sum(outcome.cost_usd for outcome in outcomes), 6),
        safety_violation_rate=_rate(violations, total),
        regression_rate=regression_rate,
        # Nothing failed means there was nothing to recover from, which is not a
        # recovery failure.
        recovery_rate=1.0 if not disrupted else _rate(recovered, len(disrupted)),
    )


def report_for(
    outcomes: Iterable[TaskOutcome],
    *,
    baseline: EvaluationMetrics | None = None,
) -> EvaluationReport:
    """Group outcomes by their dataset and aggregate them into one report."""

    grouped: dict[str, list[TaskOutcome]] = {}
    for outcome in outcomes:
        grouped.setdefault(outcome.dataset, []).append(outcome)

    reports = tuple(
        DatasetReport(name=name, outcomes=tuple(items)) for name, items in grouped.items()
    )
    pooled = tuple(outcome for report in reports for outcome in report.outcomes)
    return EvaluationReport(
        datasets=reports, metrics=metrics_from_report(pooled, baseline=baseline)
    )


def regression_against(
    candidate: EvaluationMetrics, champion: EvaluationMetrics
) -> tuple[str, ...]:
    """Metrics on which the candidate is worse than the champion.

    Returning the names rather than a boolean is what lets a refusal say *why*: an
    operator told only "regressed" has to re-run the evaluation to find out which
    number moved.

    Cost is deliberately absent. A candidate that costs more may still be worth
    promoting, and folding cost into a pass/fail would make the gate block
    improvements for a reason nobody asked it to enforce.
    """

    worse: list[str] = []
    if candidate.pass_at_1 < champion.pass_at_1:
        worse.append("pass_at_1")
    if candidate.completion_rate < champion.completion_rate:
        worse.append("completion_rate")
    if candidate.tool_effectiveness < champion.tool_effectiveness:
        worse.append("tool_effectiveness")
    if candidate.safety_violation_rate > champion.safety_violation_rate:
        worse.append("safety_violation_rate")
    return tuple(worse)


def render_report(report: EvaluationReport) -> list[str]:
    """Human-readable lines for the CLI, one per dataset plus the totals.

    The failing task ids are printed rather than summarised. "2 of 5 failed" tells an
    operator to go looking; the ids tell them where.
    """

    lines: list[str] = []
    for entry in report.datasets:
        verdict = "pass" if entry.passed else "fail"
        lines.append(
            f"{entry.name}: {verdict} ({entry.task_count - len(entry.failures)}/{entry.task_count})"
        )
        for task_id in entry.failures:
            outcome = next(item for item in entry.outcomes if item.task_id == task_id)
            lines.append(f"  - {task_id}: {outcome.failure or 'failed'}")

    metrics = report.metrics
    lines.append(
        "metrics: "
        f"pass@1={metrics.pass_at_1:.2f} "
        f"completion={metrics.completion_rate:.2f} "
        f"tools={metrics.tool_effectiveness:.2f} "
        f"cost=${metrics.cost_usd:.4f} "
        f"safety={metrics.safety_violation_rate:.2f} "
        f"regression={metrics.regression_rate:.2f} "
        f"recovery={metrics.recovery_rate:.2f}"
    )
    return lines
