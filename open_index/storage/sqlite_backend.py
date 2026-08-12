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
import logging
import sqlite3
import struct
import threading
from pathlib import Path
from typing import Any, Optional

from open_index.embeddings import cosine_similarity
from open_index.models import Entity, Relationship
from open_index.schema import DocType
from open_index.storage.base import (
    NO_EMBEDDING_PROVIDER_WARNING,
    SEARCH_MODES,
    SearchResults,
    _match_info,
    iter_semantic_entities,
    resolve_filters,
    semantic_doc_types,
    semantic_fields_in_scope,
    semantic_text_for,
)

logger = logging.getLogger("open_index.storage.sqlite")

# How many FTS candidates to re-rank with the weighted score before truncating.
_CANDIDATE_POOL = 500

# Brute-force semantic scan ceiling. SQLite has no native vector index yet; past
# ~10k entities the per-query cosine scan becomes noticeable. Use sqlite-vec or
# a vector backend for larger brains.
_MAX_SEMANTIC_SCAN = 10_000

# Reserved top-level keys in an entity's flat JSON; everything else is a field.
# `embedding` is reserved so user schema fields can never collide with the vector
# payload stored in the OpenSearch doc / SQLite entity_embeddings table.
_RESERVED_KEYS = {"id", "doc_type", "name", "related_to", "embedding"}


def _fields_of(data: dict) -> dict:
    """Extract schema field values from an entity's flat JSON."""
    return {k: v for k, v in data.items() if k not in _RESERVED_KEYS}


