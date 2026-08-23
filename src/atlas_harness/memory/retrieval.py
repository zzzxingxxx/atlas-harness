"""Keyword retrieval over memory records, ranked deterministically.

The plan requires that the same task produce the same ordering. BM25 alone does not
give that: SQLite returns rows in whatever order the index walks them, and two
records with identical scores can come back either way round. So the score is only
the first key here. :func:`search` sorts by ``(-score, -created_at_ms, memory_id)``,
and the memory id is a total order, which makes the tail deterministic no matter
what the index does.

The score itself is BM25 scaled by the record's layer weight, so a procedural memory
outranks an episodic one that matched equally well. Expiry is checked against the
clock before ranking rather than after: an expired record is ineligible, not merely
unlucky, and filtering afterwards would let it displace an eligible record from a
limit-N result.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from atlas_harness.kernel.clock import Clock, SystemClock
from atlas_harness.memory.models import LAYER_WEIGHT, MemoryLayer, MemoryRecord
from atlas_harness.memory.repository import MEMORY_COLUMNS, MemoryRepository, record_from_row

DEFAULT_LIMIT = 5
"""How many records retrieval returns by default. The capability slot holds a few
relevant items; a large result set just moves the trimming problem downstream."""

_TOKEN_PATTERN = re.compile(r"[0-9A-Za-z_]+|[^\s0-9A-Za-z_]", re.UNICODE)

_SELECT_COLUMNS = ", ".join(f"m.{column}" for column in MEMORY_COLUMNS)

_SEARCH_SQL = f"""
SELECT {_SELECT_COLUMNS}, bm25(memories_fts, 1.0, 0.5) AS rank
FROM memories_fts
JOIN memories AS m ON m.memory_id = memories_fts.memory_id
WHERE memories_fts MATCH ?
ORDER BY rank
LIMIT ?
"""


@dataclass(frozen=True)
class MemoryHit:
    """One retrieved record with the score that put it where it is."""

    record: MemoryRecord
    score: float
    text_score: float

    @property
    def layer(self) -> MemoryLayer:
        return self.record.layer


def build_match_query(query: str) -> str:
    """Turn free text into an FTS5 MATCH expression.

    Every token is quoted and the tokens are OR-ed. Quoting is not cosmetic: an
    unquoted ``AND``, ``NEAR`` or bare ``*`` in a user's task description is FTS5
    syntax, and letting it through would either change the query's meaning or raise
    an operational error on text that is perfectly ordinary prose.
    """

    tokens = [token for token in _TOKEN_PATTERN.findall(query) if token.strip()]
    quoted = [f'"{token}"' for token in tokens if _TOKEN_PATTERN.fullmatch(token)]
    return " OR ".join(quoted)


def score_for(text_score: float, layer: MemoryLayer, confidence: float) -> float:
    """Combine the text score with the record's layer and confidence.

    BM25 in SQLite is negative and better matches are more negative, so the sign is
    flipped first: everything downstream reads "higher is better", and a ranking
    function that means the opposite of what it looks like is a bug waiting to be
    written.
    """

    relevance = -text_score
    return relevance * LAYER_WEIGHT[layer] * (0.5 + 0.5 * confidence)


class MemoryRetriever:
    """Search the FTS index, then rank and filter what it returns."""

    def __init__(self, repository: MemoryRepository, *, clock: Clock | None = None) -> None:
        self.repository = repository
        self._clock = clock or SystemClock()

    @property
    def _connection(self) -> sqlite3.Connection:
        return self.repository.store.index.connection

    def search(
        self,
        query: str,
        *,
        limit: int = DEFAULT_LIMIT,
        layers: frozenset[MemoryLayer] | None = None,
        now_ms: int | None = None,
        long_term_only: bool = False,
    ) -> list[MemoryHit]:
        """Return the best eligible matches, most relevant first.

        ``long_term_only`` restricts the result to layers a prompt may present as
        standing fact, which is how a caller asks for durable knowledge rather than
        whatever happened to be observed during some earlier run.
        """

        match = build_match_query(query)
        if not match:
            return []
        moment = self._clock.now_ms() if now_ms is None else now_ms
        try:
            rows = self._connection.execute(_SEARCH_SQL, (match, max(limit, 1) * 4)).fetchall()
        except sqlite3.OperationalError:
            # A malformed MATCH is a query problem, not a reason to fail the run.
            return []
        hits: list[MemoryHit] = []
        for row in rows:
            record = record_from_row(row[: len(MEMORY_COLUMNS)])
            if not record.is_injectable(moment):
                continue
            if layers is not None and record.layer not in layers:
                continue
            if long_term_only and not record.is_long_term:
                continue
            text_score = float(row[len(MEMORY_COLUMNS)] or 0.0)
            hits.append(
                MemoryHit(
                    record=record,
                    score=score_for(text_score, record.layer, record.confidence),
                    text_score=text_score,
                )
            )
        hits.sort(key=_sort_key)
        return hits[:limit]

    def expired(self, *, now_ms: int | None = None) -> list[MemoryRecord]:
        """Records the clock has retired. Useful for a sweep and for explaining a miss."""

        moment = self._clock.now_ms() if now_ms is None else now_ms
        return [record for record in self.repository.all() if record.is_expired(moment)]


def _sort_key(hit: MemoryHit) -> tuple[float, int, str]:
    return (-hit.score, -hit.record.created_at_ms, hit.record.memory_id)
