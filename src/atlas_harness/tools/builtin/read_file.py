"""Read a UTF-8 text file that the policy engine already vetted."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from atlas_harness.kernel.errors import ToolError
from atlas_harness.tools.manifest import (
    SCOPE_FS_READ,
    PolicyRequest,
    RiskLevel,
    Tool,
    ToolContext,
    ToolManifest,
    json_schema_for,
)
from atlas_harness.tools.redaction import looks_binary


class ReadFileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, description="Workspace-relative file path.")
    start_line: int = Field(default=1, ge=1, description="First line to return, 1-based.")
    max_lines: int | None = Field(default=None, gt=0, description="Line budget.")


class ReadFileTool(Tool):
    """Read a text file. Size, containment and denylist are enforced upstream."""

    manifest = ToolManifest(
        name="read_file",
        version="1.0.0",
        description="Read a UTF-8 text file inside the workspace, optionally a line range.",
        input_schema=json_schema_for(ReadFileInput),
        risk=RiskLevel.READ,
        scopes=(SCOPE_FS_READ,),
        idempotent=True,
        timeout_ms=10_000,
    )
    input_model = ReadFileInput

    def policy_request(self, args: ReadFileInput) -> PolicyRequest:
        return PolicyRequest(reads=(args.path,))

    async def run(self, args: ReadFileInput, context: ToolContext) -> dict[str, Any]:
        path = context.path_for(args.path)
        return await asyncio.to_thread(self._read, path, args, context)

    def _read(self, path: Path, args: ReadFileInput, context: ToolContext) -> dict[str, Any]:
        data = path.read_bytes()
        if looks_binary(data):
            raise ToolError(
                "file is not UTF-8 text",
                details={"path": context.relative(path), "reason": "binary"},
            )
        lines = data.decode("utf-8").splitlines()
        start = min(args.start_line, len(lines) + 1)
        end = len(lines) if args.max_lines is None else min(len(lines), start - 1 + args.max_lines)
        selected = lines[start - 1 : end]
        return {
            "path": context.relative(path),
            "bytes": len(data),
            "total_lines": len(lines),
            "start_line": start,
            "end_line": start + len(selected) - 1 if selected else start - 1,
            "partial": len(selected) != len(lines),
            "content": "\n".join(selected),
        }
