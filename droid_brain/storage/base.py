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
from typing import Any, Optional, Protocol, runtime_checkable

from droid_brain.models import Entity
from droid_brain.schema import DocType


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
    ) -> SearchResults:
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
