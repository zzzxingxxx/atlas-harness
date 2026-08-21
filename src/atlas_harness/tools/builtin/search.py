"""Search workspace text files, skipping secrets, binaries and vendor trees."""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from atlas_harness.kernel.errors import ToolInputError
from atlas_harness.policy.path_policy import DEFAULT_SKIP_DIRS, matches_glob
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

MAX_LINE_CHARS = 400


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pattern: str = Field(min_length=1, description="Substring, or a regex when regex is true.")
    path: str = Field(default=".", description="Workspace-relative directory to search.")
    glob: str | None = Field(default=None, description="Filename filter, e.g. '*.py'.")
    regex: bool = Field(default=False, description="Treat pattern as a Python regex.")
    case_sensitive: bool = Field(default=False, description="Match case exactly.")
    max_results: int = Field(default=100, gt=0, le=1_000, description="Match budget.")


class SearchTool(Tool):
    """Walk a vetted subtree and report matching lines."""

    manifest = ToolManifest(
        name="search",
        version="1.0.0",
        description="Search workspace text files for a substring or regex.",
        input_schema=json_schema_for(SearchInput),
        risk=RiskLevel.READ,
        scopes=(SCOPE_FS_READ,),
        idempotent=True,
        timeout_ms=20_000,
    )
    input_model = SearchInput

    def policy_request(self, args: SearchInput) -> PolicyRequest:
        return PolicyRequest(dirs=(args.path,))

    async def run(self, args: SearchInput, context: ToolContext) -> dict[str, Any]:
        matcher = self._matcher(args)
        root = context.path_for(args.path)
        return await asyncio.to_thread(self._walk, root, matcher, args, context)

    def _matcher(self, args: SearchInput) -> re.Pattern[str]:
        flags = 0 if args.case_sensitive else re.IGNORECASE
        source = args.pattern if args.regex else re.escape(args.pattern)
        try:
            return re.compile(source, flags)
        except re.error as exc:
            raise ToolInputError(
                "pattern is not a valid regex",
                details={"pattern": args.pattern, "error": str(exc)},
            ) from exc

    def _walk(
        self,
        root: Path,
        matcher: re.Pattern[str],
        args: SearchInput,
        context: ToolContext,
    ) -> dict[str, Any]:
        matches: list[dict[str, Any]] = []
        scanned = 0
        skipped = 0
        truncated = False
        for current, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(name for name in dirnames if name not in DEFAULT_SKIP_DIRS)
            for filename in sorted(filenames):
                path = Path(current) / filename
                relative = context.relative(path)
                if self._is_denied(relative, context):
                    skipped += 1
                    continue
                if args.glob is not None and not self._glob_hit(relative, filename, args.glob):
                    continue
                text = self._text(path, context)
                if text is None:
                    skipped += 1
                    continue
                scanned += 1
                for number, line in enumerate(text.splitlines(), start=1):
                    if not matcher.search(line):
                        continue
                    if len(matches) >= args.max_results:
                        truncated = True
                        break
                    matches.append(
                        {"path": relative, "line": number, "text": line[:MAX_LINE_CHARS]}
                    )
                if truncated:
                    break
            if truncated:
                break
        return {
            "pattern": args.pattern,
            "root": context.relative(root),
            "matches": matches,
            "files_scanned": scanned,
            "files_skipped": skipped,
            "truncated": truncated,
        }

    def _is_denied(self, relative: str, context: ToolContext) -> bool:
        return any(matches_glob(relative, pattern) for pattern in context.deny_globs)

    def _glob_hit(self, relative: str, filename: str, pattern: str) -> bool:
        return fnmatch.fnmatch(filename, pattern) or fnmatch.fnmatch(relative, pattern)

    def _text(self, path: Path, context: ToolContext) -> str | None:
        """Return decodable text, or None for symlinks, binaries and huge files."""

        if path.is_symlink() or not path.is_file():
            return None
        if path.stat().st_size > context.max_read_bytes:
            return None
        data = path.read_bytes()
        if looks_binary(data):
            return None
        return data.decode("utf-8")
