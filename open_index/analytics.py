"""Private local usage analytics for context reads.

Usage can contain raw queries, so state lives outside the public brain checkout
under ~/.local/state/open-index rather than beside source-controlled files.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


class AnalyticsStore:
    """Record and aggregate the context that CLI/MCP clients retrieve."""

    def __init__(self, brain_root: Optional[Path]):
        identity = str((brain_root or Path.cwd()).resolve())
        slug = hashlib.sha256(identity.encode()).hexdigest()[:16]
        state_home = Path.home() / ".local" / "state" / "open-index"
        state_home.mkdir(parents=True, exist_ok=True)
        self.path = state_home / f"{slug}.db"
        self._lock = threading.Lock()
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
            """
        )
        self._conn.commit()

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
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO context_fetches (
                    fetched_at, source, operation, query, doc_types, entity_id,
                    result_count, result_doc_types, duration_ms, success, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(), source, operation, query,
                    json.dumps(doc_types or []), entity_id, result_count,
                    json.dumps(result_doc_types or {}), round(duration_ms, 2),
                    int(success), error,
                ),
            )
            self._conn.commit()

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
    """No-op fallback when the user's local state directory is not writable."""

    path = None

    def record(self, **_: Any) -> None:
        return None

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        return []

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
