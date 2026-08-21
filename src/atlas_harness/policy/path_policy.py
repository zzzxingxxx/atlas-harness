"""Workspace boundary, sensitive-file denylist and read limits."""

from __future__ import annotations

import fnmatch
from pathlib import Path

from atlas_harness.kernel.errors import PolicyDeniedError

DEFAULT_DENY_GLOBS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.pfx",
    "*.p12",
    "*.keystore",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    ".ssh/**",
    ".gnupg/**",
    ".aws/**",
    ".kube/**",
    ".docker/config.json",
    ".git/config",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "secrets.json",
    "service-account*.json",
)
"""Files that must never be read or written without an explicit rule change."""

DEFAULT_SKIP_DIRS: tuple[str, ...] = (
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".atlas",
)
"""Directories a workspace search walks past. Not a security boundary."""


def matches_glob(relative: str, pattern: str) -> bool:
    """Match a workspace-relative POSIX path against one denylist pattern."""

    rel = relative.replace("\\", "/").lower()
    pat = pattern.replace("\\", "/").lower()
    if fnmatch.fnmatchcase(rel, pat):
        return True
    parts = rel.split("/")
    if pat.endswith("/**"):
        head = pat[:-3]
        if "/" in head:
            return rel.startswith(f"{head}/")
        return head in parts[:-1]
    if "/" not in pat:
        return any(fnmatch.fnmatchcase(part, pat) for part in parts)
    return False


class PathPolicy:
    """Resolve caller supplied paths into vetted absolute paths.

    Every method either returns a path inside the workspace or raises
    :class:`PolicyDeniedError` with the rule that refused.
    """

    def __init__(
        self,
        workspace_root: Path,
        *,
        deny_globs: tuple[str, ...] = DEFAULT_DENY_GLOBS,
        allow_read: tuple[str, ...] = (),
        allow_write: tuple[str, ...] = (),
        max_read_bytes: int = 1_048_576,
        allow_symlinks: bool = False,
    ) -> None:
        self.workspace_root = workspace_root.expanduser().resolve()
        self.deny_globs = deny_globs
        self.allow_read = tuple(entry.replace("\\", "/").strip("/") for entry in allow_read)
        self.allow_write = tuple(entry.replace("\\", "/").strip("/") for entry in allow_write)
        self.max_read_bytes = max_read_bytes
        self.allow_symlinks = allow_symlinks

    def relative(self, path: Path) -> str:
        return path.relative_to(self.workspace_root).as_posix()

    def is_denied(self, relative: str) -> str | None:
        for pattern in self.deny_globs:
            if matches_glob(relative, pattern):
                return pattern
        return None

    def resolve_read(self, raw: str) -> Path:
        return self._resolve(raw, write=False)

    def resolve_write(self, raw: str) -> Path:
        return self._resolve(raw, write=True)

    def assert_readable_file(self, path: Path) -> int:
        """Confirm a vetted path is a regular file within the size limit."""

        relative = self.relative(path)
        if not path.exists():
            raise PolicyDeniedError(
                "file does not exist",
                details={"path": relative, "rule": "path_missing"},
            )
        if not path.is_file():
            raise PolicyDeniedError(
                "path is not a regular file",
                details={"path": relative, "rule": "path_not_a_file"},
            )
        size = path.stat().st_size
        if size > self.max_read_bytes:
            raise PolicyDeniedError(
                "file is larger than the read limit",
                details={
                    "path": relative,
                    "rule": "file_too_large",
                    "size": size,
                    "limit": self.max_read_bytes,
                },
            )
        return size

    def assert_directory(self, path: Path) -> None:
        """Confirm a vetted path is an existing directory."""

        relative = self.relative(path)
        if not path.exists():
            raise PolicyDeniedError(
                "directory does not exist",
                details={"path": relative, "rule": "path_missing"},
            )
        if not path.is_dir():
            raise PolicyDeniedError(
                "path is not a directory",
                details={"path": relative, "rule": "path_not_a_directory"},
            )

    def _resolve(self, raw: str, *, write: bool) -> Path:
        if not raw or not raw.strip():
            raise PolicyDeniedError(
                "empty path",
                details={"path": raw, "rule": "path_invalid"},
            )
        if "\x00" in raw:
            raise PolicyDeniedError(
                "path contains a NUL byte",
                details={"rule": "path_invalid"},
            )
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace_root / candidate
        self._reject_symlinks(candidate, raw)
        resolved = candidate.resolve()
        if resolved != self.workspace_root and not resolved.is_relative_to(self.workspace_root):
            raise PolicyDeniedError(
                "path escapes the workspace root",
                details={
                    "path": raw,
                    "rule": "path_outside_workspace",
                    "workspace_root": str(self.workspace_root),
                },
            )
        relative = self.relative(resolved)
        denied = self.is_denied(relative)
        if denied is not None:
            raise PolicyDeniedError(
                "path is on the sensitive-file denylist",
                details={"path": relative, "rule": "path_denylisted", "pattern": denied},
            )
        allowed = self.allow_write if write else self.allow_read
        if allowed and not any(
            relative == entry or relative.startswith(f"{entry}/") for entry in allowed
        ):
            raise PolicyDeniedError(
                "path is outside the allowed prefixes",
                details={
                    "path": relative,
                    "rule": "path_write_not_allowed" if write else "path_read_not_allowed",
                    "allowed": list(allowed),
                },
            )
        return resolved

    def _reject_symlinks(self, candidate: Path, raw: str) -> None:
        """Refuse symlinked components so a link cannot smuggle a path out."""

        if self.allow_symlinks:
            return
        current = candidate
        while True:
            if current.is_symlink():
                raise PolicyDeniedError(
                    "path traverses a symbolic link",
                    details={"path": raw, "rule": "path_symlink", "link": str(current)},
                )
            if current == current.parent or current == self.workspace_root:
                return
            current = current.parent
