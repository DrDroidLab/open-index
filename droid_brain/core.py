"""Core DroidBrain client — SQLite backend (default, zero-setup).

For OpenSearch, set DROID_BRAIN_BACKEND=opensearch and install opensearch-py.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from droid_brain.models import Brain, BrainStructure, DocType, Entity, SchemaField

DEFAULT_DB_PATH = os.environ.get(
    "DROID_BRAIN_DB_PATH",
    str(Path.home() / ".droid_brain" / "brains.db"),
)


class DroidBrain:
    """Client for creating/querying Droid Brains backed by SQLite + FTS5."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._ensure_tables()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_tables(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS brains (
                name TEXT PRIMARY KEY,
                description TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS doctypes (
                brain_name TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                schema_fields TEXT DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (brain_name, name),
                FOREIGN KEY (brain_name) REFERENCES brains(name) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS entities (
                entity_id TEXT PRIMARY KEY,
                brain_name TEXT NOT NULL,
                doc_type TEXT NOT NULL,
                data TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (brain_name) REFERENCES brains(name) ON DELETE CASCADE
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS entities_fts USING fts5(
                brain_name,
                doc_type,
                data,
                content='entities',
                content_rowid='rowid'
            );
        """)
        # Triggers to keep FTS in sync
        self._conn.executescript("""
            CREATE TRIGGER IF NOT EXISTS entities_ai AFTER INSERT ON entities BEGIN
                INSERT INTO entities_fts(rowid, brain_name, doc_type, data)
                VALUES (new.rowid, new.brain_name, new.doc_type, new.data);
            END;

            CREATE TRIGGER IF NOT EXISTS entities_ad AFTER DELETE ON entities BEGIN
                INSERT INTO entities_fts(entities_fts, rowid, brain_name, doc_type, data)
                VALUES ('delete', old.rowid, old.brain_name, old.doc_type, old.data);
            END;

            CREATE TRIGGER IF NOT EXISTS entities_au AFTER UPDATE ON entities BEGIN
                INSERT INTO entities_fts(entities_fts, rowid, brain_name, doc_type, data)
                VALUES ('delete', old.rowid, old.brain_name, old.doc_type, old.data);
                INSERT INTO entities_fts(rowid, brain_name, doc_type, data)
                VALUES (new.rowid, new.brain_name, new.doc_type, new.data);
            END;
        """)
        self._conn.commit()

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------
    # Brain management
    # ------------------------------------------------------------------

    def create_brain(self, name: str, description: str = "") -> dict:
        now = self._now()
        self._conn.execute(
            "INSERT INTO brains (name, description, created_at) VALUES (?, ?, ?)",
            (name, description, now),
        )
        self._conn.commit()
        return {"name": name, "description": description, "created_at": now}

    def list_brains(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM brains ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    def delete_brain(self, name: str) -> None:
        cur = self._conn.execute("DELETE FROM brains WHERE name = ?", (name,))
        if cur.rowcount == 0:
            raise ValueError(f"Brain '{name}' does not exist.")
        # CASCADE deletes doctypes and entities. FTS triggers handle cleanup.
        self._conn.commit()

    # ------------------------------------------------------------------
    # DocType management
    # ------------------------------------------------------------------

    def create_doctype(
        self,
        brain_name: str,
        name: str,
        description: str = "",
        fields: Optional[list[dict]] = None,
    ) -> dict:
        fields = fields or []
        schema_fields = [
            SchemaField(**f) if isinstance(f, dict) else f for f in fields
        ]
        now = self._now()
        dt = DocType(
            name=name,
            description=description,
            schema_fields=schema_fields,
            created_at=now,
            updated_at=now,
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO doctypes (brain_name, name, description, schema_fields, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (brain_name, name, description, json.dumps([f.model_dump() for f in schema_fields]), now, now),
        )
        self._conn.commit()
        return dt.model_dump()

    def get_doctype(self, brain_name: str, name: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM doctypes WHERE brain_name = ? AND name = ?",
            (brain_name, name),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["schema_fields"] = json.loads(d["schema_fields"])
        return d

    def list_doctypes(self, brain_name: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM doctypes WHERE brain_name = ? ORDER BY name",
            (brain_name,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["schema_fields"] = json.loads(d["schema_fields"])
            result.append(d)
        return result

    # ------------------------------------------------------------------
    # Entity management
    # ------------------------------------------------------------------

    def create_entity(
        self, brain_name: str, doc_type: str, data: dict[str, Any]
    ) -> dict:
        entity_id = str(uuid.uuid4())
        now = self._now()
        data_json = json.dumps(data)
        self._conn.execute(
            "INSERT INTO entities (entity_id, brain_name, doc_type, data, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (entity_id, brain_name, doc_type, data_json, now, now),
        )
        self._conn.commit()
        return {
            "entity_id": entity_id,
            "doc_type": doc_type,
            "data": data,
            "created_at": now,
            "updated_at": now,
        }

    def get_entity(self, brain_name: str, entity_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM entities WHERE brain_name = ? AND entity_id = ?",
            (brain_name, entity_id),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["data"] = json.loads(d["data"])
        return d

    def update_entity(
        self, brain_name: str, entity_id: str, data: dict[str, Any]
    ) -> Optional[dict]:
        existing = self.get_entity(brain_name, entity_id)
        if not existing:
            return None
        now = self._now()
        self._conn.execute(
            "UPDATE entities SET data = ?, updated_at = ? WHERE entity_id = ?",
            (json.dumps(data), now, entity_id),
        )
        self._conn.commit()
        existing["data"] = data
        existing["updated_at"] = now
        return existing

    def delete_entity(self, brain_name: str, entity_id: str) -> bool:
        cur = self._conn.execute(
            "DELETE FROM entities WHERE brain_name = ? AND entity_id = ?",
            (brain_name, entity_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def list_entities(
        self, brain_name: str, doc_type: Optional[str] = None, size: int = 50
    ) -> list[dict]:
        if doc_type:
            rows = self._conn.execute(
                "SELECT * FROM entities WHERE brain_name = ? AND doc_type = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (brain_name, doc_type, size),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM entities WHERE brain_name = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (brain_name, size),
            ).fetchall()
        return [self._row_to_entity(r) for r in rows]

    def _row_to_entity(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        d["data"] = json.loads(d["data"])
        return d

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self,
        brain_name: str,
        query_text: str,
        doc_type: Optional[str] = None,
        size: int = 20,
    ) -> list[dict]:
        raw_query = self._escape_fts(query_text)
        if doc_type:
            rows = self._conn.execute(
                "SELECT e.* FROM entities e "
                "JOIN entities_fts fts ON e.rowid = fts.rowid "
                "WHERE entities_fts MATCH ? AND e.brain_name = ? AND e.doc_type = ? "
                "ORDER BY rank LIMIT ?",
                (raw_query, brain_name, doc_type, size),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT e.* FROM entities e "
                "JOIN entities_fts fts ON e.rowid = fts.rowid "
                "WHERE entities_fts MATCH ? AND e.brain_name = ? "
                "ORDER BY rank LIMIT ?",
                (raw_query, brain_name, size),
            ).fetchall()
        return [self._row_to_entity(r) for r in rows]

    @staticmethod
    def _escape_fts(query: str) -> str:
        """Build a safe FTS5 query from raw user input."""
        # Split into tokens, quote each, join with implicit AND
        tokens = query.strip().split()
        if not tokens:
            return '""'
        return " ".join(f'"{t}"' for t in tokens)

    # ------------------------------------------------------------------
    # Brain structure (for MCP tool)
    # ------------------------------------------------------------------

    def get_brain_structure(self, brain_name: str) -> BrainStructure:
        doctypes = self.list_doctypes(brain_name)

        # Counts per doc_type
        counts = {}
        rows = self._conn.execute(
            "SELECT doc_type, COUNT(*) as cnt FROM entities WHERE brain_name = ? GROUP BY doc_type",
            (brain_name,),
        ).fetchall()
        for r in rows:
            counts[r["doc_type"]] = r["cnt"]
        total = sum(counts.values())

        dt_stats: list[dict] = []
        for dt in doctypes:
            dt_name = dt["name"]
            dt_count = counts.get(dt_name, 0)

            examples: list[dict] = []
            if dt_count > 0:
                ex_rows = self._conn.execute(
                    "SELECT data FROM entities WHERE brain_name = ? AND doc_type = ? LIMIT 2",
                    (brain_name, dt_name),
                ).fetchall()
                examples = [json.loads(r["data"]) for r in ex_rows]

            dt_stats.append(
                {
                    "name": dt_name,
                    "description": dt.get("description", ""),
                    "schema_fields": dt.get("schema_fields", []),
                    "entity_count": dt_count,
                    "examples": examples,
                }
            )

        return BrainStructure(
            brain_name=brain_name,
            doc_types=dt_stats,
            total_entities=total,
        )


def _droid_brain_factory(**kwargs: Any) -> DroidBrain:
    """Factory that respects DROID_BRAIN_BACKEND env var."""
    backend = os.environ.get("DROID_BRAIN_BACKEND", "sqlite")
    if backend == "opensearch":
        from droid_brain.opensearch_backend import DroidBrain as OSDroidBrain

        return OSDroidBrain(**kwargs)
    return DroidBrain(**kwargs)
