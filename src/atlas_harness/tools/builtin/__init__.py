"""The four builtin tools every runtime starts with."""

from atlas_harness.tools.builtin.read_file import ReadFileInput, ReadFileTool
from atlas_harness.tools.builtin.run_command import RunCommandInput, RunCommandTool
from atlas_harness.tools.builtin.search import SearchInput, SearchTool
from atlas_harness.tools.builtin.write_file import WriteFileInput, WriteFileTool
from atlas_harness.tools.manifest import Tool


def builtin_tools() -> tuple[Tool, ...]:
    return (ReadFileTool(), RunCommandTool(), SearchTool(), WriteFileTool())


__all__ = [
    "ReadFileInput",
    "ReadFileTool",
    "RunCommandInput",
    "RunCommandTool",
    "SearchInput",
    "SearchTool",
    "WriteFileInput",
    "WriteFileTool",
    "builtin_tools",
]
