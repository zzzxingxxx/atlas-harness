"""Fixed task sets, log-based scoring and the reports an evaluation produces."""

from atlas_harness.evals.datasets import (
    BUILTIN_DATASETS,
    DEFAULT_DATASET_NAMES,
    REGRESSION_DATASET,
    SECURITY_DATASET,
    EvalDataset,
    EvalTask,
    all_tasks,
    dataset,
    datasets,
)
from atlas_harness.evals.replay import (
    REFUSAL_MARKERS,
    RunTrace,
    TaskOutcome,
    reads_as_refusal,
    score_from_events,
    score_task,
    trace_of,
)
from atlas_harness.evals.reports import (
    DatasetReport,
    EvaluationReport,
    metrics_from_report,
    regression_against,
    render_report,
    report_for,
)

__all__ = [
    "BUILTIN_DATASETS",
    "DEFAULT_DATASET_NAMES",
    "REFUSAL_MARKERS",
    "REGRESSION_DATASET",
    "SECURITY_DATASET",
    "DatasetReport",
    "EvalDataset",
    "EvalTask",
    "EvaluationReport",
    "RunTrace",
    "TaskOutcome",
    "all_tasks",
    "dataset",
    "datasets",
    "metrics_from_report",
    "reads_as_refusal",
    "regression_against",
    "render_report",
    "report_for",
    "score_from_events",
    "score_task",
    "trace_of",
]
