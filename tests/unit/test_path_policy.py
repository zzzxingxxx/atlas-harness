from pathlib import Path

import pytest

from atlas_harness.kernel.errors import PolicyDeniedError
from atlas_harness.policy import PathPolicy
from atlas_harness.policy.path_policy import DEFAULT_DENY_GLOBS, matches_glob


@pytest.mark.parametrize(
    ("relative", "pattern"),
    [
        (".env", ".env"),
        (".env.local", ".env.*"),
        ("config/.env", ".env"),
        ("certs/server.pem", "*.pem"),
        (".ssh/id_rsa", ".ssh/**"),
        ("nested/.aws/credentials", ".aws/**"),
        ("service-account-prod.json", "service-account*.json"),
        ("SECRETS.JSON", "secrets.json"),
    ],
)
def test_matches_glob_accepts_the_shapes_the_denylist_needs(relative: str, pattern: str) -> None:
    assert matches_glob(relative, pattern) is True


@pytest.mark.parametrize(
    ("relative", "pattern"),
    [
        ("env", ".env"),
        ("readme.md", "*.pem"),
        ("ssh/id_rsa", ".aws/**"),
        ("docs/.ssh", ".ssh/**"),
    ],
)
def test_matches_glob_does_not_over_match(relative: str, pattern: str) -> None:
    assert matches_glob(relative, pattern) is False


def test_relative_paths_resolve_under_the_workspace(paths: PathPolicy, workspace: Path) -> None:
    assert paths.resolve_read("src/a.txt") == workspace / "src" / "a.txt"
    assert paths.resolve_write("./out.txt") == workspace / "out.txt"
    assert paths.resolve_read(".") == workspace


@pytest.mark.parametrize("raw", ["../outside.txt", "src/../../outside.txt", "/etc/passwd"])
def test_traversal_and_absolute_escapes_are_denied(paths: PathPolicy, raw: str) -> None:
    with pytest.raises(PolicyDeniedError) as caught:
        paths.resolve_read(raw)

    assert caught.value.details["rule"] == "path_outside_workspace"


@pytest.mark.parametrize("raw", ["", "   ", "a\x00b"])
def test_empty_and_nul_paths_are_invalid(paths: PathPolicy, raw: str) -> None:
    with pytest.raises(PolicyDeniedError) as caught:
        paths.resolve_read(raw)

    assert caught.value.details["rule"] == "path_invalid"


@pytest.mark.parametrize("raw", [".env", ".env.production", "id_ed25519", ".ssh/known_hosts"])
def test_sensitive_files_are_denied_for_read_and_write(paths: PathPolicy, raw: str) -> None:
    for resolve in (paths.resolve_read, paths.resolve_write):
        with pytest.raises(PolicyDeniedError) as caught:
            resolve(raw)
        assert caught.value.details["rule"] == "path_denylisted"


def test_allow_prefixes_narrow_reads_and_writes(workspace: Path) -> None:
    policy = PathPolicy(workspace, allow_read=("src",), allow_write=("out",))

    assert policy.resolve_read("src/a.txt") == workspace / "src" / "a.txt"
    assert policy.resolve_write("out/b.txt") == workspace / "out" / "b.txt"

    with pytest.raises(PolicyDeniedError) as read_denied:
        policy.resolve_read("docs/a.txt")
    assert read_denied.value.details["rule"] == "path_read_not_allowed"

    with pytest.raises(PolicyDeniedError) as write_denied:
        policy.resolve_write("src/a.txt")
    assert write_denied.value.details["rule"] == "path_write_not_allowed"


def test_assert_readable_file_reports_the_precise_rule(paths: PathPolicy, workspace: Path) -> None:
    (workspace / "dir").mkdir()
    small = workspace / "small.txt"
    small.write_text("hello", encoding="utf-8")
    large = workspace / "large.txt"
    large.write_bytes(b"x" * 64_001)

    assert paths.assert_readable_file(small) == 5

    with pytest.raises(PolicyDeniedError) as missing:
        paths.assert_readable_file(workspace / "gone.txt")
    assert missing.value.details["rule"] == "path_missing"

    with pytest.raises(PolicyDeniedError) as not_a_file:
        paths.assert_readable_file(workspace / "dir")
    assert not_a_file.value.details["rule"] == "path_not_a_file"

    with pytest.raises(PolicyDeniedError) as too_large:
        paths.assert_readable_file(large)
    assert too_large.value.details["limit"] == 64_000
    assert too_large.value.details["rule"] == "file_too_large"


def test_assert_directory_rejects_files_and_missing_paths(
    paths: PathPolicy, workspace: Path
) -> None:
    (workspace / "src").mkdir()
    (workspace / "a.txt").write_text("x", encoding="utf-8")

    paths.assert_directory(workspace / "src")

    with pytest.raises(PolicyDeniedError) as missing:
        paths.assert_directory(workspace / "nope")
    assert missing.value.details["rule"] == "path_missing"

    with pytest.raises(PolicyDeniedError) as not_a_dir:
        paths.assert_directory(workspace / "a.txt")
    assert not_a_dir.value.details["rule"] == "path_not_a_directory"


def test_is_denied_returns_the_matching_pattern(paths: PathPolicy) -> None:
    assert paths.is_denied("deploy/.env") == ".env"
    assert paths.is_denied("src/main.py") is None
    assert ".env" in DEFAULT_DENY_GLOBS
