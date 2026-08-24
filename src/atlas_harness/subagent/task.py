"""The task contract: what a parent may ask, and what it gets back.

Every limit is declared before the child starts. The plan asks for
``allowedTools``, ``maxTokens``, ``deadlineMs`` and ``returnFormat``, and the
reason all four are on one frozen model rather than keyword arguments to a runner
is auditability: the contract is written to ``subagent_task_started`` exactly as
agreed, so a run can be judged against the terms it was given instead of against
whatever the runner happened to do.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from atlas_harness.events.models import SUBAGENT_RETURN_FORMATS
from atlas_harness.kernel.errors import AtlasError
from atlas_harness.kernel.ids import new_id

DEFAULT_DEADLINE_MS = 120_000
DEFAULT_MAX_TOKENS = 20_000
MAX_OBJECTIVE_CHARS = 4_000


class SubagentTaskError(AtlasError):
    """A task could not be accepted, or could not be run to a result."""

    code = "subagent_task_error"
    exit_code = 12


class SubagentTask(BaseModel):
    """One delegated unit of work, with its ceiling attached.

    ``allowed_tools`` is a required, explicit list. An empty tuple means the child
    gets no tools at all, which is a useful contract (summarize this, decide that)
    and a safe default for a spec that forgot to say. Inheriting the parent's whole
    registry would make delegation a privilege escalation with extra steps.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    objective: str = Field(min_length=1, max_length=MAX_OBJECTIVE_CHARS)
    task_id: str = Field(default_factory=lambda: new_id("task"))
    allowed_tools: tuple[str, ...] = ()
    max_tokens: int = Field(default=DEFAULT_MAX_TOKENS, gt=0)
    deadline_ms: int = Field(default=DEFAULT_DEADLINE_MS, gt=0)
    return_format: str = "text"
    max_iterations: int = Field(default=4, gt=0)
    max_tool_calls: int = Field(default=8, gt=0)
    """Deliberately smaller than the parent's defaults. A child is given a narrow
    job; a child that can iterate as long as its parent is not a sub-agent, it is a
    second agent sharing the parent's budget."""

    system_prompt: str = ""
    """Empty means the runner's default child prompt. A parent may narrow the
    child's instructions but never widen what it is allowed to touch, which is
    governed by ``allowed_tools`` and the policy, not by prose."""

    @field_validator("return_format")
    @classmethod
    def _known_format(cls, value: str) -> str:
        if value not in SUBAGENT_RETURN_FORMATS:
            expected = sorted(SUBAGENT_RETURN_FORMATS)
            raise ValueError(f"unknown return_format {value!r}; expected one of {expected}")
        return value

    def summary(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "objective": self.objective,
            "allowed_tools": list(self.allowed_tools),
            "max_tokens": self.max_tokens,
            "deadline_ms": self.deadline_ms,
            "return_format": self.return_format,
            "max_iterations": self.max_iterations,
            "max_tool_calls": self.max_tool_calls,
        }


class SubagentResult(BaseModel):
    """What a finished task hands back: an answer, an outcome, and its evidence.

    ``evidence_refs`` are event references in the *child's* session. The child's
    events are never merged into the parent's projection, so a parent reading this
    result has no other way to reach the work that produced it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    child_session_id: str
    outcome: str
    result: str = ""
    error: str | None = None
    error_code: str | None = None
    tool_calls: int = 0
    total_tokens: int = 0
    duration_ms: int = 0
    evidence_refs: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.outcome == "completed"

    def summary(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "child_session_id": self.child_session_id,
            "outcome": self.outcome,
            "result_length": len(self.result),
            "error": self.error,
            "error_code": self.error_code,
            "tool_calls": self.tool_calls,
            "total_tokens": self.total_tokens,
            "duration_ms": self.duration_ms,
            "evidence_refs": list(self.evidence_refs),
        }
