"""Provider-neutral model contracts.

This module never imports the tool executor: a model produces *requests* to
call tools, and the agent layer decides whether any of them may run. Tool
declarations arrive as dialect-free ``{name, description, input_schema}`` dicts
from ``atlas_harness.agent.tool_declarations``; each adapter renders them into
its own wire format.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from atlas_harness.kernel.ids import new_id


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class StopReason(StrEnum):
    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    STOP_SEQUENCE = "stop_sequence"
    ERROR = "error"


class TokenUsage(BaseModel):
    """Token accounting for one model response."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def plus(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


class ModelToolCall(BaseModel):
    """A parsed request from the model to run one tool.

    ``arguments`` is only meaningful when ``valid`` is true. An invalid call is
    still carried through the loop so the failure can be fed back to the model
    as evidence instead of being silently dropped.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    raw_arguments: str = ""
    valid: bool = True
    error: str | None = None


class ModelMessage(BaseModel):
    """One conversation turn in provider-neutral form."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Role
    content: str = ""
    tool_calls: tuple[ModelToolCall, ...] = ()
    tool_call_id: str | None = None
    """Set on ``tool`` messages to bind a result back to its request."""

    name: str | None = None

    @classmethod
    def system(cls, content: str) -> ModelMessage:
        return cls(role=Role.SYSTEM, content=content)

    @classmethod
    def user(cls, content: str) -> ModelMessage:
        return cls(role=Role.USER, content=content)

    @classmethod
    def assistant(
        cls, content: str = "", *, tool_calls: tuple[ModelToolCall, ...] = ()
    ) -> ModelMessage:
        return cls(role=Role.ASSISTANT, content=content, tool_calls=tool_calls)

    @classmethod
    def tool(cls, *, tool_call_id: str, content: str, name: str | None = None) -> ModelMessage:
        return cls(role=Role.TOOL, content=content, tool_call_id=tool_call_id, name=name)


class ModelRequest(BaseModel):
    """Everything a provider needs for one streamed completion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str
    messages: tuple[ModelMessage, ...]
    tools: tuple[dict[str, Any], ...] = ()
    """Declarations in the neutral ``{name, description, input_schema}`` shape."""

    max_output_tokens: int | None = Field(default=None, gt=0)
    temperature: float | None = Field(default=None, ge=0, le=2)
    stop: tuple[str, ...] = ()
    request_id: str = Field(default_factory=lambda: new_id("req"))

    def summary(self) -> dict[str, Any]:
        """Loggable shape. Message content is counted, never copied."""

        return {
            "request_id": self.request_id,
            "model": self.model,
            "message_count": len(self.messages),
            "tool_count": len(self.tools),
            "max_output_tokens": self.max_output_tokens,
            "roles": [message.role.value for message in self.messages],
        }


class ModelCapabilities(BaseModel):
    """What one model can do, so the loop can refuse impossible requests."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    model: str
    supports_tools: bool = True
    supports_streaming: bool = True
    supports_thinking: bool = False
    max_context_tokens: int = Field(default=128_000, gt=0)
    max_output_tokens: int = Field(default=4_096, gt=0)


class TokenInput(BaseModel):
    """Input for a token count, kept separate from a full request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    messages: tuple[ModelMessage, ...] = ()
    tools: tuple[dict[str, Any], ...] = ()
    text: str | None = None


class ModelEventBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TextDelta(ModelEventBase):
    event: Literal["text_delta"] = "text_delta"
    text: str


class ThinkingDelta(ModelEventBase):
    event: Literal["thinking_delta"] = "thinking_delta"
    text: str


class ToolCallStarted(ModelEventBase):
    event: Literal["tool_call_started"] = "tool_call_started"
    index: int = Field(ge=0)
    call_id: str | None = None
    name: str | None = None


class ToolCallDelta(ModelEventBase):
    event: Literal["tool_call_delta"] = "tool_call_delta"
    index: int = Field(ge=0)
    arguments_delta: str = ""
    call_id: str | None = None
    name: str | None = None


class ToolCallCompleted(ModelEventBase):
    event: Literal["tool_call_completed"] = "tool_call_completed"
    index: int = Field(ge=0)


class MessageCompleted(ModelEventBase):
    event: Literal["message_completed"] = "message_completed"
    stop_reason: StopReason = StopReason.END_TURN
    usage: TokenUsage = Field(default_factory=TokenUsage)


class ProviderErrorEvent(ModelEventBase):
    event: Literal["provider_error"] = "provider_error"
    message: str
    error_code: str = "provider_error"
    status_code: int | None = None
    retryable: bool = False
    attempt: int = Field(default=1, ge=1)


ModelEvent = (
    TextDelta
    | ThinkingDelta
    | ToolCallStarted
    | ToolCallDelta
    | ToolCallCompleted
    | MessageCompleted
    | ProviderErrorEvent
)
"""The seven unified stream events every provider must map onto."""


@runtime_checkable
class ModelAdapter(Protocol):
    """The single seam between the agent loop and any model provider."""

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        """Yield unified events for one completion. Must not raise mid-iteration
        for provider faults; emit :class:`ProviderErrorEvent` instead."""
        ...

    async def count_tokens(self, value: TokenInput) -> int: ...

    def capabilities(self) -> ModelCapabilities: ...
