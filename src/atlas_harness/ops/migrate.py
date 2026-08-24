"""Rebuild derived state from the log, which is the only migration this runtime has.

There are no data migrations here, and that absence is a design decision rather
than an omission. The schema policy in :mod:`atlas_harness.events.compat` is
forward-only and additive: a new version may add event types and defaulted fields
and may not redefine an existing one. A log written by any supported build
therefore folds on today's build as it is, so there is never an old log to rewrite
-- and rewriting one would destroy the append-only guarantee that makes the log
usable as evidence in the first place.

What *does* need repairing is everything derived from the log. The SQLite index is
a cache: it makes ``atlas sessions`` fast and enforces idempotency-key uniqueness,
but every row in it is reconstructible. When it disagrees with the log -- the plan's
named ``SQLite/JSONL 不一致`` risk -- the response is to rebuild it, and the log
wins outright. That is what this module does.

The rebuild is per session and transactional, deleting the old rows and inserting
the log's in one ``BEGIN IMMEDIATE``, so a crash mid-rebuild leaves the index
either wholly old or wholly new and never a mixture of two histories. And a session
whose log does not parse is refused rather than partially indexed: derived state
cannot be rebuilt from a source that cannot be read, and indexing the readable
prefix would produce an index that looks complete and is not.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from atlas_harness.events.store import EventStore
from atlas_harness.kernel.errors import AtlasError


class SessionReindex(BaseModel):
    """What the rebuild did to one session's index rows."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    events: int = 0
    inserted: int = 0
    deleted: int = 0
    """Rows the index held that the log does not vouch for. Non-zero here is the
    interesting case: it means the index carried events the log never recorded."""

    changed: bool = False
    ok: bool = True
    reason: str = ""
    """Why the session was skipped, when it was. Empty on success."""

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def render(self) -> str:
        if not self.ok:
            return f"{self.session_id}: skipped, {self.reason}"
        if not self.changed:
            return f"{self.session_id}: already consistent ({self.events} events)"
        return (
            f"{self.session_id}: reindexed {self.events} events "
            f"(+{self.inserted} rows, -{self.deleted} stale)"
        )


class ReindexReport(BaseModel):
    """The rebuild across a whole data directory."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    data_dir: str
    sessions: tuple[SessionReindex, ...] = ()

    @property
    def ok(self) -> bool:
        """True when every session's index now matches its log.

        A skipped session makes this false. The index is consistent for the
        sessions that were rebuilt, but the directory is not, and a caller using
        this as a gate must not read a partial success as a pass.
        """

        return all(entry.ok for entry in self.sessions)

    @property
    def changed(self) -> tuple[SessionReindex, ...]:
        return tuple(entry for entry in self.sessions if entry.changed)

    @property
    def skipped(self) -> tuple[SessionReindex, ...]:
        return tuple(entry for entry in self.sessions if not entry.ok)

    def as_json(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        payload["ok"] = self.ok
        payload["changed"] = len(self.changed)
        payload["skipped"] = len(self.skipped)
        return payload

    def render(self) -> list[str]:
        lines = [f"data dir: {self.data_dir}", f"sessions: {len(self.sessions)}"]
        lines.extend(f"  {entry.render()}" for entry in self.sessions)
        lines.append(
            f"verdict: {'ok' if self.ok else 'incomplete'} "
            f"({len(self.changed)} rebuilt, {len(self.skipped)} skipped)"
        )
        return lines


def reindex_session(store: EventStore, session_id: str) -> SessionReindex:
    """Replace one session's index rows with what its log says.

    Reads the log strictly on purpose. ``read_events`` refuses a log with a gap, a
    bad line or a foreign session id, and those are exactly the conditions under
    which an index rebuild would install a confident-looking lie. The refusal is
    reported so ``atlas verify`` and this command agree about which session is the
    problem.
    """

    log_path = store.log_path(session_id)
    if not log_path.exists():
        return SessionReindex(session_id=session_id, ok=False, reason="no log on disk")

    try:
        events = store.read_events(session_id)
    except AtlasError as error:
        return SessionReindex(
            session_id=session_id,
            ok=False,
            reason=f"log does not parse: {error.message}",
        )

    indexed = store.index.indexed_event_ids(session_id)
    logged = {event.seq: event.event_id for event in events}
    stale = sorted(seq for seq, event_id in indexed.items() if logged.get(seq) != event_id)
    fresh = [event for event in events if indexed.get(event.seq) != event.event_id]
    consistent = store.index.last_seq(session_id) == (events[-1].seq if events else 0)

    if not stale and not fresh and consistent and store.index.get_session(session_id) is not None:
        return SessionReindex(session_id=session_id, events=len(events))

    # Delete first, insert second, one transaction: an event id or idempotency key
    # that moved to a different seq would collide with its own old row otherwise.
    # Only the rows the index does not already hold are inserted -- event_id and
    # idempotency_key are unique, so re-inserting a row that is already correct
    # fails the whole rebuild.
    store.index.write(session_id, insert=fresh, delete_seqs=stale)
    return SessionReindex(
        session_id=session_id,
        events=len(events),
        inserted=len(fresh),
        deleted=len(stale),
        changed=True,
    )


def rebuild_index(
    store: EventStore,
    *,
    sessions: Sequence[str] | None = None,
) -> ReindexReport:
    """Rebuild the index for every session in a data directory, or the named ones.

    Sessions are discovered with ``list_session_ids``, which reads the sessions
    directory rather than the index. Asking the index which sessions exist would
    make a session it has entirely forgotten unrecoverable by the tool whose job is
    to recover it.
    """

    session_ids = list(sessions) if sessions is not None else store.list_session_ids()
    return ReindexReport(
        data_dir=str(store.data_dir),
        sessions=tuple(reindex_session(store, session_id) for session_id in session_ids),
    )


def rebuild_index_at(data_dir: Path, *, sessions: Sequence[str] | None = None) -> ReindexReport:
    """Rebuild the index of a data directory this process has no store for.

    Convenience for the restore path, which has just written logs into a fresh
    directory and needs an index built before anything serves traffic from it.
    """

    with EventStore(data_dir) as store:
        return rebuild_index(store, sessions=sessions)
