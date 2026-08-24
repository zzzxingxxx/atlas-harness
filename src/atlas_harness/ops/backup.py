"""Copy a data directory somewhere safe, and prove later that the copy is intact.

The plan's release step is "备份并校验 SQLite、JSONL 和 artifacts" -- back up and
*verify* all three. The verification half is the part that carries the weight: an
unverified backup is a belief, and the moment it is needed is the worst possible
moment to discover the belief was wrong. So every file copied here is hashed at
copy time and the hash is written into a manifest, which makes a restore an
operation that can fail loudly instead of one that quietly installs damaged bytes.

Three decisions in here are worth stating because they are not obvious:

* **The manifest is written last.** An interrupted backup therefore has no
  manifest, and a directory with no manifest is refused rather than half-restored.
  A manifest written first would describe a copy that does not exist yet.
* **The SQLite index is copied through SQLite**, not through the filesystem.
  The index runs in WAL mode, so a live database's bytes on disk are only half the
  story -- the rest is in a ``-wal`` file that a naive copy either misses or
  catches mid-transaction. ``Connection.backup`` takes a consistent snapshot of a
  database that is being written to, which is the only correct way to copy one.
* **Each session's state hash is recorded.** That is what turns "the bytes match"
  into "this log still folds to the state it folded to", which is the plan's other
  release requirement: 验证从上一版本恢复 Session 的兼容性. A restore recomputes
  it on today's build, so a schema change that broke an old log fails the restore
  rather than surfacing weeks later as a wrong answer.

The index is backed up for speed of recovery only. It is derived state, and
:mod:`atlas_harness.ops.migrate` can rebuild it from the logs, so a backup whose
index is missing or stale is still a complete backup of everything irreplaceable.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from atlas_harness import __version__
from atlas_harness.context.artifacts import ARTIFACTS_DIRNAME
from atlas_harness.events.models import CURRENT_SCHEMA_VERSION
from atlas_harness.events.reducer import replay
from atlas_harness.events.store import (
    INDEX_FILENAME,
    LOG_FILENAME,
    SESSIONS_DIRNAME,
    EventStore,
)
from atlas_harness.kernel.clock import Clock, SystemClock
from atlas_harness.kernel.errors import AtlasError, ConfigurationError

MANIFEST_FILENAME = "backup-manifest.json"
MANIFEST_FORMAT = 1

_HASH_CHUNK = 1 << 20


def _digest(path: Path) -> tuple[str, int]:
    """The sha256 and byte length of one file, read in chunks.

    Chunked because a JSONL log has no size limit -- the plan's performance
    scenario is explicitly 大日志 -- and a backup tool that needs the largest file
    in memory fails exactly when it is most needed.
    """

    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


class BackupFile(BaseModel):
    """One copied file, addressed relative to the backup root."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    """Posix-separated and relative, so a backup taken on Windows verifies on
    Linux. An absolute path would also let a hand-edited manifest point a restore
    at anything on the filesystem."""

    sha256: str
    size: int

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class SessionBackup(BaseModel):
    """One session's copied log and artifacts, plus what it should fold to."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    events: int = 0
    last_seq: int = 0
    schema_versions: tuple[int, ...] = ()
    state_hash: str = ""
    foldable: bool = True
    """False when the log did not fold at backup time. Backing up a damaged log is
    still the right move -- it is the only copy of the damage -- so this records the
    fact rather than refusing the backup."""

    log: BackupFile
    artifacts: tuple[BackupFile, ...] = ()

    def files(self) -> tuple[BackupFile, ...]:
        return (self.log, *self.artifacts)

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class BackupManifest(BaseModel):
    """What a backup contains, and what every byte of it should hash to."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest_format: int = MANIFEST_FORMAT
    created_at_ms: int = 0
    atlas_version: str = __version__
    current_schema_version: int = CURRENT_SCHEMA_VERSION
    """The build that took the backup. A restore that folds to a different state
    hash needs this to say which version to blame."""

    source: str = ""
    sessions: tuple[SessionBackup, ...] = ()
    index: BackupFile | None = None

    def files(self) -> tuple[BackupFile, ...]:
        listed = [item for session in self.sessions for item in session.files()]
        if self.index is not None:
            listed.append(self.index)
        return tuple(listed)

    def session(self, session_id: str) -> SessionBackup | None:
        for entry in self.sessions:
            if entry.session_id == session_id:
                return entry
        return None

    def as_json(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        payload["files"] = len(self.files())
        payload["bytes"] = sum(item.size for item in self.files())
        return payload

    def render(self) -> list[str]:
        total = sum(item.size for item in self.files())
        lines = [
            f"source: {self.source}",
            f"atlas {self.atlas_version}, schema v{self.current_schema_version}",
            f"sessions: {len(self.sessions)}, files: {len(self.files())}, bytes: {total}",
        ]
        for entry in self.sessions:
            suffix = "" if entry.foldable else " (log did not fold)"
            lines.append(
                f"  {entry.session_id}: {entry.events} events, "
                f"{len(entry.artifacts)} artifacts{suffix}"
            )
        lines.append(f"index: {'copied' if self.index is not None else 'not copied'}")
        return lines


class BackupVerification(BaseModel):
    """Whether a backup on disk still matches the manifest that describes it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    directory: str
    checked: int = 0
    missing: tuple[str, ...] = ()
    corrupt: tuple[str, ...] = ()
    """Present but hashing differently than recorded. Bit rot, a partial copy or an
    edit -- indistinguishable from here, and all three mean do not restore this."""

    unlisted: tuple[str, ...] = ()
    """Files in the directory the manifest does not mention. A warning only: they
    are ignored by a restore, but they suggest two backups were written on top of
    each other, and the older one's leftovers are not covered by these hashes."""

    @property
    def ok(self) -> bool:
        return not self.missing and not self.corrupt

    def as_json(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        payload["ok"] = self.ok
        return payload

    def render(self) -> list[str]:
        lines = [f"backup: {self.directory}", f"checked: {self.checked} files"]
        lines.extend(f"  missing: {name}" for name in self.missing)
        lines.extend(f"  corrupt: {name}" for name in self.corrupt)
        lines.extend(f"  unlisted: {name}" for name in self.unlisted)
        lines.append(f"verdict: {'ok' if self.ok else 'damaged'}")
        return lines


class RestoredSession(BaseModel):
    """One restored session, folded on today's build and compared to the manifest.

    ``matches`` is the compatibility answer. The bytes were already proven equal by
    the checksum, so a hash that differs here cannot be the log's fault -- it means
    this build folds that log differently than the build that backed it up, which is
    precisely the backward-compatibility break the release gate exists to catch.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    events: int = 0
    schema_version: int = 0
    state_hash: str = ""
    expected_state_hash: str = ""
    matches: bool = True

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class RestoreReport(BaseModel):
    """What a restore wrote, and whether the restored logs still fold the same."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str
    target: str
    files_written: int = 0
    sessions: tuple[RestoredSession, ...] = ()
    index_restored: bool = False
    notes: tuple[str, ...] = Field(default_factory=tuple)

    @property
    def compatible(self) -> bool:
        return all(entry.matches for entry in self.sessions)

    def as_json(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        payload["compatible"] = self.compatible
        return payload

    def render(self) -> list[str]:
        lines = [
            f"restored {self.files_written} files from {self.source}",
            f"target: {self.target}",
            f"index: {'restored' if self.index_restored else 'rebuild required'}",
        ]
        for entry in self.sessions:
            verdict = "same state" if entry.matches else "STATE HASH CHANGED"
            lines.append(f"  {entry.session_id}: {entry.events} events, {verdict}")
        lines.extend(f"  note: {note}" for note in self.notes)
        lines.append(f"verdict: {'compatible' if self.compatible else 'incompatible'}")
        return lines


def _copy(source: Path, destination: Path, relative: str) -> BackupFile:
    """Copy one file and hash the copy, not the original.

    Hashing the destination is deliberate: it proves the bytes that were actually
    written are the bytes the manifest promises. Hashing the source would certify a
    file nobody will ever read again and miss a truncated write entirely.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    checksum, size = _digest(destination)
    return BackupFile(path=relative, sha256=checksum, size=size)


def _copy_index(source: Path, destination: Path) -> BackupFile | None:
    """Snapshot the SQLite index through SQLite's online backup API.

    Returns ``None`` when there is no index yet, which is a normal state: the index
    is built on first use, and a data directory that has only ever been written to
    by a crashed process may not have one. The logs are what matter.
    """

    if not source.exists():
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    origin = sqlite3.connect(str(source))
    try:
        copy = sqlite3.connect(str(destination))
        try:
            origin.backup(copy)
        finally:
            copy.close()
    finally:
        origin.close()
    checksum, size = _digest(destination)
    return BackupFile(path=INDEX_FILENAME, sha256=checksum, size=size)


def _fold(store: EventStore, session_id: str) -> tuple[int, int, tuple[int, ...], str, bool]:
    """Read one log and fold it, reporting rather than raising if it will not.

    A log that does not fold is exactly the log most worth having a backup of, so a
    parse failure downgrades to ``foldable=False`` instead of aborting the backup.
    """

    try:
        events = store.read_events(session_id)
    except AtlasError:
        return 0, 0, (), "", False
    if not events:
        return 0, 0, (), "", True
    versions = tuple(sorted({event.schema_version for event in events}))
    try:
        state = replay(events, session_id=session_id)
    except AtlasError:
        return len(events), events[-1].seq, versions, "", False
    return len(events), state.last_seq, versions, state.state_hash(), True


def create_backup(
    store: EventStore,
    destination: Path,
    *,
    sessions: Sequence[str] | None = None,
    include_index: bool = True,
    clock: Clock | None = None,
) -> BackupManifest:
    """Copy logs, artifacts and the index into ``destination`` and describe them.

    Takes an open store rather than a path so a caller that already has one does
    not open a second connection to the same SQLite file, and so the session list
    comes from the same place every other command reads it from.
    """

    resolved_clock = clock or SystemClock()
    destination.mkdir(parents=True, exist_ok=True)
    session_ids = list(sessions) if sessions is not None else store.list_session_ids()

    entries: list[SessionBackup] = []
    for session_id in session_ids:
        log_path = store.log_path(session_id)
        if not log_path.exists():
            raise ConfigurationError(
                "cannot back up a session with no log",
                details={"session_id": session_id, "path": str(log_path)},
            )
        relative_dir = f"{SESSIONS_DIRNAME}/{session_id}"
        log = _copy(
            log_path,
            destination / SESSIONS_DIRNAME / session_id / LOG_FILENAME,
            f"{relative_dir}/{LOG_FILENAME}",
        )

        artifacts: list[BackupFile] = []
        artifact_dir = log_path.parent / ARTIFACTS_DIRNAME
        if artifact_dir.is_dir():
            for item in sorted(artifact_dir.iterdir()):
                if not item.is_file():
                    continue
                artifacts.append(
                    _copy(
                        item,
                        destination / SESSIONS_DIRNAME / session_id / ARTIFACTS_DIRNAME / item.name,
                        f"{relative_dir}/{ARTIFACTS_DIRNAME}/{item.name}",
                    )
                )

        events, last_seq, versions, state_hash, foldable = _fold(store, session_id)
        entries.append(
            SessionBackup(
                session_id=session_id,
                events=events,
                last_seq=last_seq,
                schema_versions=versions,
                state_hash=state_hash,
                foldable=foldable,
                log=log,
                artifacts=tuple(artifacts),
            )
        )

    index = None
    if include_index:
        index = _copy_index(store.data_dir / INDEX_FILENAME, destination / INDEX_FILENAME)

    manifest = BackupManifest(
        created_at_ms=resolved_clock.now_ms(),
        source=str(store.data_dir),
        sessions=tuple(entries),
        index=index,
    )
    # Written last, so an interrupted backup is recognisably incomplete.
    (destination / MANIFEST_FILENAME).write_text(
        json.dumps(manifest.as_json(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def read_manifest(directory: Path) -> BackupManifest:
    """Load a backup's manifest, refusing a directory that has none."""

    path = directory / MANIFEST_FILENAME
    if not path.exists():
        raise ConfigurationError(
            "not a backup directory: no manifest",
            details={"directory": str(directory), "expected": MANIFEST_FILENAME},
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ConfigurationError(
            "backup manifest is not valid JSON",
            details={"path": str(path), "error": str(error)},
        ) from error
    if not isinstance(data, dict):
        raise ConfigurationError("backup manifest is not an object", details={"path": str(path)})
    data.pop("files", None)
    data.pop("bytes", None)
    return BackupManifest.model_validate(data)


def verify_backup(directory: Path) -> BackupVerification:
    """Re-hash every file the manifest lists and report what no longer matches."""

    manifest = read_manifest(directory)
    listed = manifest.files()
    missing: list[str] = []
    corrupt: list[str] = []

    for item in listed:
        path = directory / Path(item.path)
        if not path.exists():
            missing.append(item.path)
            continue
        checksum, size = _digest(path)
        if checksum != item.sha256 or size != item.size:
            corrupt.append(item.path)

    known = {item.path for item in listed} | {MANIFEST_FILENAME}
    unlisted = sorted(
        found.relative_to(directory).as_posix()
        for found in directory.rglob("*")
        if found.is_file() and found.relative_to(directory).as_posix() not in known
    )

    return BackupVerification(
        directory=str(directory),
        checked=len(listed),
        missing=tuple(missing),
        corrupt=tuple(corrupt),
        unlisted=tuple(unlisted),
    )


def restore_backup(directory: Path, target: Path, *, force: bool = False) -> RestoreReport:
    """Verify a backup, write it into ``target``, then prove the logs still fold.

    The order is the whole design. Verifying first means damaged bytes are never
    written over good ones; folding afterwards means a restore that succeeded
    mechanically but produces different state is still reported as a failure,
    because that is the only signal a backward-compatibility break gives.

    A non-empty target is refused unless ``force``. Restoring on top of a live data
    directory would interleave two histories of the same session ids, and the log is
    append-only precisely so that cannot happen by accident.
    """

    verification = verify_backup(directory)
    if not verification.ok:
        raise ConfigurationError(
            "refusing to restore a backup that failed verification",
            details={
                "directory": str(directory),
                "missing": list(verification.missing),
                "corrupt": list(verification.corrupt),
            },
        )

    manifest = read_manifest(directory)
    if target.exists() and any(target.iterdir()) and not force:
        raise ConfigurationError(
            "refusing to restore into a non-empty directory without force",
            details={"target": str(target)},
        )

    target.mkdir(parents=True, exist_ok=True)
    written = 0
    for item in manifest.files():
        source = directory / Path(item.path)
        landing = target / Path(item.path)
        landing.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, landing)
        written += 1

    notes: list[str] = []
    if manifest.index is None:
        notes.append("no index in the backup; run atlas reindex before serving traffic")
    if manifest.current_schema_version != CURRENT_SCHEMA_VERSION:
        notes.append(
            f"backup was taken at schema v{manifest.current_schema_version}, "
            f"this build writes v{CURRENT_SCHEMA_VERSION}"
        )

    restored: list[RestoredSession] = []
    with EventStore(target) as store:
        for entry in manifest.sessions:
            events, _, versions, state_hash, foldable = _fold(store, entry.session_id)
            if not foldable:
                notes.append(f"{entry.session_id}: restored log does not fold on this build")
            restored.append(
                RestoredSession(
                    session_id=entry.session_id,
                    events=events,
                    schema_version=max(versions) if versions else 0,
                    state_hash=state_hash,
                    expected_state_hash=entry.state_hash,
                    matches=bool(foldable and state_hash == entry.state_hash),
                )
            )

    return RestoreReport(
        source=str(directory),
        target=str(target),
        files_written=written,
        sessions=tuple(restored),
        index_restored=manifest.index is not None,
        notes=tuple(notes),
    )
