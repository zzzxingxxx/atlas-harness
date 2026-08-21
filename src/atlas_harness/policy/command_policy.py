"""Command allowlist, injection guards and dangerous-argument checks."""

from __future__ import annotations

import shlex
from pathlib import Path

from atlas_harness.kernel.errors import PolicyDeniedError

DEFAULT_ALLOWED_COMMANDS: tuple[str, ...] = (
    "cat",
    "echo",
    "git",
    "go",
    "head",
    "ls",
    "mypy",
    "node",
    "npm",
    "pnpm",
    "pytest",
    "python",
    "python3",
    "rg",
    "ruff",
    "tail",
    "uv",
    "wc",
    "yarn",
)
"""Programs a runtime may start. Anything absent is refused, not warned about."""

DEFAULT_DENIED_COMMANDS: tuple[str, ...] = (
    "bash",
    "chmod",
    "chown",
    "cmd",
    "curl",
    "dd",
    "del",
    "diskpart",
    "eval",
    "format",
    "kill",
    "mkfs",
    "nc",
    "netsh",
    "powershell",
    "pwsh",
    "reboot",
    "reg",
    "rm",
    "rmdir",
    "scp",
    "sh",
    "shutdown",
    "ssh",
    "su",
    "sudo",
    "taskkill",
    "wget",
    "zsh",
)
"""Explicitly refused even if an operator widens the allowlist by mistake."""

SHELL_METACHARACTERS: tuple[str, ...] = (
    "&&",
    "||",
    ";",
    "|",
    ">",
    "<",
    "`",
    "$(",
    "${",
    "&",
    "\n",
    "\r",
    "\x00",
)
"""Refused in string commands: nothing here ever reaches a shell, so their
presence means the caller expected shell semantics that do not exist."""

DANGEROUS_FLAGS: tuple[str, ...] = (
    "--no-preserve-root",
    "--force",
    "-rf",
    "-fr",
    "--hard",
    "/q",
    "/s",
)

INLINE_CODE_FLAGS: dict[str, frozenset[str]] = {
    "node": frozenset({"-e", "--eval", "-p", "--print"}),
    "python": frozenset({"-c"}),
    "python3": frozenset({"-c"}),
}

GIT_NETWORK_COMMANDS = frozenset(
    {"clone", "fetch", "pull", "push", "ls-remote", "archive", "send-email"}
)

RECURSIVE_FLAGS = frozenset({"-r", "-R", "--recursive"})
FORCE_FLAGS = frozenset({"-f", "--force", "/f"})

MAX_COMMAND_LENGTH = 4_096
MAX_ARGUMENTS = 64


class CommandPolicy:
    """Turn caller input into an argv that is safe to hand to ``exec``."""

    def __init__(
        self,
        *,
        allowed: tuple[str, ...] = DEFAULT_ALLOWED_COMMANDS,
        denied: tuple[str, ...] = DEFAULT_DENIED_COMMANDS,
        dangerous_flags: tuple[str, ...] = DANGEROUS_FLAGS,
        max_arguments: int = MAX_ARGUMENTS,
    ) -> None:
        self.allowed = frozenset(entry.lower() for entry in allowed)
        self.denied = frozenset(entry.lower() for entry in denied)
        self.dangerous_flags = tuple(flag.lower() for flag in dangerous_flags)
        self.max_arguments = max_arguments

    def parse(self, command: str | list[str] | tuple[str, ...]) -> tuple[str, ...]:
        """Split and vet a command without ever invoking a shell."""

        argv = self._split(command)
        if not argv:
            raise PolicyDeniedError(
                "empty command",
                details={"rule": "command_empty"},
            )
        if len(argv) > self.max_arguments:
            raise PolicyDeniedError(
                "command has too many arguments",
                details={"rule": "command_too_long", "limit": self.max_arguments},
            )
        for token in argv:
            if "\x00" in token:
                raise PolicyDeniedError(
                    "command contains a NUL byte",
                    details={"rule": "command_injection"},
                )
        self._check_program(argv[0])
        self._check_arguments(argv)
        self._check_flags(argv)
        return tuple(argv)

    def _split(self, command: str | list[str] | tuple[str, ...]) -> list[str]:
        if isinstance(command, str):
            if len(command) > MAX_COMMAND_LENGTH:
                raise PolicyDeniedError(
                    "command string is too long",
                    details={"rule": "command_too_long", "limit": MAX_COMMAND_LENGTH},
                )
            found = [token for token in SHELL_METACHARACTERS if token in command]
            if found:
                raise PolicyDeniedError(
                    "command contains shell metacharacters",
                    details={"rule": "command_injection", "tokens": found},
                )
            try:
                return shlex.split(command, posix=True)
            except ValueError as exc:
                raise PolicyDeniedError(
                    "command cannot be parsed",
                    details={"rule": "command_unparsable", "error": str(exc)},
                ) from exc
        return [str(token) for token in command]

    def _check_program(self, program: str) -> None:
        if not program:
            raise PolicyDeniedError("empty program", details={"rule": "command_empty"})
        path = Path(program)
        if len(path.parts) > 1 or path.is_absolute():
            raise PolicyDeniedError(
                "program must be a bare name resolved from PATH",
                details={"rule": "command_path_program", "program": program},
            )
        name = path.name.lower()
        stem = path.stem.lower()
        if name in self.denied or stem in self.denied:
            raise PolicyDeniedError(
                "program is on the command denylist",
                details={"rule": "command_denylisted", "program": program},
            )
        if stem not in self.allowed and name not in self.allowed:
            raise PolicyDeniedError(
                "program is not on the command allowlist",
                details={
                    "rule": "command_not_allowlisted",
                    "program": program,
                    "allowed": sorted(self.allowed),
                },
            )

    def _check_flags(self, argv: list[str]) -> None:
        lowered = [token.lower() for token in argv[1:]]
        for token in lowered:
            if token in self.dangerous_flags:
                raise PolicyDeniedError(
                    "command uses a dangerous argument",
                    details={"rule": "command_dangerous_flag", "flag": token},
                )
        tokens = set(argv[1:])
        if tokens & RECURSIVE_FLAGS and tokens & FORCE_FLAGS:
            raise PolicyDeniedError(
                "command combines recursive and force flags",
                details={"rule": "command_dangerous_flag", "flag": "recursive+force"},
            )

    def _check_arguments(self, argv: list[str]) -> None:
        """Reject common escapes from the program and workspace boundaries."""

        program = Path(argv[0]).stem.lower()
        lowered = [token.lower() for token in argv[1:]]
        blocked = INLINE_CODE_FLAGS.get(program, frozenset())
        for token in lowered:
            flag = token.split("=", 1)[0]
            if flag in blocked or any(
                option.startswith("-") and not option.startswith("--") and flag.startswith(option)
                for option in blocked
            ):
                raise PolicyDeniedError(
                    "interpreter inline code is not allowed",
                    details={"rule": "command_inline_code", "program": program, "flag": flag},
                )
        for token in argv[1:]:
            path = Path(token)
            if path.is_absolute() or ".." in path.parts:
                raise PolicyDeniedError(
                    "command argument may escape the workspace",
                    details={"rule": "command_path_escape", "argument": token},
                )
        if program == "git":
            subcommand = next((token for token in lowered if not token.startswith("-")), "")
            if subcommand in GIT_NETWORK_COMMANDS:
                raise PolicyDeniedError(
                    "git network operations are disabled",
                    details={"rule": "command_network", "subcommand": subcommand},
                )
