"""Reading skill definitions off disk.

A skill file is a small metadata document plus an instruction body. Both YAML and
JSON are accepted because the plan names both, and YAML is parsed with
``yaml.safe_load`` — a skill file is content, and the loader must not be a path from
a file in the workspace to arbitrary object construction.

Two rules shape the rest of this module:

*Loading is not activating.* Every file arrives as ``draft`` unless it explicitly
declares a status, and a file cannot declare itself ``active`` — the plan requires
an evaluation before a version becomes effective, and a status field the author
controls would route straight around that. Promotion happens through
:class:`~atlas_harness.skills.repository.SkillRepository`, which writes the event.

*A bad file is named, not skipped.* :func:`load_directory` collects errors instead
of raising on the first one, so an operator sees every malformed skill at once
rather than fixing them one restart at a time.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from atlas_harness.kernel.errors import ConfigurationError
from atlas_harness.skills.models import (
    MAX_BODY_CHARS,
    SkillRecord,
    SkillStatus,
    checksum_for,
    parse_status,
)

SKILL_SUFFIXES = (".yaml", ".yml", ".json")
"""Extensions the loader will read. Anything else in the directory is ignored
rather than guessed at."""

LOADABLE_STATUSES = frozenset({SkillStatus.DRAFT, SkillStatus.CANDIDATE})
"""Statuses a file on disk may declare. ``active`` is deliberately absent: a skill
becomes effective by passing evaluation, never by asserting that it has."""

MAX_SKILL_FILE_BYTES = 256 * 1024
"""Refuse anything larger. A skill body is instructions; a quarter megabyte of it
is a document that would swallow the context budget."""


@dataclass(frozen=True)
class SkillLoadError:
    """One file that could not be loaded, and why."""

    path: Path
    message: str


@dataclass(frozen=True)
class SkillLoadResult:
    """Everything one directory scan produced, good and bad."""

    records: tuple[SkillRecord, ...] = ()
    errors: tuple[SkillLoadError, ...] = field(default=())

    @property
    def ok(self) -> bool:
        return not self.errors


def load_file(path: Path) -> SkillRecord:
    """Parse one skill file. Raises :class:`ConfigurationError` if it is unusable."""

    if not path.is_file():
        raise ConfigurationError("skill file not found", details={"path": str(path)})
    size = path.stat().st_size
    if size > MAX_SKILL_FILE_BYTES:
        raise ConfigurationError(
            "skill file too large",
            details={"path": str(path), "size": size, "limit": MAX_SKILL_FILE_BYTES},
        )
    text = path.read_text(encoding="utf-8")
    data = _parse(text, path)
    return record_from_mapping(data, source_path=path)


def load_directory(root: Path) -> SkillLoadResult:
    """Load every skill file under ``root``, reporting failures instead of raising.

    A missing directory is not an error. A deployment with no skills yet is normal,
    and refusing to start over it would make the feature mandatory.
    """

    if not root.is_dir():
        return SkillLoadResult()
    records: list[SkillRecord] = []
    errors: list[SkillLoadError] = []
    seen: dict[tuple[str, str], Path] = {}
    for path in _skill_files(root):
        try:
            record = load_file(path)
        except ConfigurationError as exc:
            errors.append(SkillLoadError(path=path, message=exc.message))
            continue
        previous = seen.get(record.key)
        if previous is not None:
            errors.append(
                SkillLoadError(
                    path=path,
                    message=f"duplicate skill {record.label} already defined in {previous.name}",
                )
            )
            continue
        seen[record.key] = path
        records.append(record)
    records.sort(key=lambda record: record.key)
    return SkillLoadResult(records=tuple(records), errors=tuple(errors))


def record_from_mapping(data: dict[str, Any], *, source_path: Path | None = None) -> SkillRecord:
    """Build a record from parsed file content, with the file's own limits applied."""

    skill_id = _require_str(data, "id", source_path)
    body = _text(data.get("body") or data.get("instructions") or "")
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS]
    status = _load_status(data, source_path)
    return SkillRecord(
        skill_id=skill_id,
        version=_text(data.get("version") or "0.1.0"),
        status=status,
        name=_optional_text(data.get("name")),
        description=_text(data.get("description") or ""),
        body=body,
        source_path=None if source_path is None else str(source_path),
        checksum=checksum_for(body),
        required_scopes=_string_tuple(data.get("required_scopes") or data.get("scopes")),
        triggers=_string_tuple(data.get("triggers")),
        evidence_refs=_string_tuple(data.get("evidence_refs") or data.get("evidence")),
        source_task=_optional_text(data.get("source_task")),
    )


def _skill_files(root: Path) -> list[Path]:
    found = [
        path
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix.lower() in SKILL_SUFFIXES
    ]
    return found


def _parse(text: str, path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    try:
        data = json.loads(text) if suffix == ".json" else _parse_yaml(text)
    except ConfigurationError:
        raise
    except Exception as exc:
        raise ConfigurationError(
            "skill file is not valid YAML or JSON",
            details={"path": str(path), "error": str(exc)},
        ) from exc
    if not isinstance(data, dict):
        raise ConfigurationError(
            "skill file must contain a mapping",
            details={"path": str(path), "type": type(data).__name__},
        )
    return data


def _parse_yaml(text: str) -> Any:
    try:
        import yaml
    except ModuleNotFoundError as exc:  # pragma: no cover - declared dependency
        raise ConfigurationError(
            "PyYAML is required to read .yaml skill files",
            details={"error": str(exc)},
        ) from exc
    return yaml.safe_load(text)


def _load_status(data: dict[str, Any], source_path: Path | None) -> SkillStatus:
    raw = data.get("status")
    if raw is None:
        return SkillStatus.DRAFT
    status = parse_status(_text(raw))
    if status not in LOADABLE_STATUSES:
        raise ConfigurationError(
            "a skill file may not declare this status",
            details={
                "path": None if source_path is None else str(source_path),
                "status": status.value,
                "allowed": sorted(item.value for item in LOADABLE_STATUSES),
            },
        )
    return status


def _require_str(data: dict[str, Any], key: str, source_path: Path | None) -> str:
    value = data.get(key) or data.get("skill_id")
    text = _text(value or "")
    if not text:
        raise ConfigurationError(
            f"skill file is missing '{key}'",
            details={"path": None if source_path is None else str(source_path)},
        )
    return text


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else str(value)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = _text(value)
    return text or None


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, Iterable):
        items: Sequence[Any] = list(value)
        return tuple(_text(item) for item in items if _text(item))
    return ()
