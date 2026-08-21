"""Tool registration, discovery and version pinning."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

from atlas_harness.kernel.errors import (
    ToolError,
    ToolNotFoundError,
    ToolVersionError,
)
from atlas_harness.tools.manifest import Tool, ToolManifest


class ToolRegistry:
    """Hold the tools a runtime is allowed to call, keyed by name."""

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools[name] for name in sorted(self._tools))

    def register(self, tool: Tool, *, replace: bool = False) -> ToolManifest:
        manifest = tool.manifest
        existing = self._tools.get(manifest.name)
        if existing is not None and not replace:
            raise ToolError(
                "tool is already registered",
                details={
                    "tool": manifest.name,
                    "registered_version": existing.manifest.version,
                    "offered_version": manifest.version,
                },
            )
        self._tools[manifest.name] = tool
        return manifest

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def get(self, name: str, *, version: str | None = None) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolNotFoundError(
                "unknown tool",
                details={"tool": name, "available": self.names()},
            )
        if version is not None and version != tool.manifest.version:
            raise ToolVersionError(
                "tool version does not match the registered version",
                details={
                    "tool": name,
                    "requested_version": version,
                    "registered_version": tool.manifest.version,
                },
            )
        return tool

    def manifest(self, name: str) -> ToolManifest:
        return self.get(name).manifest

    def manifests(self) -> list[ToolManifest]:
        return [self._tools[name].manifest for name in sorted(self._tools)]

    def describe(self) -> list[dict[str, Any]]:
        """Discovery payload for the CLI, a model or an MCP bridge."""

        return [manifest.describe() for manifest in self.manifests()]

    def required_scopes(self) -> set[str]:
        return {scope for manifest in self.manifests() for scope in manifest.scopes}


def default_registry() -> ToolRegistry:
    """The four builtin tools every runtime starts with."""

    from atlas_harness.tools.builtin import builtin_tools

    return ToolRegistry(builtin_tools())
