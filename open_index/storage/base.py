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


# The three ways a search can be run. `mode` is not sugar over semantic_weight:
# it decides which candidates exist at all. Weighting alone would let a document
# that matched no keyword into a keyword search at score 0, which is a wrong
# answer rather than a low-ranked one.
SEARCH_MODES = ("hybrid", "keyword", "semantic")


@dataclass
class SearchResults:
    total: int
    results: list[dict[str, Any]] = field(default_factory=list)
    # Per-doc_type counts, always populated (the aggregation the map spokes use).
    doc_type_counts: dict[str, int] = field(default_factory=dict)
    limited: bool = False


# Every result dict carries `match`, so a caller (and anyone debugging an
# agent's memory) can see *why* a document came back rather than only how highly
# it ranked:
#
#   {"type": "keyword" | "semantic" | "both" | "filter" | "none",
#    "keyword_score": float,     # 0.0 when this arm did not match
#    "semantic_score": float}    # cosine rescaled to [0, 1]
#
# "semantic" means the document was among the nearest by vector — NOT that its
# cosine was above zero, which is true of very nearly every embedded document
# and would label the entire index a semantic match.
#
# "filter" and "none" are the honest answers when nothing was ranked at all: a
# pure filter, or a plain listing. Calling those a keyword match at score 0
# would misreport why the document came back, which is the one thing this field
# exists to get right.


def _match_info(keyword: bool, semantic: bool, keyword_score: float,
                semantic_score: float, filtered: bool = False) -> dict[str, Any]:
    """The `match` block for one result."""
    if keyword and semantic:
        kind = "both"
    elif keyword:
        kind = "keyword"
    elif semantic:
        kind = "semantic"
    else:
        kind = "filter" if filtered else "none"
    return {
        "type": kind,
        "keyword_score": round(float(keyword_score), 3),
        "semantic_score": round(float(semantic_score), 3),
    }


def resolve_filters(
    doc_types_map: dict[str, DocType],
    filters: Optional[dict[str, Any]],
    scope: Optional[list[str]] = None,
) -> list[tuple[str, Any]]:
    """Validate a strict filter against the schema, or raise.

    Fails closed on purpose. A filter is the one search input that may be
    carrying a security boundary — a tenant, a user, an account — and the
    dangerous outcome is not an error but a silent one: a typo'd or undeclared
    field that quietly matches nothing, and so filters nothing, and returns the
    whole index.

    A field must be declared `filterable: true` on at least one doc_type in
    scope. Entities of a type that does not carry the field never match, which
    falls out of the backends comparing a missing value: this is deliberate, and
    is why a filter cannot leak across doc_types that do not model it.
    """
    if not filters:
        return []

    in_scope = [dt for name, dt in doc_types_map.items()
                if not scope or name in scope]
    allowed = {
        f.name
        for dt in in_scope
        for f in dt.fields
        if f.filterable
    }

    unknown = [k for k in filters if k not in allowed]
    if unknown:
        known = ", ".join(sorted(allowed)) or "none"
        raise ValueError(
            "cannot filter on " + ", ".join(f"{k!r}" for k in sorted(unknown))
            + f" — filterable fields here are: {known}. "
            "Declare `filterable: true` on the field in its doc_type to filter "
            "by it. (Refusing rather than ignoring: a filter that is silently "
            "dropped returns everything.)"
        )
    return sorted(filters.items())


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
        mode: str = "hybrid",
        filters: Optional[dict[str, Any]] = None,
    ) -> SearchResults:
        """Search entities.

        `mode` selects which candidates exist:
            hybrid    keyword hits UNION the nearest by vector (the default)
            keyword   keyword hits only
            semantic  nearest by vector only

        `semantic_weight` blends the two arms *within* hybrid: 0.0 leans
        keyword, 1.0 leans semantic, None uses the brain's configured default.
        It does not decide membership — that is `mode`, so that a keyword search
        cannot return something which matched no keyword.

        `filters` is an exact-match predicate applied in the query itself, never
        after ranking. Every field in it must be declared `filterable: true`;
        see `resolve_filters`, which raises rather than ignoring an unknown one.

        Each result dict carries `match` — see the note above SearchResults.
        """
        ...

    def counts(self) -> dict[str, int]:
        """Total entity count per doc_type."""
        ...

    def get_by_external_id(self, external_id: str) -> Optional[Entity]:
        """The entity carrying this caller-supplied id, or None.

        `external_id` is free-form and its uniqueness is the caller's business,
        not something the store enforces. Implementations return the single
        match, or the first by id when a caller has reused one — deterministic
        rather than arbitrary, so the same lookup gives the same answer twice.
        """
        ...

    def delete_entity(self, entity_id: str) -> bool:
        """Remove one entity and everything derived from it. True if it existed.

        Implementations must also drop its search-index row, its embedding, and
        every edge naming it in *either* direction. An edge left pointing at a
        deleted entity is a dangling reference that still renders on the map and
        in neighbour lists, which reads as data corruption rather than as a
        deletion.

        Idempotent: deleting an absent id returns False, it does not raise.
        """
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
