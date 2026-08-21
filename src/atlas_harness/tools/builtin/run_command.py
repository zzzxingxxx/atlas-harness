"""Run one vetted program with no shell, a timeout and guaranteed child cleanup."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from atlas_harness.kernel.errors import PolicyDeniedError, ToolTimeoutError
from atlas_harness.tools.manifest import (
    SCOPE_PROCESS,
    PolicyRequest,
    RiskLevel,
    Tool,
    ToolContext,
    ToolManifest,
    json_schema_for,
)
from atlas_harness.tools.redaction import truncate_text

KILL_GRACE_SECONDS = 5.0


class RunCommandInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str | list[str] = Field(description="Program and arguments. Never a shell line.")
    cwd: str = Field(default=".", description="Workspace-relative working directory.")
    timeout_ms: int = Field(default=30_000, gt=0, le=120_000, description="Per-call timeout.")


class RunCommandTool(Tool):
    """Execute an allowlisted program directly, never through a shell."""

    manifest = ToolManifest(
        name="run_command",
        version="1.0.0",
        description="Run an allowlisted program in the workspace and capture its output.",
        input_schema=json_schema_for(RunCommandInput),
        risk=RiskLevel.WRITE,
        scopes=(SCOPE_PROCESS,),
        idempotent=False,
        timeout_ms=125_000,
    )
    input_model = RunCommandInput

    def policy_request(self, args: RunCommandInput) -> PolicyRequest:
        command = args.command if isinstance(args.command, str) else tuple(args.command)
        return PolicyRequest(dirs=(args.cwd,), commands=(command,))

    def preview(self, args: RunCommandInput, context: ToolContext) -> str | None:
        argv = context.vetted_commands[0] if context.vetted_commands else ()
        return f"$ {' '.join(argv)}" if argv else None

    async def run(self, args: RunCommandInput, context: ToolContext) -> dict[str, Any]:
        if not context.vetted_commands:
            raise PolicyDeniedError(
                "command was not vetted by the policy engine",
                details={"rule": "command_not_declared"},
            )
        argv = context.vetted_commands[0]
        cwd = context.path_for(args.cwd)
        started = context.clock.now_ms()
        process_options: dict[str, Any] = {}
        if os.name == "nt":
            process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            process_options["start_new_session"] = True
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **process_options,
        )
        try:
            async with asyncio.timeout(min(args.timeout_ms, context.timeout_ms) / 1000):
                out, err = await process.communicate()
        except TimeoutError:
            await self._terminate(process)
            raise ToolTimeoutError(
                "command exceeded its timeout",
                details={"command": list(argv), "timeout_ms": args.timeout_ms},
            ) from None
        except asyncio.CancelledError:
            await self._terminate(process)
            raise
        stdout, stdout_cut = truncate_text(out.decode("utf-8", "replace"), context.max_output_bytes)
        stderr, stderr_cut = truncate_text(err.decode("utf-8", "replace"), context.max_output_bytes)
        return {
            "command": list(argv),
            "cwd": context.relative(cwd),
            "exit_code": process.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "truncated": stdout_cut or stderr_cut,
            "duration_ms": context.clock.now_ms() - started,
        }

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        """Kill and reap the child so a cancelled call leaves nothing running."""

        if process.returncode is not None:
            return
        if os.name == "nt":
            with contextlib.suppress(OSError):
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await killer.communicate()
        else:
            killpg = getattr(os, "killpg", None)
            sigkill = getattr(signal, "SIGKILL", None)
            if callable(killpg) and sigkill is not None:
                with contextlib.suppress(ProcessLookupError):
                    killpg(process.pid, sigkill)
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(asyncio.shield(process.wait()), KILL_GRACE_SECONDS)
