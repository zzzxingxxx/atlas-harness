"""Check that the log, the index and the artifacts still agree with each other.

The runtime's whole guarantee is that the JSONL log is the truth and everything
else is rebuildable from it. That is only a guarantee if somebody checks, because
every failure mode here is silent: a SQLite index that lost a row still answers
queries, an artifact whose bytes changed still opens, and a log with a hole in it
still replays -- into a state that quietly omits whatever happened in the hole.

So this module reads both sides and reports the difference, and it deliberately
does not fix anything. Repair belongs to :mod:`atlas_harness.ops.migrate`, which
rebuilds the index from the log. Keeping the two apart is what lets a verify run
against a production data directory without changing a byte of it, which is what
makes it usable both as a release gate and as a startup check.

Findings are data, not exceptions. A log with four bad lines should report four
findings, and a caller deciding whether to ship needs the whole list rather than
whichever problem happened to come first -- so nothing here raises on a broken
log, even though the event store rightly refuses to read one.

Severity is the operator's action rather than a measure of how alarming a finding
looks:

``error``
    The log itself is wrong and nothing in this package can fix it. Restore from
    a backup, or accept the loss knowingly.
``repairable``
    The log is fine and something derived from it drifted. ``atlas reindex``
    fixes it and nothing is lost.
``warning``
    Worth an eye, nothing to do: an orphan artifact file, or an event type newer
    than the schema version its own line claims.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from atlas_harness.context.artifacts import ARTIFACTS_DIRNAME
from atlas_harness.events import compat
from atlas_harness.events.models import (
    CURRENT_SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    Event,
    EventType,
)
from atlas_harness.events.store import EventStore
from atlas_harness.kernel.errors import AtlasError

SEVERITY_ERROR = "error"
SEVERITY_REPAIRABLE = "repairable"
SEVERITY_WARNING = "warning"

SEVERITIES: tuple[str, ...] = (SEVERITY_ERROR, SEVERITY_REPAIRABLE, SEVERITY_WARNING)
"""Worst first, so a report can order findings by severity without a lookup."""

FINDING_CODES: tuple[str, ...] = (
    "log_missing",
    "log_partial_line",
    "log_blank_line",
    "log_not_json",
    "log_not_object",
    "event_invalid",
    "event_foreign_session",
    "schema_unreadable",
    "schema_anachronistic",
    "seq_gap",
    "seq_duplicate",
    "seq_out_of_order",
    "event_id_duplicate",
    "idempotency_duplicate",
    "index_missing_rows",
    "index_stale_rows",
    "index_last_seq_mismatch",
    "index_summary_missing",
    "artifact_missing",
    "artifact_checksum_mismatch",
    "artifact_size_mismatch",
    "artifact_orphan",
    "schema_policy",
)
"""Closed set. A caller may branch on a code, so a new check earns a name here
rather than inventing a message somebody's script has never seen."""


