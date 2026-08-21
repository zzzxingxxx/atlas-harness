"""Tool contract, registry and result hygiene.

``executor`` and ``builtin`` are deliberately absent: both depend on
``atlas_harness.policy``, which depends back on :mod:`atlas_harness.tools.manifest`.
Import them by module path (``from atlas_harness.tools.executor import ToolExecutor``).
"""

from atlas_harness.tools.manifest import (
    APPROVAL_BY_DEFAULT,
    DEFAULT_SCOPES,
    SCOPE_FS_READ,
    SCOPE_FS_WRITE,
    SCOPE_NETWORK,
    SCOPE_PROCESS,
    PolicyRequest,
    RiskLevel,
    Tool,
    ToolContext,
    ToolManifest,
    json_schema_for,
)
from atlas_harness.tools.redaction import (
    REDACTED,
    looks_binary,
    redact,
    redact_value,
    truncate_text,
)
from atlas_harness.tools.registry import ToolRegistry, default_registry

__all__ = [
    "APPROVAL_BY_DEFAULT",
    "DEFAULT_SCOPES",
    "REDACTED",
    "SCOPE_FS_READ",
    "SCOPE_FS_WRITE",
    "SCOPE_NETWORK",
    "SCOPE_PROCESS",
    "PolicyRequest",
    "RiskLevel",
    "Tool",
    "ToolContext",
    "ToolManifest",
    "ToolRegistry",
    "default_registry",
    "json_schema_for",
    "looks_binary",
    "redact",
    "redact_value",
    "truncate_text",
]
