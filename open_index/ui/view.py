"""View-model for the explorer: everything the UI shows, minus the rendering.

Kept free of Streamlit so the decisions that actually matter — which doc_types
to list, what to anchor the map on when the user hasn't chosen, how to describe
an entity's neighbours — are plain functions that can be tested. `app.py` is
then a thin layer of widgets over these.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from open_index.brain import Brain

DEFAULT_COLOR = "#6b7280"

# How many entities the map anchors on when the user hasn't picked any. Small on
# purpose: a map that opens with every entity is an unreadable hairball and slow
# to settle, which reads as "broken" rather than "busy".
DEFAULT_ANCHOR_COUNT = 3


@dataclass
class DocTypeRow:
    """One doc_type as the sidebar/browser shows it."""

    name: str
    count: int
    color: str
    storage: str
    description: str = ""


@dataclass
class NeighbourRow:
    """One edge from an entity, oriented for display."""

    direction: str          # "→" outgoing, "←" incoming
    meaning: str
    other_id: str
    other_name: str
    other_doc_type: str
    color: str
    exists: bool = True

    @property
    def label(self) -> str:
        return f"{self.direction}  {self.meaning or '(related)'}: {self.other_name}"


@dataclass
class BrainSummary:
    """The one-glance answer to 'what is in this brain?'"""

    name: str
    description: str
    total_entities: int
    doc_types: list[DocTypeRow] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return self.total_entities == 0

    @property
    def has_schema(self) -> bool:
        return bool(self.doc_types)


def color_for(brain: Brain, doc_type: str) -> str:
    dt = brain.config.doc_type(doc_type)
    return dt.display.color if dt else DEFAULT_COLOR


def summarize(brain: Brain) -> BrainSummary:
    """Counts and schema for every doc_type, busiest first.

    Ordering by count puts the types that actually hold data at the top, so a
    brain with twenty declared concepts and two populated ones still opens on
    something useful.
    """
    counts = brain.counts()
    rows = [
        DocTypeRow(
            name=name,
            count=counts.get(name, 0),
            color=dt.display.color,
            storage=dt.storage,
            description=dt.description,
        )
        for name, dt in brain.config.doc_types.items()
    ]
    rows.sort(key=lambda r: (-r.count, r.name))
    return BrainSummary(
        name=brain.config.name,
        description=brain.config.description,
        total_entities=sum(counts.values()),
        doc_types=rows,
    )


def edge_counts(brain: Brain, doc_types: Optional[list[str]] = None) -> dict[str, int]:
    """Total degree (in + out) per entity id.

    Used to pick what to show first: the most-connected entity is the most
    informative thing to open a map on, and the least likely to render as a
    lonely dot.
    """
    degree: dict[str, int] = {}
    for entity in brain.backend.all_entities(doc_types):
        degree.setdefault(entity.id, 0)
        for rel in entity.related_to:
            degree[entity.id] = degree.get(entity.id, 0) + 1
            degree[rel.target] = degree.get(rel.target, 0) + 1
    return degree


def default_anchors(
    brain: Brain,
    doc_types: Optional[list[str]] = None,
    limit: int = DEFAULT_ANCHOR_COUNT,
) -> list[str]:
    """Entity ids to anchor the map on when the user hasn't chosen any.

    Picks the most-connected entities, so the map opens on something with edges
    instead of an empty canvas — the previous behaviour, which read as broken.
    Falls back to any entities at all when nothing is connected yet.
    """
    entities = brain.backend.all_entities(doc_types)
    if not entities:
        return []

    degree = edge_counts(brain, doc_types)
    known = {e.id for e in entities}
    ranked = sorted(
        entities,
        key=lambda e: (-degree.get(e.id, 0), e.id),
    )
    anchors = [e.id for e in ranked[:limit] if e.id in known]
    return anchors


def neighbours(brain: Brain, entity_id: str) -> list[NeighbourRow]:
    """Both directions of an entity's edges, resolved for display.

    Incoming edges are included because "what points at this?" is usually the
    more interesting question, and an entity's own JSON can't answer it.
    """
    rows: list[NeighbourRow] = []
    pairs = [("→", t, m) for _s, t, m in brain.backend.relationships_from(entity_id)]
    pairs += [("←", s, m) for s, _t, m in brain.backend.relationships_to(entity_id)]

    for direction, other_id, meaning in pairs:
        other = brain.get_entity(other_id)
        doc_type = other.doc_type if other else other_id.split(":", 1)[0]
        rows.append(NeighbourRow(
            direction=direction,
            meaning=meaning,
            other_id=other_id,
            # A dangling edge still shows, labelled by its slug — hiding it would
            # make a broken reference invisible.
            other_name=other.name if other else other_id.split(":", 1)[-1],
            other_doc_type=doc_type,
            color=color_for(brain, doc_type),
            exists=other is not None,
        ))
    return rows


def field_rows(entity) -> list[dict[str, Any]]:
    """An entity's schema fields as table rows, skipping empties."""
    return [
        {"field": key, "value": str(value)}
        for key, value in entity.fields.items()
        if value not in (None, "")
    ]


def provenance_row(entity) -> Optional[dict[str, Any]]:
    """Attribution for display, or None when the claim is unattributed."""
    prov = getattr(entity, "provenance", None)
    if prov is None or prov.is_empty():
        return None
    return {
        "asserted_by": prov.asserted_by or "—",
        "asserted_at": prov.asserted_at or "—",
        "confidence": "—" if prov.confidence is None else f"{prov.confidence:.2f}",
        "evidence": prov.evidence or "—",
    }


# Search modes offered in the UI, mapped to a semantic_weight override.
SEARCH_MODES = {
    "Hybrid": None,     # the brain's configured blend
    "Keyword": 0.0,
    "Semantic": 1.0,
}


def semantic_weight_for(mode: str) -> Optional[float]:
    return SEARCH_MODES.get(mode)
