"""Embedded storage for Droid Brain.

Each brain is a single local SQLite file (with FTS5 full-text search) stored
under ``~/.droid_brains/``. There is no search server to run: relevance ranking
uses SQLite FTS5's ``bm25()`` with per-column weights (field boosters),
multiplied by a per-doc_type boost (type boosters).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")

# FTS5 column weights for bm25(name, doc_type, body): field boosters.
# Matches on an entity's name count 10x, its doc_type 3x, its content 1x.
FIELD_WEIGHTS = (10.0, 3.0, 1.0)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS brain_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS doc_types (
    name TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    boost REAL NOT NULL DEFAULT 1.0,
    schema_json TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY,
    doc_type TEXT NOT NULL REFERENCES doc_types(name),
    name TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (doc_type, name)
);
CREATE VIRTUAL TABLE IF NOT EXISTS entities_fts USING fts5(name, doc_type, body);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate_name(kind: str, name: str) -> None:
    if not name or not NAME_RE.match(name):
        raise ValueError(
            f"Invalid {kind} name {name!r}: use 1-64 chars of letters, digits, '-' or '_', starting with a letter or digit."
        )


def brains_dir() -> Path:
    return Path(os.environ.get("DROID_BRAIN_HOME", "~/.droid_brains")).expanduser()


def brain_path(name: str) -> Path:
    _validate_name("brain", name)
    return brains_dir() / f"{name}.db"


def brain_exists(name: str) -> bool:
    return brain_path(name).is_file()


def list_brains() -> list[dict[str, Any]]:
    """All brains, most recently used first."""
    d = brains_dir()
    if not d.is_dir():
        return []
    brains = [
        {"name": p.stem, "path": str(p), "updated_at": p.stat().st_mtime}
        for p in d.glob("*.db")
    ]
    return sorted(brains, key=lambda b: b["updated_at"], reverse=True)


def most_recent_brain() -> str | None:
    brains = list_brains()
    return brains[0]["name"] if brains else None


def create_brain(name: str, description: str = "") -> "Brain":
    _validate_name("brain", name)
    if brain_exists(name):
        raise ValueError(f"Brain {name!r} already exists at {brain_path(name)}")
    brain = Brain(name)
    brain.set_meta("description", description)
    return brain


def open_brain(name: str) -> "Brain":
    if not brain_exists(name):
        raise ValueError(f"Brain {name!r} does not exist. Create it with: droid-brain new {name}")
    return Brain(name)


def _flatten(value: Any) -> str:
    """Flatten a JSON value (keys included) into searchable text."""
    parts: list[str] = []

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            for k, val in v.items():
                parts.append(str(k))
                walk(val)
        elif isinstance(v, list):
            for item in v:
                walk(item)
        elif v is not None:
            parts.append(str(v))

    walk(value)
    return " ".join(parts)


def _tokens(query: str) -> list[str]:
    """Extract searchable tokens from a query."""
    return re.findall(r"[\w.-]+", query)


def schema_field_paths(schema: dict[str, Any] | None, prefix: str = "") -> list[str]:
    """Dotted paths of a doc_type schema's fields: ['team', 'spec.replicas', ...].

    Nested objects recurse via ``properties``; arrays recurse into ``items``.
    """
    if not isinstance(schema, dict):
        return []
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return []
    paths = []
    for key, subschema in properties.items():
        path = f"{prefix}{key}"
        paths.append(path)
        if isinstance(subschema, dict):
            if subschema.get("type") == "array":
                paths.extend(schema_field_paths(subschema.get("items"), f"{path}[]."))
            else:
                paths.extend(schema_field_paths(subschema, f"{path}."))
    return paths


def entity_template(schema: dict[str, Any] | None) -> dict[str, Any]:
    """Skeleton entity data built from a doc_type schema (empty values per type)."""
    template: dict[str, Any] = {}
    for key, subschema in (schema or {}).get("properties", {}).items():
        field_type = subschema.get("type") if isinstance(subschema, dict) else None
        if field_type == "object":
            template[key] = entity_template(subschema)
        elif field_type == "array":
            template[key] = []
        elif field_type in ("number", "integer"):
            template[key] = 0
        elif field_type == "boolean":
            template[key] = False
        elif field_type == "string" or field_type is None:
            template[key] = ""
        else:
            template[key] = None
    return template


def _validate_schema(schema: Any) -> None:
    """Shape-check a doc_type schema at creation time (nested JSON, JSON-Schema-ish)."""
    if not isinstance(schema, dict):
        raise ValueError("schema must be a JSON object")
    if "properties" in schema and not isinstance(schema["properties"], dict):
        raise ValueError("schema 'properties' must be a JSON object")
    if "required" in schema and not isinstance(schema["required"], list):
        raise ValueError("schema 'required' must be a JSON list of field names")
    try:
        json.dumps(schema)
    except TypeError:
        raise ValueError("schema must be JSON serializable") from None


def _schema(schema_json: str) -> dict[str, Any] | None:
    """Parse a stored schema; a corrupted row degrades to no schema instead of breaking the brain."""
    if not schema_json:
        return None
    try:
        return json.loads(schema_json)
    except json.JSONDecodeError:
        return None


def _fts_query(user_query: str) -> str:
    """Build a safe FTS5 MATCH expression: ANDed prefix-quoted tokens."""
    return " ".join(f'"{t}"*' for t in _tokens(user_query))


class Brain:
    """A single brain: doc_types + entities in one SQLite file."""

    def __init__(self, name: str):
        self.name = name
        self.path = brain_path(name)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # timeout: wait briefly instead of failing on "database is locked";
        # check_same_thread: the Streamlit UI shares one connection across reruns;
        # WAL: UI, MCP server and CLI can use the same brain file concurrently.
        self.conn = sqlite3.connect(str(self.path), timeout=5.0, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Bring brains created by older versions up to date."""
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(doc_types)")}
        if "schema_json" not in columns:
            try:
                self.conn.execute("ALTER TABLE doc_types ADD COLUMN schema_json TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError:
                pass  # another connection migrated it concurrently

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Brain":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---- metadata ----

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO brain_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM brain_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    # ---- doc types ----

    def create_doc_type(
        self,
        name: str,
        description: str = "",
        boost: float = 1.0,
        schema: dict[str, Any] | None = None,
    ) -> None:
        """Create a doc_type. ``schema`` is an optional nested JSON object
        (conventionally JSON-Schema-ish: ``{"properties": {...}, "required": [...]}``)
        describing the structure of its entities."""
        _validate_name("doc_type", name)
        if boost <= 0:
            raise ValueError("boost must be > 0")
        if schema is not None:
            _validate_schema(schema)
        schema_json = json.dumps(schema) if schema else ""
        try:
            self.conn.execute(
                "INSERT INTO doc_types (name, description, boost, schema_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (name, description, float(boost), schema_json, _now()),
            )
        except sqlite3.IntegrityError:
            self.conn.rollback()
            raise ValueError(f"doc_type {name!r} already exists") from None
        self.conn.commit()

    def get_doc_type(self, name: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT name, description, boost, schema_json FROM doc_types WHERE name = ?", (name,)
        ).fetchone()
        if not row:
            return None
        return {
            "name": row["name"],
            "description": row["description"],
            "boost": row["boost"],
            "schema": _schema(row["schema_json"]),
        }

    def list_doc_types(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT dt.name, dt.description, dt.boost, dt.schema_json, COUNT(e.id) AS entities "
            "FROM doc_types dt LEFT JOIN entities e ON e.doc_type = dt.name "
            "GROUP BY dt.name ORDER BY dt.name"
        ).fetchall()
        return [
            {
                "name": row["name"],
                "description": row["description"],
                "boost": row["boost"],
                "schema": _schema(row["schema_json"]),
                "entities": row["entities"],
            }
            for row in rows
        ]

    def doc_type_exists(self, name: str) -> bool:
        return (
            self.conn.execute("SELECT 1 FROM doc_types WHERE name = ?", (name,)).fetchone()
            is not None
        )

    # ---- entities ----

    def upsert_entity(self, doc_type: str, name: str, data: dict[str, Any]) -> int:
        dt = self.get_doc_type(doc_type)
        if dt is None:
            raise ValueError(f"doc_type {doc_type!r} does not exist in brain {self.name!r}")
        name = name.strip()
        if not name:
            raise ValueError("entity name cannot be empty")
        if not isinstance(data, dict):
            raise ValueError("entity data must be a JSON object")
        required = (dt["schema"] or {}).get("required") or []
        if not isinstance(required, list):  # hand-edited/corrupt row
            required = []
        missing = [key for key in required if key not in data]
        if missing:
            raise ValueError(f"entity is missing required field(s) from the {doc_type!r} schema: {', '.join(missing)}")
        try:
            payload = json.dumps(data)
        except TypeError:
            raise ValueError("entity data must be JSON serializable") from None
        now = _now()
        try:
            # Atomic upsert: safe for concurrent writers (UI + MCP + CLI).
            self.conn.execute(
                "INSERT INTO entities (doc_type, name, data, created_at, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(doc_type, name) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
                (doc_type, name, payload, now, now),
            )
            entity_id = self.conn.execute(
                "SELECT id FROM entities WHERE doc_type = ? AND name = ?", (doc_type, name)
            ).fetchone()["id"]
            self.conn.execute("DELETE FROM entities_fts WHERE rowid = ?", (entity_id,))
            self.conn.execute(
                "INSERT INTO entities_fts (rowid, name, doc_type, body) VALUES (?, ?, ?, ?)",
                (entity_id, name, doc_type, _flatten(data)),
            )
        except sqlite3.Error:
            self.conn.rollback()
            raise
        self.conn.commit()
        return entity_id

    def delete_entity(self, entity_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM entities WHERE id = ?", (entity_id,))
        self.conn.execute("DELETE FROM entities_fts WHERE rowid = ?", (entity_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def get_entity(self, doc_type: str, name: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM entities WHERE doc_type = ? AND name = ?", (doc_type, name)
        ).fetchone()
        return self._entity_dict(row) if row else None

    def list_entities(self, doc_type: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT * FROM entities"
        params: list[Any] = []
        if doc_type:
            sql += " WHERE doc_type = ?"
            params.append(doc_type)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        return [self._entity_dict(r) for r in self.conn.execute(sql, params).fetchall()]

    # ---- search ----

    def search(
        self, query: str, doc_type: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Full-text search with field + type boosters.

        Field boosters come from FTS5 bm25 column weights (name > doc_type >
        body); type boosters multiply the bm25 rank by the doc_type's boost
        (ranks are negative, lower is better, so a higher boost ranks higher).
        bm25 scores converge to ~0 on small brains for common terms, so ties
        are broken deterministically: entities whose name contains the query
        terms first, then most recently updated. An empty query lists the most
        recently updated entities.
        """
        match = _fts_query(query)
        if not match:
            return self.list_entities(doc_type=doc_type, limit=limit)
        tokens = _tokens(query.lower())
        name_affinity = " + ".join("(instr(lower(e.name), ?) > 0)" for _ in tokens)
        sql = (
            "SELECT e.id, e.doc_type, e.name, e.data, e.created_at, e.updated_at, "
            f"bm25(entities_fts, {', '.join(str(w) for w in FIELD_WEIGHTS)}) * dt.boost AS rank "
            "FROM entities_fts "
            "JOIN entities e ON e.id = entities_fts.rowid "
            "JOIN doc_types dt ON dt.name = e.doc_type "
            "WHERE entities_fts MATCH ?"
        )
        params: list[Any] = [match]
        if doc_type:
            sql += " AND e.doc_type = ?"
            params.append(doc_type)
        sql += f" ORDER BY rank, ({name_affinity}) DESC, e.updated_at DESC LIMIT ?"
        params.extend(tokens)
        params.append(limit)
        results = []
        for row in self.conn.execute(sql, params).fetchall():
            entity = self._entity_dict(row)
            entity["score"] = round(-row["rank"], 6)  # positive, higher = better
            results.append(entity)
        return results

    # ---- structure ----

    def structure_text(self) -> str:
        """Textual explanation of the brain for agents (MCP tool 1)."""
        doc_types = self.list_doc_types()
        total = sum(dt["entities"] for dt in doc_types)
        lines = [
            f"Brain: {self.name}",
        ]
        description = self.get_meta("description")
        if description:
            lines.append(f"Description: {description}")
        lines.append(f"doc_types: {len(doc_types)}, entities: {total}")
        lines.append("")
        for dt in doc_types:
            examples = ", ".join(
                e["name"] for e in self.list_entities(doc_type=dt["name"], limit=3)
            )
            lines.append(
                f"- {dt['name']} (boost {dt['boost']:g}): {dt['entities']} entities. "
                f"{dt['description'] or 'No description.'}"
                + (f" Schema fields: {', '.join(schema_field_paths(dt['schema']))}." if dt["schema"] else "")
                + (f" Examples: {examples}" if examples else "")
            )
        return "\n".join(lines)

    # ---- internals ----

    @staticmethod
    def _entity_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "doc_type": row["doc_type"],
            "name": row["name"],
            "data": json.loads(row["data"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
