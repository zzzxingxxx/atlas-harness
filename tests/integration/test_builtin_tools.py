import asyncio
from pathlib import Path

import pytest
from tests.conftest import TOOL_SESSION_ID, ExecutorFactory

from atlas_harness.events import EventStore, EventType
from atlas_harness.tools.executor import ToolCall, ToolExecutor


@pytest.mark.asyncio
async def test_read_search_and_write_follow_the_policy_and_approval_boundary(
    workspace: Path,
    executor_factory: ExecutorFactory,
    tool_store: EventStore,
) -> None:
    (workspace / "notes.txt").write_text("alpha\nneedle\nomega\n", encoding="utf-8")
    (workspace / ".env").write_text("needle=secret\n", encoding="utf-8")
    (workspace / "binary.bin").write_bytes(b"needle\x00binary")

    executor = executor_factory()
    read = await executor.execute(
        ToolCall(
            tool_name="read_file",
            arguments={"path": "notes.txt", "start_line": 2, "max_lines": 1},
        ),
        session_id=TOOL_SESSION_ID,
    )
    search = await executor.execute(
        ToolCall(tool_name="search", arguments={"pattern": "needle"}),
        session_id=TOOL_SESSION_ID,
    )

    assert read.success
    assert read.output["content"] == "needle"
    assert search.success
    assert search.output["matches"] == [{"path": "notes.txt", "line": 2, "text": "needle"}]
    assert search.output["files_skipped"] == 2

    denied_executor = executor_factory(approve=False)
    denied = await denied_executor.execute(
        ToolCall(tool_name="write_file", arguments={"path": "created.txt", "content": "new"}),
        session_id=TOOL_SESSION_ID,
    )
    assert not denied.success
    assert denied.error_code == "approval_denied"
    assert not (workspace / "created.txt").exists()

    written = await executor.execute(
        ToolCall(tool_name="write_file", arguments={"path": "created.txt", "content": "new"}),
        session_id=TOOL_SESSION_ID,
    )
    unchanged = await executor.execute(
        ToolCall(tool_name="write_file", arguments={"path": "created.txt", "content": "new"}),
        session_id=TOOL_SESSION_ID,
    )

    assert written.success and written.output["changed"] is True
    assert unchanged.success and unchanged.output["changed"] is False
    assert (workspace / "created.txt").read_text(encoding="utf-8") == "new"
    events = tool_store.read_events(TOOL_SESSION_ID)
    starts = [event for event in events if event.event_type is EventType.TOOL_STARTED]
    results = [event for event in events if event.event_type is EventType.TOOL_RESULT]
    assert all(event.payload.model_dump().get("idempotency_key") for event in starts + results)


@pytest.mark.asyncio
async def test_run_command_blocks_inline_code_times_out_and_redacts_output(
    workspace: Path,
    executor_factory: ExecutorFactory,
) -> None:
    (workspace / "emit.py").write_text(
        "print('api_key=sk-abcdefghijklmnop123456')\n",
        encoding="utf-8",
    )
    (workspace / "sleep.py").write_text(
        "import time\ntime.sleep(10)\n",
        encoding="utf-8",
    )
    executor = executor_factory()

    blocked = await executor.execute(
        ToolCall(tool_name="run_command", arguments={"command": ["python", "-c", "print(1)"]}),
        session_id=TOOL_SESSION_ID,
    )
    emitted = await executor.execute(
        ToolCall(tool_name="run_command", arguments={"command": ["python", "emit.py"]}),
        session_id=TOOL_SESSION_ID,
    )
    timed_out = await executor.execute(
        ToolCall(
            tool_name="run_command",
            arguments={"command": ["python", "sleep.py"], "timeout_ms": 50},
        ),
        session_id=TOOL_SESSION_ID,
    )

    assert not blocked.success
    assert blocked.error_details["rule"] == "command_inline_code"
    assert emitted.success
    assert emitted.output["stdout"].strip() == "api_key=[redacted]"
    assert not timed_out.success
    assert timed_out.error_code == "tool_timeout"
    assert timed_out.approved is True
    assert timed_out.tool_version == "1.0.0"


@pytest.mark.asyncio
async def test_cancelling_run_command_stops_the_child_process_tree(
    workspace: Path,
    executor_factory: ExecutorFactory,
    tool_store: EventStore,
) -> None:
    (workspace / "heartbeat.py").write_text(
        "import pathlib, sys, time\n"
        "path = pathlib.Path(sys.argv[1])\n"
        "while True:\n"
        "    path.write_text(str(time.time_ns()), encoding='utf-8')\n"
        "    time.sleep(0.02)\n",
        encoding="utf-8",
    )
    (workspace / "launcher.py").write_text(
        "import subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, 'heartbeat.py', sys.argv[1]])\n"
        "raise SystemExit(child.wait())\n",
        encoding="utf-8",
    )
    heartbeat = workspace / "heartbeat.txt"
    executor: ToolExecutor = executor_factory()
    task = asyncio.create_task(
        executor.execute(
            ToolCall(
                tool_name="run_command",
                arguments={"command": ["python", "launcher.py", "heartbeat.txt"]},
            ),
            session_id=TOOL_SESSION_ID,
        )
    )

    for _ in range(100):
        if heartbeat.exists():
            break
        await asyncio.sleep(0.02)
    assert heartbeat.exists()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.sleep(0.15)
    first = heartbeat.read_text(encoding="utf-8")
    await asyncio.sleep(0.15)
    second = heartbeat.read_text(encoding="utf-8")

    assert first == second
    events = tool_store.read_events(TOOL_SESSION_ID)
    result = [event for event in events if event.event_type is EventType.TOOL_RESULT][-1]
    payload = result.payload.model_dump()
    assert payload["error_code"] == "cancelled"
    assert payload["tool_version"] == "1.0.0"
    assert payload["idempotency_key"]
