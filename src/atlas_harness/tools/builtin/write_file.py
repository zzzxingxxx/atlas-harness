"""Write a workspace file atomically, behind approval and a diff preview."""

from __future__ import annotations

import asyncio
import difflib
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from atlas_harness.kernel.errors import ToolError
from atlas_harness.tools.manifest import (
    SCOPE_FS_WRITE,
    PolicyRequest,
    RiskLevel,
    Tool,
    ToolContext,
    ToolManifest,
    json_schema_for,
)

MAX_PREVIEW_LINES = 200


class WriteFileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, description="Workspace-relative file path.")
    content: str = Field(description="Full new file content, UTF-8.")
    create_dirs: bool = Field(default=False, description="Create missing parent directories.")


class WriteFileTool(Tool):
    """Replace a file's content. Declared idempotent: the same call is a no-op."""

    manifest = ToolManifest(
        name="write_file",
        version="1.0.0",
        description="Write UTF-8 content to a workspace file, replacing it atomically.",
        input_schema=json_schema_for(WriteFileInput),
        risk=RiskLevel.WRITE,
        scopes=(SCOPE_FS_WRITE,),
        idempotent=True,
        timeout_ms=15_000,
    )
    input_model = WriteFileInput

    def policy_request(self, args: WriteFileInput) -> PolicyRequest:
        return PolicyRequest(writes=(args.path,))

    def preview(self, args: WriteFileInput, context: ToolContext) -> str | None:
        path = context.path_for(args.path)
        before = self._current_text(path, context)
        diff = difflib.unified_diff(
            (before or "").splitlines(),
            args.content.splitlines(),
            fromfile=f"a/{context.relative(path)}",
            tofile=f"b/{context.relative(path)}",
            lineterm="",
        )
        lines = list(diff)[:MAX_PREVIEW_LINES]
        if not lines:
            return f"no change to {context.relative(path)}"
        return "\n".join(lines)

    async def run(self, args: WriteFileInput, context: ToolContext) -> dict[str, Any]:
        path = context.path_for(args.path)
        return await asyncio.to_thread(self._write, path, args, context)

    def _current_text(self, path: Path, context: ToolContext) -> str | None:
        """Existing content, or None when there is nothing comparable to show."""

        if not path.is_file() or path.stat().st_size > context.max_read_bytes:
            return None
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return None

    def _write(self, path: Path, args: WriteFileInput, context: ToolContext) -> dict[str, Any]:
        relative = context.relative(path)
        if path.is_dir():
            raise ToolError(
                "target path is a directory",
                details={"path": relative, "reason": "not_a_file"},
            )
        payload = args.content.encode("utf-8")
        existed = path.is_file()
        if existed and path.read_bytes() == payload:
            return {
                "path": relative,
                "bytes_written": 0,
                "changed": False,
                "created": False,
            }
        if not path.parent.exists():
            if not args.create_dirs:
                raise ToolError(
                    "parent directory does not exist",
                    details={"path": relative, "reason": "missing_parent"},
                )
            path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.{os.getpid()}.", suffix=".tmp", dir=path.parent
        )
        temp = Path(temp_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
        return {
            "path": relative,
            "bytes_written": len(payload),
            "changed": True,
            "created": not existed,
        }