class SQLiteBackend:
    def __init__(self, db_path: str | Path, config=None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the explorer caches one Brain (and so one
        # connection) while Starlette runs its sync endpoints on a threadpool, so
        # the connection is reached from several threads. Access is serialized
        # through _lock, which is what actually keeps it safe.
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.Lock()
        self._doc_types: dict[str, DocType] = {}
        self._config = config
        self._embedding_provider = None  # constructed lazily on first semantic need
        self._embedding_provider_initialized = False
        self._warned_no_provider = False
        self._warned_dim_mismatch = False
        self._create_tables()

    def _get_embedding_provider(self):
        """Return the configured provider, constructing it lazily on first use."""
        if self._embedding_provider is None and not self._embedding_provider_initialized and self._config is not None:
            from open_index.embeddings import get_embedding_provider

            self._embedding_provider = get_embedding_provider(self._config)
            self._embedding_provider_initialized = True
        return self._embedding_provider

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

            CREATE TABLE IF NOT EXISTS entity_embeddings (
                entity_id  TEXT PRIMARY KEY,
                model      TEXT NOT NULL,
                vector     BLOB NOT NULL,
                updated_at TEXT
            );
            """
        )
        self._conn.commit()

    def ensure_schema(self, doc_types: dict[str, DocType]) -> None:
        # doc_type schemas drive TF weighting; the SQL schema itself is static.
        self._doc_types = dict(doc_types)
        self._backfill_embeddings_if_needed()

    # -- semantic helpers ------------------------------------------------------

    def _store_embedding(self, entity: Entity) -> None:
        # Check for embeddable text FIRST: doc_types without semantic fields
        # must not pay for lazy provider construction (model load) on writes.
        # (semantic_text_for returns "" when the doc_type has no semantic fields.)
        dt = self._doc_types.get(entity.doc_type)
        text = semantic_text_for(entity, dt)
        if not text:
            return
        provider = self._get_embedding_provider()
        if provider is None:
            return
        vector = provider.encode([text])[0]
        blob = struct.pack(f"{len(vector)}f", *vector)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO entity_embeddings (entity_id, model, vector, updated_at)
                VALUES (?, ?, ?, datetime('now'))
                ON CONFLICT(entity_id) DO UPDATE SET
                    model = excluded.model,
                    vector = excluded.vector,
                    updated_at = excluded.updated_at
                """,
                (entity.id, provider.name, blob),
            )
            self._conn.commit()

    def _load_embeddings(self, entity_ids: list[str], dim: int) -> dict[str, list[float]]:
        provider = self._get_embedding_provider()
        if not entity_ids or provider is None:
            return {}
        placeholders = ",".join("?" * len(entity_ids))
        rows = self._conn.execute(
            f"SELECT entity_id, model, vector FROM entity_embeddings WHERE entity_id IN ({placeholders})",
            entity_ids,
        ).fetchall()
        out: dict[str, list[float]] = {}
        expected = dim * 4
        for r in rows:
            blob = r["vector"]
            if len(blob) != expected:
                if not self._warned_dim_mismatch:
                    logger.warning(
                        "Stored embedding dimension for %s does not match provider dimension "
                        "(%d vs %d). Run `open-index index --reembed` to rebuild embeddings.",
                        r["entity_id"],
                        len(blob) // 4,
                        dim,
                    )
                    self._warned_dim_mismatch = True
                continue
            out[r["entity_id"]] = list(struct.unpack(f"{dim}f", blob))
        return out

    def _backfill_embeddings_if_needed(self) -> None:
        semantic_types = semantic_doc_types(self._doc_types)
        if not semantic_types:
            return
        provider = self._get_embedding_provider()
        if provider is None:
            return
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) FROM entities WHERE doc_type IN ({','.join('?' * len(semantic_types))})",
                list(semantic_types),
            ).fetchone()[0]
            emb_count = self._conn.execute(
                f"SELECT COUNT(*) FROM entity_embeddings WHERE entity_id IN "
                f"(SELECT id FROM entities WHERE doc_type IN ({','.join('?' * len(semantic_types))}))",
                list(semantic_types),
            ).fetchone()[0]
        if total == emb_count:
            return
        # O(n) one-time backfill scoped to semantic doc_types.
        for entity in iter_semantic_entities(self._doc_types, self.all_entities()):
            self._store_embedding(entity)

    def _warn_no_embedding_provider(self) -> None:
        if not self._warned_no_provider:
            logger.warning(NO_EMBEDDING_PROVIDER_WARNING)
            self._warned_no_provider = True

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

    def _write_entity_rows(self, cur: sqlite3.Cursor, entity: Entity) -> None:
        """Write one entity's row, FTS row and edges. Caller owns the transaction."""
        data = json.dumps(entity.to_json(), ensure_ascii=False)

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

    def upsert_entity(self, entity: Entity, doc_type: Optional[DocType] = None) -> None:
        if doc_type is not None:
            self._doc_types[doc_type.doc_type] = doc_type

        with self._lock:
            self._write_entity_rows(self._conn.cursor(), entity)
            self._conn.commit()

        # Embeddings are computed outside the lock because encoding may be slow.
        self._store_embedding(entity)

    def upsert_many(self, items: list[tuple[Entity, Optional[DocType]]]) -> None:
        """One transaction and one embedding call for the whole batch.

        The per-entity path commits and encodes each time; on a few hundred rows
        that dominates the import. Embedding models batch well, so the single
        encode call here is the larger of the two savings."""
        if not items:
            return

        for _entity, doc_type in items:
            if doc_type is not None:
                self._doc_types[doc_type.doc_type] = doc_type

        with self._lock:
            cur = self._conn.cursor()
            for entity, _doc_type in items:
                self._write_entity_rows(cur, entity)
            self._conn.commit()

        self._store_embeddings_many([entity for entity, _ in items])

    def _store_embeddings_many(self, entities: list[Entity]) -> None:
        """Encode every embeddable entity in one call, then write the vectors."""
        # Resolve texts first so a batch of non-semantic doc_types never triggers
        # lazy provider construction (which loads a model).
        pending = [
            (entity, text)
            for entity in entities
            if (text := semantic_text_for(entity, self._doc_types.get(entity.doc_type)))
        ]
        if not pending:
            return
        provider = self._get_embedding_provider()
        if provider is None:
            return

        vectors = provider.encode([text for _entity, text in pending])
        rows = [
            (entity.id, provider.name, struct.pack(f"{len(vector)}f", *vector))
            for (entity, _text), vector in zip(pending, vectors)
        ]
        with self._lock:
            self._conn.executemany(
                """
                INSERT INTO entity_embeddings (entity_id, model, vector, updated_at)
                VALUES (?, ?, ?, datetime('now'))
                ON CONFLICT(entity_id) DO UPDATE SET
                    model = excluded.model,
                    vector = excluded.vector,
                    updated_at = excluded.updated_at
                """,
                rows,
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
                cur.execute("DELETE FROM entity_embeddings WHERE entity_id = ?", (r["id"],))
            cur.execute(
                f"DELETE FROM entities WHERE doc_type IN ({placeholders})", doc_types
            )
            self._conn.commit()

    def clear(self) -> None:
        with self._lock:
            self._conn.executescript(
                "DELETE FROM entities; DELETE FROM relationships; DELETE FROM entities_fts; DELETE FROM entity_embeddings;"
            )
            self._conn.commit()

    def reembed(self) -> None:
        if self._get_embedding_provider() is None:
            return
        for entity in iter_semantic_entities(self._doc_types, self.all_entities()):
            self._store_embedding(entity)

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

    @staticmethod
    def _filter_sql(pairs: list[tuple[str, Any]]) -> tuple[str, list[Any]]:
        """SQL for an exact-match filter over schema fields.

        json_extract rather than a column because schema fields live inside the
        stored JSON blob. They sit at its top level, not under a `fields` key —
        the blob is the flat shape `_fields_of` reads back — so the path is
        `$.<name>`. A missing field extracts to NULL and `NULL = ?` is never
        true, so an entity of a doc_type that does not model the field is
        excluded, which is the safe direction for a filter that may be carrying
        a tenant boundary.
        """
        if not pairs:
            return "", []
        sql = "".join(" AND json_extract(e.data, '$.' || ?) = ?" for _ in pairs)
        params: list[Any] = []
        for name, value in pairs:
            params.extend([name, value])
        return sql, params

    def _entities_in_scope(self, doc_types: Optional[list[str]], limit: int,
                           filter_pairs: Optional[list[tuple[str, Any]]] = None
                           ) -> list[sqlite3.Row]:
        """Rows the semantic arm may consider.

        Filters are applied here too, not only on the keyword query: this is the
        set the vector scan ranks, and leaving it unfiltered would let a
        semantic search return exactly the rows a filter was meant to exclude.
        """
        filter_sql, filter_params = self._filter_sql(filter_pairs or [])
        where = ["1=1"]
        params: list[Any] = []
        if doc_types:
            where.append("doc_type IN (%s)" % ",".join("?" * len(doc_types)))
            params.extend(doc_types)
        return self._conn.execute(
            f"SELECT id, doc_type, name, data FROM entities e "
            f"WHERE {' AND '.join(where)}{filter_sql} ORDER BY name LIMIT ?",
            params + filter_params + [limit],
        ).fetchall()

    def search(
        self,
        query: Optional[str] = None,
        doc_types: Optional[list[str]] = None,
        limit: int = 20,
        counts_only: bool = False,
        semantic_weight: Optional[float] = None,
        mode: str = "hybrid",
        filters: Optional[dict[str, Any]] = None,
    ) -> SearchResults:
        if mode not in SEARCH_MODES:
            raise ValueError(f"unknown search mode {mode!r} — expected one of "
                             + ", ".join(SEARCH_MODES))
        w = semantic_weight if semantic_weight is not None else (
            self._config.search.semantic_weight if self._config else 0.3)
        # Raises on an undeclared field rather than filtering nothing.
        filter_pairs = resolve_filters(self._doc_types, filters, doc_types)
        params: list[Any] = []
        where: list[str] = []

        # A keyword search must not run the FTS query in semantic mode: the
        # candidate set is the vector neighbourhood, and a keyword hit that is
        # not near the query has no business being in it.
        match_expr = self._fts_query(query) if (query and mode != "semantic") else ""
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

        # Appended after the doc_type clause so the filter constrains the counts
        # and every candidate query below, not just the rows finally shown.
        filter_sql, filter_params = self._filter_sql(filter_pairs)
        where_sql += filter_sql
        params += filter_params

        # Per-doc_type aggregate counts (always computed — the map's spoke data).
        count_rows = self._conn.execute(
            f"SELECT e.doc_type AS dt, COUNT(*) AS n {base}{where_sql} GROUP BY e.doc_type",
            params,
        ).fetchall()
        doc_type_counts = {r["dt"]: r["n"] for r in count_rows}
        total = sum(doc_type_counts.values())

        if counts_only:
            return SearchResults(total=total, results=[], doc_type_counts=doc_type_counts)

        # In semantic mode the vector arm runs whatever the weight says: the
        # weight blends the two arms in hybrid, and letting a configured 0.0
        # silence an explicitly semantic search would answer a different
        # question than the one asked.
        wants_semantic = mode == "semantic" or (mode == "hybrid" and w > 0)
        semantic_scope = (query and wants_semantic
                          and semantic_fields_in_scope(self._doc_types, doc_types))
        provider = self._get_embedding_provider() if semantic_scope else None
        if semantic_scope and provider is not None:
            # Brute-force semantic scan over entities in scope. This is the SQLite
            # equivalent of a vector search; it is intentionally simple and capped
            # so it stays practical until sqlite-vec is adopted.
            rows = self._entities_in_scope(doc_types, limit=_MAX_SEMANTIC_SCAN,
                                           filter_pairs=filter_pairs)
            query_emb = provider.encode([query])[0]
            query_terms = [
                t for t in "".join(c if c.isalnum() else " " for c in query.lower()).split() if t
            ]
            dim = provider.dim
            emb_map = self._load_embeddings([r["id"] for r in rows], dim)

            # Vectorized cosine for all embedded rows in one numpy pass (a
            # per-row Python loop costs ~10x more at 1k+ entities). Rows
            # without an embedding get sem=0 via the mask.
            sem_by_id: dict[str, float] = {}
            if emb_map:
                import numpy as np

                ids = list(emb_map)
                mat = np.array([emb_map[i] for i in ids], dtype=np.float32)
                qv = np.array(query_emb, dtype=np.float32)
                qn = np.linalg.norm(qv)
                mn = np.linalg.norm(mat, axis=1)
                valid = (qn > 0) & (mn > 0)
                cos = np.zeros(len(ids), dtype=np.float32)
                if qn > 0:
                    cos[valid] = (mat[valid] @ qv) / (mn[valid] * qn)
                sem_by_id = {i: (float(c) + 1.0) / 2.0 for i, c in zip(ids, cos)}

            scored: list[tuple[float, float, sqlite3.Row, dict]] = []
            kw_max = 0.0
            sem_max = 0.0
            for r in rows:
                data = json.loads(r["data"])
                dt = self._doc_types.get(r["doc_type"])
                kw = self._score_entity(query_terms, dt, r["name"], _fields_of(data))
                sem = sem_by_id.get(r["id"], 0.0)
                kw_max = max(kw_max, kw)
                sem_max = max(sem_max, sem)
                scored.append((kw, sem, r, data))

            # Candidate set mirrors the OpenSearch hybrid contract: keyword
            # matches UNION the top-K nearest by cosine (K = max(limit, 50)),
            # so `total`/`doc_type_counts` mean the same thing on both backends.
            # ((cos+1)/2 is > 0 for virtually every vector, so raw "sem > 0"
            # would count every embedded entity as a match.)
            # Membership is tracked, not inferred from the scores: cosine is
            # positive for very nearly every vector, so "sem > 0" would call the
            # whole index a semantic match. A document is a semantic match when
            # it is among the K nearest — that is what the arm actually
            # retrieved.
            K = max(limit, 50)
            candidates: dict[str, tuple[float, float, sqlite3.Row, dict]] = {}
            kw_hits: set[str] = set()
            sem_hits: set[str] = set()

            if mode in ("hybrid", "keyword"):
                for kw, sem, r, data in scored:
                    if kw > 0:
                        candidates[r["id"]] = (kw, sem, r, data)
                        kw_hits.add(r["id"])
            if mode in ("hybrid", "semantic"):
                for kw, sem, r, data in sorted(scored, key=lambda s: -s[1])[:K]:
                    if sem > 0:
                        candidates.setdefault(r["id"], (kw, sem, r, data))
                        sem_hits.add(r["id"])

            final = []
            for kw, sem, r, data in candidates.values():
                kw_norm = kw / kw_max if kw_max else 0.0
                # (cos+1)/2 is already bounded to [0, 1]; keep it absolute so a
                # weak best-match doesn't inflate the semantic arm.
                sem_norm = sem if sem_max else 0.0
                if mode == "keyword":
                    score = kw_norm
                elif mode == "semantic":
                    score = sem_norm
                else:
                    score = (1 - w) * kw_norm + w * sem_norm
                final.append((score, kw_norm, sem_norm, r, data))
            # Highest combined score first; stable by name for ties.
            final.sort(key=lambda s: (-s[0], s[3]["name"]))
            picked = final[:limit]
            results = [
                {"id": r["id"], "doc_type": r["doc_type"], "name": r["name"],
                 "score": round(score, 3), "entity": data,
                 "match": _match_info(r["id"] in kw_hits, r["id"] in sem_hits,
                                      kw_norm, sem_norm)}
                for (score, kw_norm, sem_norm, r, data) in picked
            ]
            doc_type_counts = {}
            for _kw, _sem, r, _data in candidates.values():
                doc_type_counts[r["doc_type"]] = doc_type_counts.get(r["doc_type"], 0) + 1
            total = len(candidates)
            return SearchResults(
                total=total,
                results=results,
                doc_type_counts=doc_type_counts,
                limited=total > limit,
            )
        elif semantic_scope:
            self._warn_no_embedding_provider()

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
            top = scored[0][0] if scored else 0.0
            picked = scored[:limit]
            results = [
                {"id": r["id"], "doc_type": r["doc_type"], "name": r["name"],
                 "score": round(score, 3), "entity": data,
                 "match": _match_info(True, False, score / top if top else 0.0, 0.0)}
                for (score, r, data) in picked
            ]
        else:
            rows = self._conn.execute(
                f"SELECT e.id, e.doc_type, e.name, e.data {base}{where_sql} "
                f"ORDER BY e.name LIMIT ?",
                params + [limit],
            ).fetchall()
            # Nothing was ranked here — this is a listing, or a pure filter.
            # Saying so beats implying a relevance match that never happened.
            results = [
                {"id": r["id"], "doc_type": r["doc_type"], "name": r["name"],
                 "score": 0.0, "entity": json.loads(r["data"]),
                 "match": _match_info(False, False, 0.0, 0.0,
                                      filtered=bool(filter_pairs))}
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
