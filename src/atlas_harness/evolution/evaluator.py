"""Measuring a candidate before anything is allowed to promote it.

Three stages, in the order the plan names them, each able to stop the evaluation:

``rules``
    The candidate runs the fixed task sets and is scored deterministically. This is
    the stage that can fail on its own evidence: a candidate that breaks a
    regression task or answers a security task fails here, without any model being
    asked for an opinion.

``judge``
    An optional model-graded pass over the answers the rules could not settle. It is
    optional because a judge is a network call, and a gate that cannot run without
    one would stop being a gate the first time the provider was unreachable. When a
    judge is configured but fails, the verdict is ``inconclusive`` rather than
    ``pass`` -- an unmeasured candidate is not a passing one.

``shadow``
    The champion runs the *same* tasks, and the two sets of numbers are compared.
    This is the only stage that can answer the question promotion actually asks:
    not "is the candidate good" but "is it better than what is already serving
    requests". A candidate that passes every task while scoring below the champion
    is a regression, and it fails here.

Nothing in this module writes an event or changes a status. It returns a record; the
repository stores it and :mod:`atlas_harness.evolution.champion` decides what the
record permits. Keeping the measurement separate from the consequence is what lets
an evaluation be re-run against a stored log without promoting anything.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from atlas_harness.evals.datasets import EvalDataset, EvalTask, datasets
from atlas_harness.evals.replay import RunTrace, score_task
from atlas_harness.evals.reports import (
    EvaluationReport,
    metrics_from_report,
    regression_against,
    report_for,
)
from atlas_harness.events.models import EvaluationMetrics
from atlas_harness.evolution.models import (
    EvaluationRecord,
    EvaluationVerdict,
    SkillCandidate,
)
from atlas_harness.kernel.clock import Clock, SystemClock
from atlas_harness.kernel.ids import new_id
from atlas_harness.skills.models import SkillRecord

STAGE_RULES = "rules"
STAGE_JUDGE = "judge"
STAGE_SHADOW = "shadow"

COST_PER_1K_TOKENS = 0.003
"""Flat rate used to turn token counts into the cost metric the plan asks for.

A single rate across input and output is wrong in absolute terms and right for what
the number is used for: comparing a candidate against a champion measured the same
way. An accurate per-provider table would change both sides equally and change no
decision, so it is not worth carrying here."""

TaskRunner = Callable[[EvalTask, SkillRecord | None], RunTrace]
"""Runs one task with one skill version made available, and returns what happened.

The skill is a parameter rather than ambient state because the shadow stage runs the
same task twice, once per version. ``None`` means "no candidate skill" -- the plain
baseline. Tests supply a scripted runner; the real one drives a session.
"""

Judge = Callable[[EvaluationReport], tuple[bool, tuple[str, ...]]]
"""Grades a scored report, returning a verdict and its reasons.

