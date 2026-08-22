"""The agent loop: the layer that turns model requests into vetted tool calls.

This package depends on ``model`` and on ``tools``; neither depends on it. That
direction is what keeps a provider ignorant of the policy engine and a tool
ignorant of the model.
"""

from atlas_harness.agent.loop import AgentLoop, tool_declarations
from atlas_harness.agent.queues import (
    CONSUMPTION_ORDER,
    MAX_MESSAGE_CHARS,
    QueuedMessage,
    QueueManager,
    QueueName,
    QueueRequest,
    QueueSnapshot,
)
from atlas_harness.agent.service import AgentService, RunReport
from atlas_harness.agent.state import (
    DEFAULT_SYSTEM_PROMPT,
    TERMINAL_FAILURES,
    BudgetLimits,
    RunResult,
    RunState,
    StopCause,
    steer_messages,
)

__all__ = [
    "CONSUMPTION_ORDER",
    "DEFAULT_SYSTEM_PROMPT",
    "MAX_MESSAGE_CHARS",
    "TERMINAL_FAILURES",
    "AgentLoop",
    "AgentService",
    "BudgetLimits",
    "QueueManager",
    "QueueName",
    "QueueRequest",
    "QueueSnapshot",
    "QueuedMessage",
    "RunReport",
    "RunResult",
    "RunState",
    "StopCause",
    "steer_messages",
    "tool_declarations",
]
