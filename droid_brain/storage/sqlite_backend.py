"""SQLite + FTS5 search backend — the zero-friction default.

Design notes:
  * `entities` holds the canonical row (JSON blob + a couple of hot columns).
  * `relationships` is a plain edge table (source, target, meaning).
  * `entities_fts` is an FTS5 index with two indexed columns, `name` and
    `search_text`. Per-field `boost` from the schema is honoured two ways:
      - column weighting via bm25(fts, name_w, search_w), and
      - term-frequency weighting: each field's text is repeated round(boost)
        times inside `search_text`, so high-boost/curated fields rank higher —
        the SQLite-native stand-in for OpenSearch's `field^10`.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

from droid_brain.models import Entity, Relationship
from droid_brain.schema import DocType
from droid_brain.storage.base import SearchResults

# How many FTS candidates to re-rank with the weighted score before truncating.
_CANDIDATE_POOL = 500

# Reserved top-level keys in an entity's flat JSON; everything else is a field.
_RESERVED_KEYS = {"id", "doc_type", "name", "related_to"}


def _fields_of(data: dict) -> dict:
    """Extract schema field values from an entity's flat JSON."""
    return {k: v for k, v in data.items() if k not in _RESERVED_KEYS}


class SQLiteBackend:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the Streamlit UI caches one Brain (and thus one
        # connection) but reruns it across worker threads. Access is serialized
        # through _lock so the shared connection stays safe.
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.Lock()
        self._doc_types: dict[str, DocType] = {}
        self._create_tables()

    # -- schema ----------------------------------------------------------------

    def _create_tables(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS entities (
                id         TEXT PRIMARY KEY,
                doc_type   TEXT NOT NULL,
                name       TEXT NOT NULL,
                data       TEXT NOT NULL,
                created_at TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS relationships (
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                meaning   TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (source_id, target_id, meaning)
            );
            CREATE INDEX IF NOT EXISTS idx_rel_source ON relationships(source_id);
            CREATE INDEX IF NOT EXISTS idx_rel_target ON relationships(target_id);

            CREATE VIRTUAL TABLE IF NOT EXISTS entities_fts USING fts5(
                name,
                search_text,
                tokenize = 'unicode61'
            );
            """
        )
        self._conn.commit()

    def ensure_schema(self, doc_types: dict[str, DocType]) -> None:
        # doc_type schemas drive TF weighting; the SQL schema itself is static.
        self._doc_types = dict(doc_types)

    # -- writes ----------------------------------------------------------------

    def _build_search_text(self, entity: Entity) -> str:
        """Concatenate searchable field values for FTS recall.

        FTS is only used to *find* candidates (recall + prefix matching); the
        actual ranking is a true per-field weighted score computed in
        `_score_entity`, so there's no need to distort the index here."""
        dt = self._doc_types.get(entity.doc_type)
        parts: list[str] = []
        for key, value in entity.fields.items():
            if value in (None, ""):
                continue
            if dt is not None:
                spec = dt.field(key)
                if spec is not None and spec.search == "none":
                    continue
            parts.append(str(value))
        return " ".join(parts)

    def _score_entity(self, query_terms: list[str], doc_type: Optional[DocType],
                      name: str, fields: dict[str, Any]) -> float:
        """Genuine per-field weighting: each field contributes
        `field.boost × (query terms it matches)`. A term matches a field if it is
        a substring of, or a prefix of a word in, that field's text. So a hit in a
        `boost: 6` title outweighs a hit in a `boost: 1` description 6-to-1 —
        exactly, not approximately."""
        # Map field name -> value, folding the label field's value (entity.name).
        values = dict(fields)
        label = doc_type.display.label_field if doc_type else "name"
        values.setdefault(label, name)

        specs = {f.name: f for f in doc_type.fields} if doc_type else {}
        score = 0.0
        for fname, value in values.items():
            if value in (None, ""):
                continue
            spec = specs.get(fname)
            if spec is not None and spec.search == "none":
                continue
            boost = spec.boost if spec is not None else 1.0
            text = str(value).lower()
            words = text.split()
            matched = sum(
                1 for t in query_terms
                if t in text or any(w.startswith(t) for w in words)
            )
            score += boost * matched
        return score

    def upsert_entity(self, entity: Entity, doc_type: Optional[DocType] = None) -> None:
        if doc_type is not None:
            self._doc_types[doc_type.doc_type] = doc_type
        data = json.dumps(entity.to_json(), ensure_ascii=False)

        with self._lock:
            cur = self._conn.cursor()

            # Remove any prior FTS row for this entity (keyed by entities.rowid).
            row = cur.execute("SELECT rowid FROM entities WHERE id = ?", (entity.id,)).fetchone()
            if row is not None:
                cur.execute("DELETE FROM entities_fts WHERE rowid = ?", (row["rowid"],))

            cur.execute(
                """
                INSERT INTO entities (id, doc_type, name, data, created_at, updated_at)
                VALUES (?, ?, ?, ?, COALESCE((SELECT created_at FROM entities WHERE id = ?), datetime('now')), datetime('now'))
                ON CONFLICT(id) DO UPDATE SET
                    doc_type = excluded.doc_type,
                    name = excluded.name,
                    data = excluded.data,
                    updated_at = datetime('now')
                """,
                (entity.id, entity.doc_type, entity.name, data, entity.id),
            )
            rowid = cur.execute("SELECT rowid FROM entities WHERE id = ?", (entity.id,)).fetchone()["rowid"]
            cur.execute(
                "INSERT INTO entities_fts (rowid, name, search_text) VALUES (?, ?, ?)",
                (rowid, entity.name, self._build_search_text(entity)),
            )

            # Replace this entity's outgoing edges.
            cur.execute("DELETE FROM relationships WHERE source_id = ?", (entity.id,))
            for rel in entity.related_to:
                cur.execute(
                    "INSERT OR IGNORE INTO relationships (source_id, target_id, meaning) VALUES (?, ?, ?)",
                    (entity.id, rel.target, rel.relationship_edge_meaning),
                )
            self._conn.commit()

    def delete_by_doc_type(self, doc_types: list[str]) -> None:
        if not doc_types:
            return
        placeholders = ",".join("?" * len(doc_types))
        with self._lock:
            cur = self._conn.cursor()
            rows = cur.execute(
                f"SELECT rowid, id FROM entities WHERE doc_type IN ({placeholders})",
                doc_types,
            ).fetchall()
            for r in rows:
                cur.execute("DELETE FROM entities_fts WHERE rowid = ?", (r["rowid"],))
                cur.execute("DELETE FROM relationships WHERE source_id = ?", (r["id"],))
            cur.execute(
                f"DELETE FROM entities WHERE doc_type IN ({placeholders})", doc_types
            )
            self._conn.commit()

    def clear(self) -> None:
        with self._lock:
            self._conn.executescript(
                "DELETE FROM entities; DELETE FROM relationships; DELETE FROM entities_fts;"
            )
            self._conn.commit()

    # -- reads -----------------------------------------------------------------

    def _row_to_entity(self, row: sqlite3.Row) -> Entity:
        data = json.loads(row["data"])
        return Entity.from_dict(data)

    def get_entity(self, entity_id: str) -> Optional[Entity]:
        row = self._conn.execute(
            "SELECT data FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        return self._row_to_entity(row) if row else None

    def all_entities(self, doc_types: Optional[list[str]] = None) -> list[Entity]:
        if doc_types:
            placeholders = ",".join("?" * len(doc_types))
            rows = self._conn.execute(
                f"SELECT data FROM entities WHERE doc_type IN ({placeholders}) ORDER BY name",
                doc_types,
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT data FROM entities ORDER BY name").fetchall()
        return [self._row_to_entity(r) for r in rows]

    def relationships_from(self, entity_id: str) -> list[tuple[str, str, str]]:
        rows = self._conn.execute(
            "SELECT source_id, target_id, meaning FROM relationships WHERE source_id = ?",
            (entity_id,),
        ).fetchall()
        return [(r["source_id"], r["target_id"], r["meaning"]) for r in rows]

    def relationships_to(self, entity_id: str) -> list[tuple[str, str, str]]:
        rows = self._conn.execute(
            "SELECT source_id, target_id, meaning FROM relationships WHERE target_id = ?",
            (entity_id,),
        ).fetchall()
        return [(r["source_id"], r["target_id"], r["meaning"]) for r in rows]

    def counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT doc_type, COUNT(*) AS n FROM entities GROUP BY doc_type"
        ).fetchall()
        return {r["doc_type"]: r["n"] for r in rows}

    # -- search ----------------------------------------------------------------

    @staticmethod
    def _fts_query(query: str) -> str:
        """Turn a free-text query into a permissive FTS5 MATCH expression.

        Each alphanumeric term becomes a prefix term (`term*`) and terms are
        OR'd, so partial and multi-word queries still recall; bm25 ranking then
        floats the best matches (more matched terms → better score)."""
        terms = [t for t in "".join(c if c.isalnum() else " " for c in query).split() if t]
        if not terms:
            return ""
        return " OR ".join(f"{t}*" for t in terms)

    def search(
        self,
        query: Optional[str] = None,
        doc_types: Optional[list[str]] = None,
        limit: int = 20,
        counts_only: bool = False,
    ) -> SearchResults:
        params: list[Any] = []
        where: list[str] = []

        match_expr = self._fts_query(query) if query else ""
        if match_expr:
            base = (
                "FROM entities_fts "
                "JOIN entities e ON e.rowid = entities_fts.rowid "
                "WHERE entities_fts MATCH ? "
            )
            params.append(match_expr)
        else:
            base = "FROM entities e WHERE 1=1 "

        if doc_types:
            where.append("e.doc_type IN (%s)" % ",".join("?" * len(doc_types)))
            params.extend(doc_types)
        where_sql = (" AND " + " AND ".join(where)) if where else ""

        # Per-doc_type aggregate counts (always computed — the map's spoke data).
        count_rows = self._conn.execute(
            f"SELECT e.doc_type AS dt, COUNT(*) AS n {base}{where_sql} GROUP BY e.doc_type",
            params,
        ).fetchall()
        doc_type_counts = {r["dt"]: r["n"] for r in count_rows}
        total = sum(doc_type_counts.values())

        if counts_only:
            return SearchResults(total=total, results=[], doc_type_counts=doc_type_counts)

        if match_expr:
            # FTS finds candidates (recall + prefix); pull a generous pool, then
            # rank by the true per-field weighted score below. Cap the pool so a
            # huge match set stays bounded.
            rows = self._conn.execute(
                f"SELECT e.id, e.doc_type, e.name, e.data {base}{where_sql} "
                f"ORDER BY bm25(entities_fts) LIMIT ?",
                params + [_CANDIDATE_POOL],
            ).fetchall()
            query_terms = [
                t for t in "".join(c if c.isalnum() else " " for c in query.lower()).split() if t
            ]
            scored = []
            for r in rows:
                data = json.loads(r["data"])
                dt = self._doc_types.get(r["doc_type"])
                score = self._score_entity(query_terms, dt, r["name"], _fields_of(data))
                scored.append((score, r, data))
            # Highest weighted score first; stable by name for ties.
            scored.sort(key=lambda s: (-s[0], s[1]["name"]))
            picked = scored[:limit]
            results = [
                {"id": r["id"], "doc_type": r["doc_type"], "name": r["name"],
                 "score": round(score, 3), "entity": data}
                for (score, r, data) in picked
            ]
        else:
            rows = self._conn.execute(
                f"SELECT e.id, e.doc_type, e.name, e.data {base}{where_sql} "
                f"ORDER BY e.name LIMIT ?",
                params + [limit],
            ).fetchall()
            results = [
                {"id": r["id"], "doc_type": r["doc_type"], "name": r["name"],
                 "score": 0.0, "entity": json.loads(r["data"])}
                for r in rows
            ]

        return SearchResults(
            total=total,
            results=results,
            doc_type_counts=doc_type_counts,
            limited=total > limit,
        )

    def close(self) -> None:
        self._conn.close()
