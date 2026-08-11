"""Private local usage analytics for context reads.

Usage can contain raw queries, so state lives outside the public brain checkout
under ~/.local/state/open-index rather than beside source-controlled files.

Two levels are recorded. `context_fetches` is one row per read — the question.
`retrieval_results` is one row per document that read returned — the answer,
with the score and the reason it matched. The second is what makes an agent's
memory debuggable: "this turn retrieved that document, ranked third, on a
semantic match at 0.71" is answerable, and "why did it think that?" stops being
guesswork.

Both carry the caller's `trace_id` when one was supplied, so a turn can be
followed from the agent's side back into the index.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# How many reads to keep. Per-result rows multiply volume by the page size, and
# this is a local debugging aid, not a warehouse. OPEN_INDEX_ANALYTICS_MAX
# raises it, or 0 disables pruning for a deployment that ships the file
# somewhere durable.
_DEFAULT_MAX_FETCHES = 50_000


def _as_float(value: Any) -> Optional[float]:
    """Best-effort float. Analytics must never fail a read it is describing."""
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


class AnalyticsStore:
    """Record and aggregate the context that CLI/MCP clients retrieve."""

    def __init__(self, brain_root: Optional[Path]):
        identity = str((brain_root or Path.cwd()).resolve())
        slug = hashlib.sha256(identity.encode()).hexdigest()[:16]
        state_home = Path.home() / ".local" / "state" / "open-index"
        state_home.mkdir(parents=True, exist_ok=True)
        self.path = state_home / f"{slug}.db"
        self._lock = threading.Lock()
        try:
            self._max_fetches = int(
                os.environ.get("OPEN_INDEX_ANALYTICS_MAX", _DEFAULT_MAX_FETCHES))
        except ValueError:
            self._max_fetches = _DEFAULT_MAX_FETCHES
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS context_fetches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fetched_at TEXT NOT NULL,
                source TEXT NOT NULL,
                operation TEXT NOT NULL,
                query TEXT,
                doc_types TEXT,
                entity_id TEXT,
                result_count INTEGER,
                result_doc_types TEXT,
                duration_ms REAL NOT NULL,
                success INTEGER NOT NULL,
                error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_context_fetches_at
                ON context_fetches(fetched_at);

            -- One row per document returned. Deliberately not a foreign key
            -- with ON DELETE CASCADE: pruning deletes from both tables in one
            -- transaction, and a hard constraint would turn an analytics
            -- bookkeeping slip into a failed read.
            CREATE TABLE IF NOT EXISTS retrieval_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fetch_id INTEGER NOT NULL,
                rank INTEGER NOT NULL,
                entity_id TEXT NOT NULL,
                doc_type TEXT,
                score REAL,
                keyword_score REAL,
                semantic_score REAL,
                match_type TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_retrieval_fetch
                ON retrieval_results(fetch_id);
            CREATE INDEX IF NOT EXISTS idx_retrieval_entity
                ON retrieval_results(entity_id);
            """
        )
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Add columns an older state database predates.

        These files live in the user's state directory and outlive any single
        version, so a new column has to arrive by ALTER rather than by assuming
        CREATE TABLE ran with it.
        """
        existing = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(context_fetches)")
        }
        if "trace_id" not in existing:
            self._conn.execute("ALTER TABLE context_fetches ADD COLUMN trace_id TEXT")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_context_fetches_trace "
                "ON context_fetches(trace_id)"
            )

    def record(
        self,
        *,
        source: str,
        operation: str,
        duration_ms: float,
        query: Optional[str] = None,
        doc_types: Optional[list[str]] = None,
        entity_id: Optional[str] = None,
        result_count: Optional[int] = None,
        result_doc_types: Optional[dict[str, int]] = None,
        success: bool = True,
        error: Optional[str] = None,
        trace_id: Optional[str] = None,
        results: Optional[list[dict[str, Any]]] = None,
    ) -> Optional[int]:
        """Record one read, and the documents it returned. Returns the fetch id.

        `results` are the rows the caller actually received, in the order they
        were received: rank is position, not score order, because that is what
        the agent saw.
        """
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO context_fetches (
                    fetched_at, source, operation, query, doc_types, entity_id,
                    result_count, result_doc_types, duration_ms, success, error,
                    trace_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(), source, operation, query,
                    json.dumps(doc_types or []), entity_id, result_count,
                    json.dumps(result_doc_types or {}), round(duration_ms, 2),
                    int(success), error, trace_id,
                ),
            )
            fetch_id = cur.lastrowid
            if results:
                self._conn.executemany(
                    """
                    INSERT INTO retrieval_results (
                        fetch_id, rank, entity_id, doc_type, score,
                        keyword_score, semantic_score, match_type
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            fetch_id, i, str(r.get("id") or ""), r.get("doc_type"),
                            _as_float(r.get("score")),
                            _as_float((r.get("match") or {}).get("keyword_score")),
                            _as_float((r.get("match") or {}).get("semantic_score")),
                            (r.get("match") or {}).get("type"),
                        )
                        for i, r in enumerate(results, start=1)
                        if r.get("id")
                    ],
                )
            self._conn.commit()
            self._prune_locked()
            return fetch_id

    def _prune_locked(self) -> None:
        """Keep the state file bounded.

        Per-result rows multiply volume by the page size — a few thousand
        searches is tens of thousands of rows — and this is a debugging aid on
        someone's laptop, not a warehouse. Oldest fetches go first, with their
        results, so a trace is either wholly present or wholly gone rather than
        surviving as a fetch with no documents.
        """
        if self._max_fetches <= 0:
            return
        row = self._conn.execute("SELECT COUNT(*) FROM context_fetches").fetchone()
        if row[0] <= self._max_fetches:
            return
        cutoff = self._conn.execute(
            "SELECT id FROM context_fetches ORDER BY id DESC LIMIT 1 OFFSET ?",
            (self._max_fetches - 1,),
        ).fetchone()
        if cutoff is None:
            return
        self._conn.execute("DELETE FROM retrieval_results WHERE fetch_id < ?", (cutoff[0],))
        self._conn.execute("DELETE FROM context_fetches WHERE id < ?", (cutoff[0],))
        self._conn.commit()

    def results_for(self, fetch_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
        """The documents each of those fetches returned, keyed by fetch id."""
        if not fetch_ids:
            return {}
        placeholders = ",".join("?" * len(fetch_ids))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM retrieval_results WHERE fetch_id IN ({placeholders}) "
                f"ORDER BY fetch_id, rank",
                fetch_ids,
            ).fetchall()
        out: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            out.setdefault(row["fetch_id"], []).append(dict(row))
        return out

    def by_trace(self, trace_id: str) -> list[dict[str, Any]]:
        """Every read made under one trace id, each with the documents it returned."""
        if not trace_id:
            return []
        with self._lock:
            fetches = self._conn.execute(
                "SELECT * FROM context_fetches WHERE trace_id = ? ORDER BY id",
                (trace_id,),
            ).fetchall()
        events = [dict(row) for row in fetches]
        results = self.results_for([e["id"] for e in events])
        for event in events:
            event["results"] = results.get(event["id"], [])
        return events

    def retrievals_of(self, entity_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Which queries returned this document, most recent first.

        The question asked when a document keeps turning up where it should not:
        not "what did this query return" but "what is retrieving this".
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT r.rank, r.score, r.keyword_score, r.semantic_score,
                       r.match_type, f.id AS fetch_id, f.fetched_at, f.source,
                       f.operation, f.query, f.trace_id
                FROM retrieval_results r
                JOIN context_fetches f ON f.id = r.fetch_id
                WHERE r.entity_id = ?
                ORDER BY r.fetch_id DESC LIMIT ?
                """,
                (entity_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM context_fetches ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def summary(self) -> dict[str, Any]:
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) FROM context_fetches").fetchone()[0]
            successful = self._conn.execute(
                "SELECT COUNT(*) FROM context_fetches WHERE success = 1"
            ).fetchone()[0]
            zero_results = self._conn.execute(
                "SELECT COUNT(*) FROM context_fetches WHERE operation = 'search' "
                "AND success = 1 AND result_count = 0"
            ).fetchone()[0]
            avg_ms = self._conn.execute(
                "SELECT COALESCE(AVG(duration_ms), 0) FROM context_fetches"
            ).fetchone()[0]
            source_rows = self._conn.execute(
                "SELECT source, COUNT(*) AS n FROM context_fetches "
                "GROUP BY source ORDER BY n DESC"
            ).fetchall()
            operation_rows = self._conn.execute(
                "SELECT operation, COUNT(*) AS n FROM context_fetches "
                "GROUP BY operation ORDER BY n DESC"
            ).fetchall()
            context_rows = self._conn.execute(
                """
                SELECT COALESCE(NULLIF(query, ''), entity_id, operation) AS context,
                       COUNT(*) AS n
                FROM context_fetches
                GROUP BY context ORDER BY n DESC LIMIT 20
                """
            ).fetchall()
        return {
            "available": True,
            "total_fetches": total,
            "successful_fetches": successful,
            "failed_fetches": total - successful,
            "zero_result_searches": zero_results,
            "average_duration_ms": round(float(avg_ms), 1),
            "by_source": {str(row["source"]): row["n"] for row in source_rows},
            "by_operation": {
                str(row["operation"]): row["n"] for row in operation_rows
            },
            "by_context": {str(row["context"]): row["n"] for row in context_rows},
        }


class NullAnalyticsStore:
    """No-op fallback when the user's local state directory is not writable.

    Mirrors the real store's surface exactly. Anything missing here becomes an
    AttributeError on a machine where analytics happen to be unavailable — a
    failure in the path whose entire purpose is to not fail.
    """

    path = None

    def record(self, **_: Any) -> Optional[int]:
        return None

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        return []

    def by_trace(self, trace_id: str) -> list[dict[str, Any]]:
        return []

    def retrievals_of(self, entity_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return []

    def results_for(self, fetch_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
        return {}

    def summary(self) -> dict[str, Any]:
        return {
            "available": False,
            "total_fetches": 0,
            "successful_fetches": 0,
            "failed_fetches": 0,
            "zero_result_searches": 0,
            "average_duration_ms": 0.0,
            "by_source": {},
            "by_operation": {},
            "by_context": {},
        }
