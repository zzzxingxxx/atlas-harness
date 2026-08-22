"""A tool the model can call to compact its own context.

The plan asks for three ways to trigger a compaction: ``/compact`` from an
operator, the token threshold, and the model asking for one. This is the third.

The tool itself performs no compaction. It records the request and returns the
structured summary; the loop owns the decision and the rewrite, exactly as it
owns whether any other tool may run. A model that could rewrite its own prompt
directly would be able to drop the fixed slot, which is the one thing the
compiler guarantees it cannot do.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from atlas_harness.tools.manifest import (
    PolicyRequest,
    RiskLevel,
    Tool,
    ToolContext,
    ToolManifest,
    json_schema_for,
)

COMPACT_TOOL_NAME = "compact_context"
"""Named here so the loop can recognize the call without importing the tool class."""


class CompactContextInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(
        default="",
        max_length=400,
        description="Why the context should be compacted now.",
    )
    keep_recent: int = Field(
        default=4,
        ge=1,
        le=32,
        description="How many recent turns to keep verbatim.",
    )


class CompactContextTool(Tool):
    """Ask the harness to compact the conversation.

    Declared ``read`` risk and idempotent: it touches no file, runs no process,
    and asking twice is the same as asking once. That combination also makes it
    the one tool recovery may safely replay unattended.
    """

    manifest = ToolManifest(
        name=COMPACT_TOOL_NAME,
        version="1.0.0",
        description=(
            "Request a structured compaction of the conversation when the context is "
            "getting long. Preserves the objective, blockers, next actions and evidence "
            "references; the full history stays in the event log."
        ),
        input_schema=json_schema_for(CompactContextInput),
        risk=RiskLevel.READ,
        scopes=(),
        idempotent=True,
        timeout_ms=5_000,
    )
    input_model = CompactContextInput

    def policy_request(self, args: CompactContextInput) -> PolicyRequest:
        return PolicyRequest()

    async def run(self, args: CompactContextInput, context: ToolContext) -> dict[str, Any]:
        """Acknowledge the request. The loop compacts at the next boundary.

        Returning the request rather than a summary keeps this tool free of the
        event store: it stays a pure declaration of intent, which is what makes
        it safe to replay.
        """

        return {
            "requested": True,
            "reason": args.reason or "model requested a compaction",
            "keep_recent": args.keep_recent,
            "note": (
                "compaction will run at the next iteration boundary; the original "
                "events and artifacts are preserved"
            ),
        }