Raising is allowed and meaningful: a judge that cannot be reached makes the
evaluation ``inconclusive``, which blocks promotion without recording a failure the
candidate did not earn.
"""


def cost_of(trace: RunTrace) -> float:
    return round((trace.input_tokens + trace.output_tokens) / 1000.0 * COST_PER_1K_TOKENS, 6)


def run_datasets(
    runner: TaskRunner,
    skill: SkillRecord | None,
    *,
    sets: Sequence[EvalDataset],
) -> EvaluationReport:
    """Run every task in every set and score the results."""

    outcomes = []
    for item in sets:
        for task in item.tasks:
            trace = runner(task, skill)
            outcomes.append(score_task(task, trace, dataset=item.name, cost_usd=cost_of(trace)))
    return report_for(outcomes)


class Evaluator:
    """Runs the three stages and returns one record for the whole evaluation."""

    def __init__(
        self,
        *,
        runner: TaskRunner,
        judge: Judge | None = None,
        dataset_names: Sequence[str] | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.runner = runner
        self.judge = judge
        self.sets = datasets(dataset_names)
        self._clock = clock or SystemClock()

    @property
    def dataset_label(self) -> str:
        return "+".join(item.name for item in self.sets)

    def evaluate(
        self,
        candidate: SkillCandidate,
        *,
        champion: SkillRecord | None = None,
    ) -> EvaluationRecord:
        """Measure one candidate and say whether it may be promoted.

        ``champion`` is the version currently serving requests, or ``None`` when this
        skill has none yet. Without a champion the shadow stage has nothing to compare
        against, so it does not run -- and its absence is visible in ``stages`` rather
        than being reported as a stage that passed.
        """

        stages: list[str] = []
        failed: list[str] = []
        notes: list[str] = []
        verdict = EvaluationVerdict.PASS

        stages.append(STAGE_RULES)
        report = run_datasets(self.runner, candidate.to_skill_record(), sets=self.sets)
        if report.task_count == 0:
            # An empty run is the cheapest possible pass, so it is explicitly not one.
            return self._record(
                candidate,
                champion=champion,
                report=report,
                stages=tuple(stages),
                failed=(STAGE_RULES,),
                verdict=EvaluationVerdict.INCONCLUSIVE,
                notes=("no tasks ran",),
                baseline=None,
            )
        if not report.passed:
            failed.append(STAGE_RULES)
            verdict = EvaluationVerdict.FAIL
            notes.append(f"failed tasks: {', '.join(report.failures)}")

        if self.judge is not None:
            stages.append(STAGE_JUDGE)
            try:
                approved, reasons = self.judge(report)
            except Exception as error:  # noqa: BLE001 - any judge failure blocks equally
                failed.append(STAGE_JUDGE)
                notes.append(f"judge unavailable: {error}")
                verdict = EvaluationVerdict.INCONCLUSIVE
            else:
                notes.extend(reasons)
                if not approved:
                    failed.append(STAGE_JUDGE)
                    if verdict is EvaluationVerdict.PASS:
                        verdict = EvaluationVerdict.FAIL

        baseline: EvaluationMetrics | None = None
        if champion is not None:
            stages.append(STAGE_SHADOW)
            shadow = run_datasets(self.runner, champion, sets=self.sets)
            baseline = shadow.metrics
            worse = regression_against(report.metrics, baseline)
            if worse:
                failed.append(STAGE_SHADOW)
                notes.append(f"worse than {champion.label} on: {', '.join(worse)}")
                if verdict is EvaluationVerdict.PASS:
                    verdict = EvaluationVerdict.FAIL
        else:
            notes.append("no champion to compare against; shadow stage skipped")

        return self._record(
            candidate,
            champion=champion,
            report=report,
            stages=tuple(stages),
            failed=tuple(failed),
            verdict=verdict,
            notes=tuple(notes),
            baseline=baseline,
        )

    def _record(
        self,
        candidate: SkillCandidate,
        *,
        champion: SkillRecord | None,
        report: EvaluationReport,
        stages: tuple[str, ...],
        failed: tuple[str, ...],
        verdict: EvaluationVerdict,
        notes: tuple[str, ...],
        baseline: EvaluationMetrics | None,
    ) -> EvaluationRecord:
        # Recomputed with the baseline so the regression rate means "used to pass and
        # does not now" rather than the plain failure rate it defaults to.
        metrics = (
            report.metrics
            if baseline is None
            else metrics_from_report(report.outcomes, baseline=baseline)
        )
        return EvaluationRecord(
            evaluation_id=new_id("eval"),
            candidate_id=candidate.candidate_id,
            skill_id=candidate.skill_id,
            version=candidate.version,
            dataset=self.dataset_label,
            verdict=verdict,
            stages=stages,
            failed_stages=failed,
            metrics=metrics,
            baseline_metrics=baseline,
            champion_version=None if champion is None else champion.version,
            task_count=report.task_count,
            failures=report.failures,
            notes=notes,
            evaluated_at_ms=self._clock.now_ms(),
        )
