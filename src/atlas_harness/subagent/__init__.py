"""Sub-agents: child runs with a narrower contract than the parent that spawned them.

A sub-agent shares exactly one thing with its parent: the event log. It does not
share the parent's session, its queues, its registry or its approval gate. That is
the whole design. A child that could reach into the parent's mutable state would
make the parent's own limits meaningless -- delegation would become a way to spend
a budget nobody agreed to, using tools nobody granted.

What comes back is a result and references to the child's own events. The child's
session is never folded into the parent's projection, so those references are the
only durable link between an answer and the work behind it.
"""

from atlas_harness.subagent.runner import SubagentRunner
from atlas_harness.subagent.task import (
    SubagentResult,
    SubagentTask,
    SubagentTaskError,
)

__all__ = [
    "SubagentResult",
    "SubagentRunner",
    "SubagentTask",
    "SubagentTaskError",
]
