"""Dangerous commands and sensitive files are refused before anything runs.

The plan lists 敏感文件访问 and 危险命令访问 as scenarios that must be tested on
every commit. Both are decided in :mod:`atlas_harness.policy`, ahead of any tool
implementation, which is what makes them testable in isolation here: no subprocess
is spawned and no file is opened, because a refusal that only happened after the
process started would not be a refusal at all.

The lists are read from the policy modules rather than copied, so a denied binary
or a deny glob that is removed from the default set fails a test here instead of
quietly widening what the harness will run.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from atlas_harness.kernel.errors import PolicyDeniedError
from atlas_harness.policy.command_policy import (
    DANGEROUS_FLAGS,
    DEFAULT_DENIED_COMMANDS,
    MAX_ARGUMENTS,
    MAX_COMMAND_LENGTH,
    SHELL_METACHARACTERS,
    CommandPolicy,
)
from atlas_harness.policy.path_policy import DEFAULT_DENY_GLOBS, PathPolicy

SECRET_BODY = "api_key=sup3rsecretvalue\nAKIAIOSFODNN7EXAMPLE\n"


@pytest.fixture
def commands() -> CommandPolicy:
    return CommandPolicy()


@pytest.mark.parametrize("program", sorted(DEFAULT_DENIED_COMMANDS))
def test_every_denied_binary_is_refused(commands: CommandPolicy, program: str) -> None:
    with pytest.raises(PolicyDeniedError) as raised:
        commands.parse(f"{program} --version")

    assert raised.value.as_dict()["details"]["rule"] == "command_denylisted"


@pytest.mark.parametrize("interpreter", ["bash", "sh", "zsh", "powershell", "pwsh", "cmd"])
def test_a_shell_interpreter_cannot_be_used_to_smuggle_a_command(
    commands: CommandPolicy, interpreter: str
) -> None:
    """A shell would evaluate its argument, putting the whole grammar back in reach."""

    with pytest.raises(PolicyDeniedError):
        commands.parse([interpreter, "-c", "whoami"])


@pytest.mark.parametrize("metacharacter", sorted(SHELL_METACHARACTERS))
def test_every_shell_metacharacter_is_refused(commands: CommandPolicy, metacharacter: str) -> None:
    with pytest.raises(PolicyDeniedError) as raised:
        commands.parse(f"ls {metacharacter} whoami")

    assert raised.value.as_dict()["details"]["rule"] == "command_injection"


@pytest.mark.parametrize("flag", DANGEROUS_FLAGS)
def test_every_dangerous_flag_is_refused(commands: CommandPolicy, flag: str) -> None:
    with pytest.raises(PolicyDeniedError) as raised:
        commands.parse(["git", flag])

    assert raised.value.as_dict()["details"]["rule"] == "command_dangerous_flag"


def test_recursion_combined_with_force_is_refused_even_when_neither_flag_alone_is(
    commands: CommandPolicy,
) -> None:
    with pytest.raises(PolicyDeniedError) as raised:
        commands.parse(["git", "clean", "-r", "-f"])

    details = raised.value.as_dict()["details"]
    assert details["rule"] == "command_dangerous_flag"
    assert details.get("flag") == "recursive+force"


@pytest.mark.parametrize("command", ["", "   ", "\t\t"])
def test_an_empty_command_is_refused_rather_than_treated_as_a_no_op(
    commands: CommandPolicy, command: str
) -> None:
    with pytest.raises(PolicyDeniedError) as raised:
        commands.parse(command)

    assert raised.value.as_dict()["details"]["rule"] == "command_empty"


def test_too_many_arguments_are_refused(commands: CommandPolicy) -> None:
    with pytest.raises(PolicyDeniedError) as raised:
        commands.parse(["echo", *("x" for _ in range(MAX_ARGUMENTS))])

    details = raised.value.as_dict()["details"]
    # The argument-count refusal reuses the command_too_long rule string, so the
    # limit is what distinguishes it from an overlong command line.
    assert details["rule"] == "command_too_long"
    assert details["limit"] == MAX_ARGUMENTS


def test_an_overlong_command_is_refused_before_it_is_parsed(commands: CommandPolicy) -> None:
    with pytest.raises(PolicyDeniedError) as raised:
        commands.parse("echo " + "x" * MAX_COMMAND_LENGTH)

    assert raised.value.as_dict()["details"]["rule"] == "command_too_long"


def test_an_unlisted_program_is_refused_by_default(commands: CommandPolicy) -> None:
    """The allowlist is the boundary; an unknown binary is not a neutral one."""

    with pytest.raises(PolicyDeniedError) as raised:
        commands.parse("nmap -sS 10.0.0.1")

    assert raised.value.as_dict()["details"]["rule"] == "command_not_allowlisted"


def test_a_program_given_by_path_is_refused_so_the_allowlist_cannot_be_bypassed(
    commands: CommandPolicy,
) -> None:
    with pytest.raises(PolicyDeniedError) as raised:
        commands.parse("/bin/ls -l")

    assert raised.value.as_dict()["details"]["rule"] == "command_path_program"


@pytest.mark.parametrize("command", [["python", "-c", "import os"], ["node", "-e", "1"]])
def test_inline_code_flags_are_refused_on_otherwise_allowed_interpreters(
    commands: CommandPolicy, command: list[str]
) -> None:
    """``python`` is allowed so tests can run; ``python -c`` is a shell in disguise."""

    with pytest.raises(PolicyDeniedError) as raised:
        commands.parse(command)

    assert raised.value.as_dict()["details"]["rule"] == "command_inline_code"


def test_an_allowed_command_still_parses(commands: CommandPolicy) -> None:
    """Without this the suite would pass just as well if the policy denied everything."""

    assert commands.parse("pytest tests/unit -q") == ("pytest", "tests/unit", "-q")


@pytest.mark.parametrize("pattern", DEFAULT_DENY_GLOBS)
def test_every_default_deny_glob_refuses_a_matching_file(
    workspace: Path, paths: PathPolicy, pattern: str
) -> None:
    relative = pattern.replace("**", "nested").replace("*", "sample")
    target = workspace / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(SECRET_BODY, encoding="utf-8")

    with pytest.raises(PolicyDeniedError) as raised:
        paths.resolve_read(relative)

    error = raised.value.as_dict()
    assert error["details"]["rule"] == "path_denylisted"
    assert "sup3rsecretvalue" not in repr(error)


@pytest.mark.parametrize("pattern", DEFAULT_DENY_GLOBS)
def test_a_denied_file_cannot_be_written_either(paths: PathPolicy, pattern: str) -> None:
    """Denial is not read-only: writing a forged ``.npmrc`` is its own attack."""

    relative = pattern.replace("**", "nested").replace("*", "sample")

    with pytest.raises(PolicyDeniedError):
        paths.resolve_write(relative)


@pytest.mark.parametrize(
    "candidate",
    ["../../etc/passwd", "..", "../outside.txt", "subdir/../../outside.txt"],
)
def test_traversal_out_of_the_workspace_is_refused(paths: PathPolicy, candidate: str) -> None:
    with pytest.raises(PolicyDeniedError) as raised:
        paths.resolve_read(candidate)

    assert raised.value.as_dict()["details"]["rule"] == "path_outside_workspace"


def test_an_absolute_path_outside_the_workspace_is_refused(
    tmp_path: Path, paths: PathPolicy
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text(SECRET_BODY, encoding="utf-8")

    with pytest.raises(PolicyDeniedError) as raised:
        paths.resolve_read(str(outside))

    error = raised.value.as_dict()
    assert error["details"]["rule"] == "path_outside_workspace"
    assert "sup3rsecretvalue" not in repr(error)


def test_a_symlink_pointing_out_of_the_workspace_is_refused(
    tmp_path: Path, workspace: Path, paths: PathPolicy
) -> None:
    """The resolved target is what matters; a link is only a name for elsewhere."""

    outside = tmp_path / "outside.txt"
    outside.write_text(SECRET_BODY, encoding="utf-8")
    link = workspace / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:  # pragma: no cover - Windows without developer mode
        pytest.skip("creating a symlink requires privileges on this platform")

    with pytest.raises(PolicyDeniedError) as raised:
        paths.resolve_read("link.txt")

    assert raised.value.as_dict()["details"]["rule"] in {"path_symlink", "path_outside_workspace"}


def test_a_symlinked_directory_cannot_hide_a_denied_file(
    tmp_path: Path, workspace: Path, paths: PathPolicy
) -> None:
    secrets = tmp_path / "elsewhere"
    secrets.mkdir()
    (secrets / "notes.txt").write_text(SECRET_BODY, encoding="utf-8")
    link = workspace / "linked"
    try:
        link.symlink_to(secrets, target_is_directory=True)
    except OSError:  # pragma: no cover - Windows without developer mode
        pytest.skip("creating a symlink requires privileges on this platform")

    with pytest.raises(PolicyDeniedError):
        paths.resolve_read("linked/notes.txt")


def test_a_file_over_the_read_limit_is_refused_before_it_is_loaded(
    workspace: Path, paths: PathPolicy
) -> None:
    """The limit exists so one huge file cannot exhaust memory or the context."""

    big = workspace / "big.txt"
    big.write_text("x" * (paths.max_read_bytes + 1), encoding="utf-8")

    with pytest.raises(PolicyDeniedError) as raised:
        paths.assert_readable_file(paths.resolve_read("big.txt"))

    assert raised.value.as_dict()["details"]["rule"] == "file_too_large"


def test_a_readable_file_inside_the_workspace_still_resolves(
    workspace: Path, paths: PathPolicy
) -> None:
    (workspace / "ok.txt").write_text("fine", encoding="utf-8")

    resolved = paths.resolve_read("ok.txt")

    assert resolved == (workspace / "ok.txt").resolve()
    assert os.fspath(resolved).startswith(os.fspath(workspace.resolve()))
