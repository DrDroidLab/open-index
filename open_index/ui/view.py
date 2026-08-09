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


# -- map rendering ------------------------------------------------------------

# The canvas width handed to streamlit-agraph. It MUST be an int: the library
# does `f"{width}px"`, so a CSS string like "100%" becomes the invalid value
# "100%px", the canvas fails to size, and the graph is stranded in a corner
# instead of centred.
# Sized to sit beside the legend column rather than to fill the page: at the
# old full-page width the canvas overflowed its column once the legend arrived.
GRAPH_WIDTH = 950
GRAPH_HEIGHT = 650

# Past this many nodes a force layout keeps drifting, so we slow it down rather
# than switching physics off — vis only auto-fits the viewport as part of
# stabilisation, and without stabilisation the graph never centres.
BUSY_GRAPH_NODES = 150

_GRAPH_THEMES = {
    "dark": {
        # vis defaults to near-black labels with a white halo, which on a dark
        # canvas is both unreadable and visually noisy.
        "node_label": "#e8eaed",
        "edge_label": "#aab2bd",
        "edge": "#6b7280",
        "stroke_width": 0,
    },
    "light": {
        "node_label": "#1f2328",
        "edge_label": "#57606a",
        "edge": "#c8ccd2",
        "stroke_width": 0,
    },
}


# Buttons styled as full-width list rows, so results and neighbours read as a
# list rather than a wall of chrome.
#
# Every value is theme-agnostic on purpose. Hardcoding `background:#fff` painted
# white rows under Streamlit's dark theme, which keeps its light text — white on
# white, and the entity list became invisible. So: transparent background,
# inherited text colour, and translucent grey borders that read correctly
# against either a light or a dark surface.
ROW_CSS = """
<style>
div[data-testid='stButton'] > button{
  width:100%; text-align:left; justify-content:flex-start;
  border:1px solid rgba(128,128,128,0.35); border-radius:6px;
  background:transparent; color:inherit;
  padding:7px 12px; font-weight:400; font-size:0.92rem; margin-bottom:-1px;
}
div[data-testid='stButton'] > button:hover{
  background:rgba(128,128,128,0.12);
  border-color:rgba(128,128,128,0.6);
  color:inherit;
}
div[data-testid='stButton'] > button:focus{box-shadow:none;color:inherit}
/* Streamlit wraps button labels in <p>; without this the label keeps its own
   colour and ignores the inherit above. */
div[data-testid='stButton'] > button p{color:inherit;margin:0}
</style>
"""


def mcp_client_config(url: str, server_name: str = "open-index") -> str:
    """The JSON block to paste into an agent, as displayed in the How to use tab."""
    import json

    from open_index.mcp_config import normalize_mcp_url

    return json.dumps(
        {"mcpServers": {server_name: {"type": "http", "url": normalize_mcp_url(url)}}},
        indent=2,
    )


# What each tool does, in the order an agent would reach for them. Kept here
# rather than in the page so the list can be asserted against the tools the MCP
# server actually registers — a stale tool list in the docs is worse than none.
READ_TOOLS = [
    ("navigation_guidelines()",
     "The whole guide to this index: every doc_type, its fields, the "
     "relationship vocabulary in use, and worked examples. Injected into the "
     "MCP handshake, so an agent starts oriented."),
    ("search_brain(query, doc_types, limit)",
     "Free-text search, hybrid keyword + semantic. `doc_types` narrows it."),
    ("get_entity(entity_id)",
     "One entity with its outgoing AND incoming edges — the incoming direction "
     "answers \"what else points at this?\"."),
]

WRITE_TOOLS = [
    ("put_entity(doc_type, id, name, fields, related_to)",
     "Add or update one entity. Upsert — the same id replaces it."),
    ("put_entities([...])",
     "A whole batch in one call, with optional shared provenance. Use this "
     "rather than calling put_entity in a loop."),
    ("create_doc_type(doc_type, description, fields, relationships, storage)",
     "Define a new concept, when no existing doc_type fits."),
]

# The tab label for the help page. Leftmost, so it is what a first-time visitor
# lands on, and a bare question mark so it reads as help rather than content.
HELP_TAB = "?"

# What each tab is for, in the order they appear. This list *is* the tab order —
# the page builds its tabs from it, so the two cannot drift apart.
TAB_GUIDE = [
    (HELP_TAB,
     "This page: how to connect an agent, what tools it gets, and what "
     "everything else here does."),
    ("Schema",
     "Every doc_type in this index and the shape of it — each field's type, how "
     "it is searched, its ranking weight, and the relationship vocabulary that "
     "connects types to each other. Read this before writing to the index."),
    ("Explore",
     "Search the index, or browse by doc_type. Open an entity to see its fields, "
     "its attribution, and every relationship in both directions — click through "
     "to walk the graph."),
    ("Map",
     "The same relationships drawn. It auto-anchors on the most-connected "
     "entities so it shows something immediately; click any node to expand it, "
     "and narrow by doc_type to cut the noise."),
    ("Analytics",
     "What has been asked of this index, by which client (CLI, agent, this UI) "
     "and how often. Zero-result searches are the interesting number: they are "
     "the questions this index cannot yet answer."),
    ("Jobs",
     "Connectors that pull entities in on a schedule, with their last run."),
]


