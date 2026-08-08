"""The storage + search seam.

`SearchBackend` is the one interface a storage engine must implement. The SQLite
backend is the zero-friction default; an OpenSearch backend can be dropped in
behind the same contract for anyone who wants reference-grade search.

The `search(..., counts_only=...)` contract mirrors the reference
`search_resources`: with `counts_only=True` you get only per-doc_type aggregate
counts (cheap — no documents materialized), which is what the map uses to draw
spoke nodes before pulling full entities on expand.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Protocol, runtime_checkable

from open_index.models import Entity
from open_index.schema import DocType


NO_EMBEDDING_PROVIDER_WARNING = (
    "Semantic fields are declared but no embedding provider is available. "
    "Install 'open-index[semantic]' or set OPEN_INDEX_EMBEDDING_* env vars. "
    "Falling back to keyword-only search."
)


def semantic_doc_types(doc_types: dict[str, DocType]) -> set[str]:
    """Return the doc_type names that have at least one `search: semantic` field."""
    return {
        dt.doc_type
        for dt in doc_types.values()
        if any(f.search == "semantic" for f in dt.fields)
    }


def semantic_fields_in_scope(
    doc_types: dict[str, DocType], scope: Optional[list[str]] = None
) -> bool:
    """Whether any doc_type in the requested scope has a `search: semantic` field."""
    types = (
        [doc_types[t] for t in scope if t in doc_types]
        if scope
        else list(doc_types.values())
    )
    return any(f.search == "semantic" for dt in types for f in dt.fields)


def semantic_text_for(entity: Entity, doc_type: Optional[DocType]) -> str:
    """Text to embed: name + concatenated `search: semantic` field values."""
    if doc_type is None:
        return entity.name
    semantic_fields = [f for f in doc_type.fields if f.search == "semantic"]
    if not semantic_fields:
        return ""
    parts = [entity.name]
    for f in semantic_fields:
        value = entity.fields.get(f.name)
        if value not in (None, ""):
            parts.append(str(value))
    return " ".join(parts)


def iter_semantic_entities(
    doc_types: dict[str, DocType], entities: Iterable[Entity]
) -> Iterable[Entity]:
    """Yield entities whose doc_type has a semantic field."""
    semantic_types = semantic_doc_types(doc_types)
    for entity in entities:
        if entity.doc_type in semantic_types:
            yield entity


@dataclass
class SearchResults:
    total: int
    results: list[dict[str, Any]] = field(default_factory=list)
    # Per-doc_type counts, always populated (the aggregation the map spokes use).
    doc_type_counts: dict[str, int] = field(default_factory=dict)
    limited: bool = False


@runtime_checkable
class SearchBackend(Protocol):
    """Persist entities + their relationships, and search over them."""

    def ensure_schema(self, doc_types: dict[str, DocType]) -> None:
        """Prepare storage (create tables/indices) given the doc_type schemas."""
        ...

    def upsert_entity(self, entity: Entity, doc_type: Optional[DocType] = None) -> None:
        """Insert or replace one entity and its relationships."""
        ...

    def upsert_many(self, items: list[tuple[Entity, Optional[DocType]]]) -> None:
        """Insert or replace many entities in one pass.

        Exists because the per-entity path pays a fixed cost each time — a commit
        on SQLite, an index refresh on OpenSearch, and one embedding call per
        entity — which makes a 500-row import hundreds of times slower than it
        needs to be. Implementations should batch all three.

        Not atomic: a backend may apply some rows and fail on others. Callers
        that need to report per-entity outcomes should validate first."""
        ...

    def get_entity(self, entity_id: str) -> Optional[Entity]:
        ...

    def all_entities(self, doc_types: Optional[list[str]] = None) -> list[Entity]:
        ...

    def relationships_from(self, entity_id: str) -> list[tuple[str, str, str]]:
        """Return (source_id, target_id, meaning) edges originating at entity_id."""
        ...

    def relationships_to(self, entity_id: str) -> list[tuple[str, str, str]]:
        """Return (source_id, target_id, meaning) edges pointing at entity_id."""
        ...

    def search(
        self,
        query: Optional[str] = None,
        doc_types: Optional[list[str]] = None,
        limit: int = 20,
        counts_only: bool = False,
        semantic_weight: Optional[float] = None,
    ) -> SearchResults:
        """Search entities. `semantic_weight` overrides the config's
        search.semantic_weight for this call: 0.0 = keyword-only,
        1.0 = semantic-only, None = the brain's configured default."""
        ...

    def counts(self) -> dict[str, int]:
        """Total entity count per doc_type."""
        ...

    def delete_by_doc_type(self, doc_types: list[str]) -> None:
        """Remove all entities (and their edges) of the given doc_types.

        Used to reconcile file-backed types on `index` without touching
        index-backed types that live only in the DB."""
        ...

    def clear(self) -> None:
        """Remove all entities (used by a full reindex)."""
        ...

    def reembed(self) -> None:
        """Recompute embeddings for every stored entity.

        Used after enabling semantic search on an existing brain or switching
        embedding models."""
        ...