class Finding(BaseModel):
    """One thing that is wrong, in a form both a human and a script can read."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str
    severity: str = SEVERITY_ERROR
    session_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)

    def render(self) -> str:
        where = "" if self.session_id is None else f" {self.session_id}"
        return f"[{self.severity}]{where} {self.code}: {self.message}"

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class SessionVerification(BaseModel):
    """What one session's log, index rows and artifact files add up to."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    log_events: int = 0
    log_last_seq: int = 0
    indexed_events: int = 0
    indexed_last_seq: int = 0
    schema_versions: tuple[int, ...] = ()
    artifacts_referenced: int = 0
    artifacts_intact: int = 0
    findings: tuple[Finding, ...] = ()

    @property
    def ok(self) -> bool:
        """True when nothing needs a human or a rebuild. Warnings do not count."""

        return not any(item.severity != SEVERITY_WARNING for item in self.findings)

    def of_severity(self, severity: str) -> tuple[Finding, ...]:
        return tuple(item for item in self.findings if item.severity == severity)

    def as_json(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        payload["ok"] = self.ok
        return payload

    def render(self) -> list[str]:
        versions = "/".join(str(version) for version in self.schema_versions) or "-"
        head = (
            f"{self.session_id}: {self.log_events} events, last seq {self.log_last_seq}, "
            f"schema v{versions}, "
            f"artifacts {self.artifacts_intact}/{self.artifacts_referenced}"
        )
        return [head, *(f"  {item.render()}" for item in self.findings)]


class VerifyReport(BaseModel):
    """One data directory's verdict, plus the compatibility statement it holds to."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    data_dir: str
    sessions: tuple[SessionVerification, ...] = ()
    findings: tuple[Finding, ...] = ()
    """Findings about the build or the directory rather than about one session."""

    compatibility: dict[str, Any] = Field(default_factory=dict)
    """:func:`atlas_harness.events.compat.history_report`, carried so a report
    filed against a released build says which schema versions that build read."""

    @property
    def all_findings(self) -> tuple[Finding, ...]:
        return self.findings + tuple(item for session in self.sessions for item in session.findings)

    @property
    def ok(self) -> bool:
        return not any(item.severity != SEVERITY_WARNING for item in self.all_findings)

    @property
    def repairable(self) -> bool:
        """True when every non-warning finding is one ``atlas reindex`` would fix.

        Worth its own property because it is the difference between a data
        directory an operator can bring back with a command and one that needs a
        restore, and that is the only decision this report exists to inform.
        """

        findings = [item for item in self.all_findings if item.severity != SEVERITY_WARNING]
        return bool(findings) and all(item.severity == SEVERITY_REPAIRABLE for item in findings)

    def counts(self) -> dict[str, int]:
        findings = self.all_findings
        return {
            severity: sum(1 for item in findings if item.severity == severity)
            for severity in SEVERITIES
        }

    def as_json(self) -> dict[str, Any]:
        return {
            "data_dir": self.data_dir,
            "ok": self.ok,
            "repairable": self.repairable,
            "counts": self.counts(),
            "sessions": [session.as_json() for session in self.sessions],
            "findings": [item.as_json() for item in self.findings],
            "compatibility": self.compatibility,
        }

    def render(self) -> list[str]:
        counts = self.counts()
        lines = [f"data dir: {self.data_dir}", f"sessions: {len(self.sessions)}"]
        lines.extend(f"  {item.render()}" for item in self.findings)
        for session in self.sessions:
            lines.extend(session.render())
        verdict = "ok" if self.ok else ("repairable" if self.repairable else "problems")
        lines.append(
            f"verdict: {verdict} "
            f"({counts[SEVERITY_ERROR]} error, {counts[SEVERITY_REPAIRABLE]} repairable, "
            f"{counts[SEVERITY_WARNING]} warning)"
        )
        return lines


def verify_session(store: EventStore, session_id: str) -> SessionVerification:
    """Read one session's log without trusting it, then check the rest against it."""

    path = store.log_path(session_id)
    if not path.exists():
        return SessionVerification(
            session_id=session_id,
            findings=(
                Finding(
                    code="log_missing",
                    session_id=session_id,
                    message="no event log for this session",
                    details={"path": str(path)},
                ),
            ),
        )
    events, findings = _scan_log(session_id, path)
    indexed = store.index.indexed_event_ids(session_id)
    findings.extend(_check_index(store, session_id, events, indexed))
    referenced, intact, artifact_findings = _check_artifacts(
        session_id, path.parent / ARTIFACTS_DIRNAME, events
    )
    findings.extend(artifact_findings)
    return SessionVerification(
        session_id=session_id,
        log_events=len(events),
        log_last_seq=events[-1].seq if events else 0,
        indexed_events=len(indexed),
        indexed_last_seq=store.index.last_seq(session_id),
        schema_versions=tuple(sorted({event.schema_version for event in events})),
        artifacts_referenced=referenced,
        artifacts_intact=intact,
        findings=tuple(findings),
    )


def verify_data_dir(store: EventStore, *, sessions: Sequence[str] | None = None) -> VerifyReport:
    """Verify every session in a data directory, or only the ones named.

    The schema policy is checked once here rather than per session: it is a
    property of the build, and reporting it against a session would suggest that
    session is at fault for a defect every session shares.
    """

    session_ids = list(sessions) if sessions is not None else store.list_session_ids()
    findings = [Finding(code="schema_policy", message=item) for item in compat.check_policy()]
    return VerifyReport(
        data_dir=str(store.data_dir),
        sessions=tuple(verify_session(store, session_id) for session_id in session_ids),
        findings=tuple(findings),
        compatibility=compat.history_report(),
    )


def _scan_log(session_id: str, path: Path) -> tuple[list[Event], list[Finding]]:
    """Parse every line, keeping the good events and a finding per bad one.

    Deliberately not :meth:`EventStore.iter_events`: that refuses the whole log on
    the first anomaly, which is right for a runtime that must not act on a log it
    does not understand, and wrong for a check whose entire output is the list of
    anomalies.
    """

    events: list[Event] = []
    findings: list[Finding] = []
    seen_event_ids: dict[str, int] = {}
    seen_keys: dict[str, int] = {}
    last_seq = 0

    def report(code: str, message: str, severity: str = SEVERITY_ERROR, **details: Any) -> None:
        findings.append(
            Finding(
                code=code,
                message=message,
                severity=severity,
                session_id=session_id,
                details=details,
            )
        )

    with path.open("r", encoding="utf-8", newline="") as handle:
        for line, raw in enumerate(handle, start=1):
            if not raw.endswith("\n"):
                report(
                    "log_partial_line",
                    "log ends mid-line, so the last append never finished",
                    line=line,
                )
                break
            text = raw.strip()
            if not text:
                report("log_blank_line", "blank line in the log", line=line)
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                report("log_not_json", "line is not valid json", line=line, error=str(exc))
                continue
            if not isinstance(data, dict):
                report("log_not_object", "line is not a json object", line=line)
                continue
            claimed = data.get("schema_version", CURRENT_SCHEMA_VERSION)
            if compat.unreadable([claimed]):
                report(
                    "schema_unreadable",
                    "this build cannot read the schema version the line claims",
                    line=line,
                    schema_version=claimed,
                    supported=sorted(SUPPORTED_SCHEMA_VERSIONS),
                )
                continue
            try:
                event = Event.model_validate(data)
            except (AtlasError, ValidationError) as exc:
                report(
                    "event_invalid",
                    "line does not validate as an event",
                    line=line,
                    error=str(exc),
                )
                continue
            findings.extend(
                _check_event(session_id, event, line, last_seq, seen_event_ids, seen_keys)
            )
            seen_event_ids.setdefault(event.event_id, line)
            seen_keys.setdefault(event.idempotency_key, line)
            last_seq = max(last_seq, event.seq)
            events.append(event)
    return events, findings


def _check_event(
    session_id: str,
    event: Event,
    line: int,
    last_seq: int,
    seen_event_ids: dict[str, int],
    seen_keys: dict[str, int],
) -> list[Finding]:
    """Everything checkable about one valid event given the ones before it."""

    findings: list[Finding] = []

    def report(code: str, message: str, severity: str = SEVERITY_ERROR, **details: Any) -> None:
        findings.append(
            Finding(
                code=code,
                message=message,
                severity=severity,
                session_id=session_id,
                details={"line": line, **details},
            )
        )

    if event.session_id != session_id:
        report(
            "event_foreign_session",
            "event names a different session than the directory holding it",
            event_session_id=event.session_id,
        )
    introduced = compat.version_of(event.event_type)
    if introduced > event.schema_version:
        report(
            "schema_anachronistic",
            "event type is newer than the schema version its own line claims",
            severity=SEVERITY_WARNING,
            event_type=event.event_type.value,
            schema_version=event.schema_version,
            introduced_in=introduced,
        )
    if event.event_id in seen_event_ids:
        report(
            "event_id_duplicate",
            "event id appears twice",
            event_id=event.event_id,
            first_line=seen_event_ids[event.event_id],
        )
    if event.idempotency_key in seen_keys:
        report(
            "idempotency_duplicate",
            "idempotency key appears twice, so one append landed more than once",
            idempotency_key=event.idempotency_key,
            first_line=seen_keys[event.idempotency_key],
        )
    if event.seq == last_seq:
        report("seq_duplicate", "two events share one seq", seq=event.seq)
    elif event.seq < last_seq:
        report("seq_out_of_order", "seq goes backwards", seq=event.seq, previous_seq=last_seq)
    elif event.seq > last_seq + 1:
        report(
            "seq_gap",
            "the log skips events, so the folded state is missing what happened in between",
            seq=event.seq,
            previous_seq=last_seq,
            missing=list(range(last_seq + 1, event.seq)),
        )
    return findings


def _check_index(
    store: EventStore,
    session_id: str,
    events: Sequence[Event],
    indexed: Mapping[int, str],
) -> list[Finding]:
    """Compare the index rows against the log, which always wins.

    Every finding here is ``repairable``, because the index is derived state: a
    disagreement costs a rebuild and never data. This is the plan's named
    "SQLite/JSONL 不一致" risk, and the reason its response is a tool rather than
    a restore.
    """

    findings: list[Finding] = []
    logged = {event.seq: event.event_id for event in events}
    missing = sorted(seq for seq, event_id in logged.items() if indexed.get(seq) != event_id)
    stale = sorted(seq for seq, event_id in indexed.items() if logged.get(seq) != event_id)

    def report(code: str, message: str, **details: Any) -> None:
        findings.append(
            Finding(
                code=code,
                message=message,
                severity=SEVERITY_REPAIRABLE,
                session_id=session_id,
                details=details,
            )
        )

    if missing:
        report(
            "index_missing_rows",
            "events in the log have no row in the index",
            count=len(missing),
            seqs=missing[:20],
        )
    if stale:
        report(
            "index_stale_rows",
            "index rows disagree with the log at that seq",
            count=len(stale),
            seqs=stale[:20],
        )
    log_last_seq = events[-1].seq if events else 0
    index_last_seq = store.index.last_seq(session_id)
    if index_last_seq != log_last_seq:
        report(
            "index_last_seq_mismatch",
            "the index's last seq is not the log's last seq",
            log_last_seq=log_last_seq,
            index_last_seq=index_last_seq,
        )
    if events and store.index.get_session(session_id) is None:
        report("index_summary_missing", "the session has no row in the index's session table")
    return findings


def _check_artifacts(
    session_id: str, directory: Path, events: Sequence[Event]
) -> tuple[int, int, list[Finding]]:
    """Check every artifact the log points at, and notice the files it does not.

    An artifact holds the evidence a truncated tool result refers to, so a missing
    or rewritten one is an ``error``: nothing here can reconstruct it, and a replay
    that quietly dropped it would still look clean. An orphan file is only a
    warning -- the store writes the file before the event on purpose, so an orphan
    is the expected shape of a crash in between.
    """

    findings: list[Finding] = []
    referenced: set[str] = set()
    intact = 0
    for event in events:
        if event.event_type is not EventType.ARTIFACT_STORED:
            continue
        name, problems = _check_one_artifact(session_id, directory, event)
        referenced.add(name)
        findings.extend(problems)
        if not problems:
            intact += 1

    if directory.exists():
        for entry in sorted(directory.iterdir()):
            if entry.is_file() and entry.name not in referenced:
                findings.append(
                    Finding(
                        code="artifact_orphan",
                        message="an artifact file no event points at",
                        severity=SEVERITY_WARNING,
                        session_id=session_id,
                        details={"path": entry.name, "size": entry.stat().st_size},
                    )
                )
    return len(referenced), intact, findings


def _check_one_artifact(
    session_id: str, directory: Path, event: Event
) -> tuple[str, list[Finding]]:
    """Check one ``artifact_stored`` against the file it claims. No findings is intact."""

    payload = event.payload.model_dump(mode="json")
    artifact_id = str(payload.get("artifact_id") or "")
    # ``Path(...).name`` because this path comes out of a log line, and a log line
    # is not a trusted source of a filesystem path.
    name = Path(str(payload.get("path") or f"{artifact_id}.txt")).name
    path = directory / name

    def finding(code: str, message: str, **extra: Any) -> Finding:
        return Finding(
            code=code,
            message=message,
            session_id=session_id,
            details={"artifact_id": artifact_id, "path": name, **extra},
        )

    if not path.exists():
        return name, [
            finding("artifact_missing", "the log references an artifact file that is not there")
        ]
    raw = path.read_bytes()
    recorded_checksum = str(payload.get("checksum") or "")
    digest = hashlib.sha256(raw).hexdigest()
    if recorded_checksum and digest != recorded_checksum:
        return name, [
            finding(
                "artifact_checksum_mismatch",
                "the artifact's bytes no longer hash to the checksum the log recorded",
                recorded=recorded_checksum,
                actual=digest,
            )
        ]
    recorded_size = payload.get("size")
    if isinstance(recorded_size, int) and recorded_size != len(raw):
        return name, [
            finding(
                "artifact_size_mismatch",
                "the artifact's size is not the size the log recorded",
                recorded=recorded_size,
                actual=len(raw),
            )
        ]
    return name, []