def schema_field_rows(doc_type) -> list[dict]:
    """One row per field, as the Schema tab tabulates them.

    `search` and `boost` are the two that change behaviour rather than just
    describing it, so they are spelled out rather than shown as raw enum values.
    """
    searchable = {
        "syntactic": "keyword match",
        "semantic": "meaning (vector)",
        "none": "not searched",
    }
    rows = []
    for f in doc_type.fields:
        rows.append({
            "field": f.name,
            "type": f.type,
            "searched by": searchable.get(f.search, f.search),
            "weight": f"{f.boost:g}×" if f.search != "none" else "—",
            "required": "yes" if f.required else "",
            "notes": f.description or "",
        })
    return rows


def schema_relationship_rows(brain, doc_type_name: str) -> list[dict]:
    """Declared and observed edges for one doc_type, merged.

    Declared is the vocabulary the schema intends; observed is what the entities
    actually use. Showing both together is the point — a declared edge with zero
    uses and an undeclared edge in heavy use are both worth noticing.
    """
    doc_type = brain.config.doc_type(doc_type_name)
    observed = brain.observed_relationships(doc_type_name)
    rows = []
    seen = set()

    for spec in (doc_type.relationships if doc_type else []):
        seen.add(spec.name)
        rows.append({
            "relationship": spec.name,
            "points at": spec.target_doc_type or "any",
            "declared": "yes",
            "in use": observed.get(spec.name, 0),
        })
    for meaning, count in sorted(observed.items(), key=lambda kv: -kv[1]):
        if meaning not in seen:
            rows.append({
                "relationship": meaning,
                "points at": "—",
                "declared": "no",
                "in use": count,
            })
    return rows


# Node labels are drawn next to the dot, so a long one overlaps its neighbours
# and the map becomes unreadable. The full text lives in the hover tooltip.
MAX_NODE_LABEL = 22

# Cap on nodes drawn at once. Past this a force layout stops settling and the
# picture stops being readable regardless.
MAX_GRAPH_NODES = 250

# Above this many edges, their labels are hidden: overlapping relationship text
# on every line is the main thing that made the map unreadable.
MAX_LABELLED_EDGES = 60


def truncate_label(text: str, limit: int = MAX_NODE_LABEL) -> str:
    """Shorten a node label, breaking on a word boundary where one is close."""
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)
    head = cut[0] if len(cut) > 1 and len(cut[0]) >= limit - 8 else text[:limit]
    return head.rstrip(" ,;:—-") + "…"


def node_tooltip(entity_id: str, name: str, doc_type: str,
                 fields: Optional[dict] = None) -> str:
    """What hovering a node shows: the full name, its type, and a few fields.

    vis renders this as plain text, so it is newline-separated rather than HTML.
    """
    lines = [name, f"{doc_type} · {entity_id}"]
    for key, value in list((fields or {}).items()):
        if value in (None, "") or key == "name":
            continue
        text = " ".join(str(value).split())
        lines.append(f"{key}: {text[:90]}{'…' if len(text) > 90 else ''}")
        if len(lines) >= 7:   # a tooltip taller than the graph helps nobody
            break
    return "\n".join(lines)


def edge_tooltip(source: str, target: str, meaning: str) -> str:
    return f"{source}\n  —[{meaning or 'related'}]→\n{target}"


def legend_rows(brain, graph) -> list[dict]:
    """Doc types present in a drawn graph, with their colour and node count.

    Built from the graph rather than the schema so it describes what is on
    screen: a doc_type filtered out, or cut by the node cap, should not appear.
    """
    counts: dict[str, int] = {}
    for node in graph.nodes:
        counts[node.doc_type] = counts.get(node.doc_type, 0) + 1
    return [
        {"doc_type": name, "count": counts[name], "color": color_for(brain, name)}
        for name in sorted(counts, key=lambda n: (-counts[n], n))
    ]


def graph_theme(theme_type: Optional[str]) -> dict:
    """Label and edge colours for the map, given Streamlit's active theme.

    Anything unrecognised (including None, which is what Streamlit reports when
    the user is following their browser preference and the server was never
    told) falls back to the light palette.
    """
    return _GRAPH_THEMES.get((theme_type or "").lower(), _GRAPH_THEMES["light"])
