"""Secret redaction, output truncation and binary detection for tool results."""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[redacted]"

TRUNCATION_NOTE = "\n[truncated]"

SECRET_NAME_PATTERN = (
    r"(?:api[_-]?key|secret[_-]?key|secret|token|password|passwd|pwd"
    r"|access[_-]?key|private[_-]?key|credential[s]?|authorization)"
)

_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        REDACTED,
    ),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\bASIA[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), REDACTED),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), REDACTED),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"), REDACTED),
    (
        re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
        REDACTED,
    ),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{12,}"), f"Bearer {REDACTED}"),
    (re.compile(r"(?i)\b(basic)\s+[A-Za-z0-9+/=]{12,}"), rf"\1 {REDACTED}"),
    (re.compile(r"://[^/\s:@]+:[^/\s@]+@"), f"://{REDACTED}@"),
    (
        re.compile(rf"(?i)\b({SECRET_NAME_PATTERN})(\s*[:=]\s*)(\"|')?[^\s\"',;]{{4,}}"),
        rf"\1\2\3{REDACTED}",
    ),
)


def redact(text: str) -> str:
    """Replace anything that looks like a credential with a fixed marker."""

    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text


def redact_value(value: Any) -> Any:
    """Recursively redact strings inside a normalized tool result."""

    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [redact_value(item) for item in value]
    return value


def truncate_text(text: str, max_bytes: int) -> tuple[str, bool]:
    """Cut a string to a byte budget without splitting a UTF-8 code point."""

    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    note = TRUNCATION_NOTE.encode("utf-8")
    if max_bytes <= len(note):
        return note[:max_bytes].decode("utf-8", errors="ignore"), True
    keep = max_bytes - len(note)
    head = encoded[:keep].decode("utf-8", errors="ignore")
    return f"{head}{TRUNCATION_NOTE}", True


def looks_binary(data: bytes) -> bool:
    """Treat NUL bytes and undecodable content as binary."""

    if b"\x00" in data:
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False
