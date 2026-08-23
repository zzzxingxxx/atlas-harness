"""Mutable per-run state for the agent loop: transcript, budgets, stop reason.

This module holds no I/O. The loop mutates a :class:`RunState` as it goes and
the values here are what the CLI reports at the end. Everything durable lives in
the event log; this is the working set for one in-flight operation.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from atlas_harness.model.protocol import ModelMessage, Role, TokenUsage

DEFAULT_SYSTEM_PROMPT = """You are AtlasHarness, a tool-using agent.

Answer the user's question using the tools you are given. Call a tool when you
need information you do not have; do not guess file contents. When a tool fails,
read the error and either fix the arguments or explain the problem. When you have
enough information, answer in plain text without calling another tool."""


class StopCause(StrEnum):
    """Why the loop stopped. Every run ends with exactly one of these."""

    COMPLETED = "completed"
    """The model answered without requesting another tool."""

    MAX_ITERATIONS = "max_iterations"
    MAX_TOOL_CALLS = "max_tool_calls"
    TOKEN_BUDGET = "token_budget"
    PROVIDER_ERROR = "provider_error"
    CANCELLED = "cancelled"
    TOOL_DENIED = "tool_denied"
    """A tool call was refused in a way the loop cannot productively retry."""


TERMINAL_FAILURES = frozenset(
    {
        StopCause.PROVIDER_ERROR,
        StopCause.CANCELLED,
    }
)
"""Causes that make an operation *failed* rather than merely *finished early*."""


class BudgetLimits(BaseModel):
    """Ceilings for one run, resolved from settings before the loop starts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_iterations: int = Field(default=12, gt=0)
    max_tool_calls: int = Field(default=48, gt=0)
    max_total_tokens: int | None = Field(default=None, gt=0)
    """``None`` means "trust the provider's context limit" rather than "unlimited"."""


class RunResult(BaseModel):
    """What one completed run reports back to its caller."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    operation_id: str
    stop_cause: StopCause
    answer: str = ""
    iterations: int = 0
    tool_calls: int = 0
    compactions: int = 0
    """How many times the prompt was compacted. Zero for a run that never needed it."""

    usage: TokenUsage = Field(default_factory=TokenUsage)
    error: str | None = None
    error_code: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.stop_cause is StopCause.COMPLETED

    def summary(self) -> dict[str, Any]:
        """Counts and codes only. The answer text is reported separately."""

        return {
            "session_id": self.session_id,
            "operation_id": self.operation_id,
            "stop_cause": self.stop_cause.value,
            "iterations": self.iterations,
            "tool_calls": self.tool_calls,
            "compactions": self.compactions,
            "answer_length": len(self.answer),
            "input_tokens": self.usage.input_tokens,
            "output_tokens": self.usage.output_tokens,
            "total_tokens": self.usage.total_tokens,
            "error": self.error,
            "error_code": self.error_code,
        }


class RunState:
    """The transcript and counters for one operation, mutated in place."""

    def __init__(
        self,
        *,
        session_id: str,
        operation_id: str,
        limits: BudgetLimits | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    ) -> None:
        self.session_id = session_id
        self.operation_id = operation_id
        self.limits = limits or BudgetLimits()
        self.iterations = 0
        self.tool_calls = 0
        self.usage = TokenUsage()
        self.compactions = 0
        self.compact_pending = False
        """Set once the soft threshold is announced, cleared by a compaction, so
        the pending event is written once per compaction cycle rather than on
        every iteration above 70%."""

        self.prompt_tokens = 0
        """Size of the last prompt actually sent. Compaction decisions read this
        rather than cumulative usage: the window holds one request, not the sum of
        every request in the run."""

        self.stop_cause: StopCause | None = None
        self.error: str | None = None
        self.error_code: str | None = None
        self._messages: list[ModelMessage] = []
        if system_prompt:
            self._messages.append(ModelMessage.system(system_prompt))

    @property
    def messages(self) -> tuple[ModelMessage, ...]:
        return tuple(self._messages)

    def add(self, message: ModelMessage) -> None:
        self._messages.append(message)

    def extend(self, messages: Iterable[ModelMessage]) -> None:
        self._messages.extend(messages)

    def add_user(self, content: str) -> None:
        self._messages.append(ModelMessage.user(content))

    def replace_messages(self, messages: Sequence[ModelMessage]) -> int:
        """Swap the transcript for a compacted one, returning how many turns went.

        Only the working set changes. Every message removed here is already in the
        event log, so this is a cache eviction rather than a deletion -- which is
        why the method is free to drop content the run still depends on knowing
        *about*: the summary that replaces it carries the parts that matter.
        """

        removed = max(0, len(self._messages) - len(messages))
        self._messages = list(messages)
        self.compactions += 1
        self.compact_pending = False
        return removed

    def last_user_text(self) -> str:
        """The most recent user turn, which is what capability retrieval queries.

        The latest turn rather than the original prompt: a steer message changes
        what the run is doing now, and retrieving against the opening request would
        keep serving the model capabilities for a task it has moved on from.
        """

        for message in reversed(self._messages):
            if message.role is Role.USER and message.content:
                return message.content
        return ""

    def last_assistant_text(self) -> str:
        """The most recent non-empty assistant text, i.e. the answer so far."""

        for message in reversed(self._messages):
            if message.role is Role.ASSISTANT and message.content:
                return message.content
        return ""

    def record_usage(self, usage: TokenUsage) -> None:
        self.usage = self.usage.plus(usage)

    def stop(self, cause: StopCause, *, error: str | None = None, code: str | None = None) -> None:
        """Record the first stop cause. Later calls do not overwrite it."""

        if self.stop_cause is not None:
            return
        self.stop_cause = cause
        self.error = error
        self.error_code = code

    @property
    def stopped(self) -> bool:
        return self.stop_cause is not None

    def iteration_budget_left(self) -> bool:
        return self.iterations < self.limits.max_iterations

    def tool_budget_left(self, wanted: int = 1) -> bool:
        return self.tool_calls + wanted <= self.limits.max_tool_calls

    def token_budget_left(self) -> bool:
        limit = self.limits.max_total_tokens
        return limit is None or self.usage.total_tokens < limit

    def result(self, *, answer: str | None = None) -> RunResult:
        return RunResult(
            session_id=self.session_id,
            operation_id=self.operation_id,
            stop_cause=self.stop_cause or StopCause.COMPLETED,
            answer=self.last_assistant_text() if answer is None else answer,
            iterations=self.iterations,
            tool_calls=self.tool_calls,
            compactions=self.compactions,
            usage=self.usage,
            error=self.error,
            error_code=self.error_code,
        )

    def transcript_summary(self) -> dict[str, int]:
        """Message counts per role, safe to log because it carries no content."""

        counts: dict[str, int] = {}
        for message in self._messages:
            counts[message.role.value] = counts.get(message.role.value, 0) + 1
        return counts


def steer_messages(contents: Sequence[str], *, prefix: str = "") -> tuple[ModelMessage, ...]:
    """Turn drained queue content into user messages for the next request."""

    return tuple(
        ModelMessage.user(f"{prefix}{content}" if prefix else content)
        for content in contents
        if content
    )
