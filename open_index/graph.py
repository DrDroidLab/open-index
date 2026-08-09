"""Build the context map around an anchor entity.

Generalizes the reference's anchor→expand model: pick any entity, BFS outward
over relationships to a given depth, and return type-colored nodes + edges
labeled with each `relationship_edge_meaning`. Edges are treated as undirected
for traversal (you want to see both what an anchor points to and what points at
it) but their direction and meaning are preserved on the returned edge.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

from open_index.brain import Brain

_DEFAULT_COLOR = "#6b7280"


@dataclass
class GraphNode:
    id: str
    label: str
    doc_type: str
    color: str
    is_anchor: bool = False
    depth: int = 0
    # Full entity JSON for the UI to show on click.
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class GraphEdge:
    source: str
    target: str
    meaning: str = ""


@dataclass
class ContextGraph:
    anchors: list[str] = field(default_factory=list)
    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)

    @property
    def anchor(self) -> Optional[str]:
        """First anchor — convenience for single-anchor callers."""
        return self.anchors[0] if self.anchors else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "anchors": self.anchors,
            "nodes": [n.__dict__ for n in self.nodes],
            "edges": [e.__dict__ for e in self.edges],
        }


def _color_for(brain: Brain, doc_type: str) -> str:
    dt = brain.config.doc_type(doc_type)
    if dt is not None:
        return dt.display.color
    return _DEFAULT_COLOR


def _label_for(brain: Brain, entity) -> str:
    dt = brain.config.doc_type(entity.doc_type)
    label_field = dt.display.label_field if dt else "name"
    return entity.label_for(label_field)


def build_overview_graph(
    brain: Brain,
    doc_types: Optional[list[str]] = None,
    limit: int = 250,
) -> ContextGraph:
    """Every entity of the given doc_types, with the edges that run between them.

    The anchor-and-expand view answers "what is around this one thing?". This
    answers the other question — "what does this index look like?" — which is
    what you want when you have never seen the index before and have no entity
    in mind to anchor on.

    Only edges whose *both* ends are in scope are drawn: a dangling half-edge to
    a filtered-out doc_type would render as an unexplained line to nowhere.

    `limit` caps the node count, keeping the most-connected entities. A force
    layout with thousands of nodes is neither readable nor quick, and the
    best-connected ones are the ones worth seeing. Callers should report when
    the cap bit — silently showing a subset reads as "this is everything".
    """
    entities = brain.backend.all_entities(doc_types)

    degree: dict[str, int] = {e.id: 0 for e in entities}
    for entity in entities:
        for rel in entity.related_to:
            degree[entity.id] = degree.get(entity.id, 0) + 1
            if rel.target in degree:
                degree[rel.target] = degree.get(rel.target, 0) + 1

    if len(entities) > limit:
        entities = sorted(entities, key=lambda e: (-degree.get(e.id, 0), e.id))[:limit]

    in_scope = {e.id for e in entities}
    graph = ContextGraph(anchors=[])
    for entity in entities:
        graph.nodes.append(GraphNode(
            id=entity.id,
            label=_label_for(brain, entity),
            doc_type=entity.doc_type,
            color=_color_for(brain, entity.doc_type),
            depth=0,
            data=entity.to_json(),
        ))

    seen: set[tuple[str, str, str]] = set()
    for entity in entities:
        for source, target, meaning in brain.backend.relationships_from(entity.id):
            if target not in in_scope:
                continue
            key = (source, target, meaning)
            if key not in seen:
                seen.add(key)
                graph.edges.append(GraphEdge(source, target, meaning))
    return graph


def build_graph(brain: Brain, anchors: str | list[str], depth: int = 1) -> ContextGraph:
    """Build the map around one or more anchor entities.

    Each anchor is placed at the center; the graph fans out over relationships
    (both directions) for `depth` hops. `depth=1` — the default and the primary
    interaction — shows each anchor's directly-related entities; clicking a node
    in the UI adds it to the anchor set, growing the map organically (rather than
    bumping a global hop count)."""
    anchor_ids = [anchors] if isinstance(anchors, str) else list(anchors)
    graph = ContextGraph(anchors=anchor_ids)

    seen_nodes: dict[str, GraphNode] = {}
    seen_edges: set[tuple[str, str, str]] = set()

    def add_node(entity, d: int, is_anchor: bool = False) -> None:
        if entity.id in seen_nodes:
            if is_anchor:  # a later anchor overrides a neighbor placement
                seen_nodes[entity.id].is_anchor = True
            return
        seen_nodes[entity.id] = GraphNode(
            id=entity.id,
            label=_label_for(brain, entity),
            doc_type=entity.doc_type,
            color=_color_for(brain, entity.doc_type),
            is_anchor=is_anchor,
            depth=d,
            data=entity.to_json(),
        )

    def add_placeholder(node_id: str, d: int) -> None:
        """A relationship may point at an id that has no stored entity yet."""
        if node_id in seen_nodes:
            return
        doc_type = node_id.split(":", 1)[0] if ":" in node_id else "unknown"
        seen_nodes[node_id] = GraphNode(
            id=node_id,
            label=node_id.split(":", 1)[-1],
            doc_type=doc_type,
            color=_color_for(brain, doc_type),
            depth=d,
            data={"id": node_id, "doc_type": doc_type, "missing": True},
        )

    queue: deque[tuple[str, int]] = deque()
    for anchor_id in anchor_ids:
        anchor = brain.get_entity(anchor_id)
        if anchor is None:
            continue
        add_node(anchor, 0, is_anchor=True)
        queue.append((anchor_id, 0))

    if not seen_nodes:
        return graph

    while queue:
        current_id, d = queue.popleft()
        if d >= depth:
            continue

        # Outgoing and incoming edges both expand the neighborhood.
        outgoing = brain.backend.relationships_from(current_id)
        incoming = brain.backend.relationships_to(current_id)

        for source_id, target_id, meaning in outgoing + incoming:
            neighbor_id = target_id if source_id == current_id else source_id
            edge_key = (source_id, target_id, meaning)
            if edge_key not in seen_edges:
                seen_edges.add(edge_key)
                graph.edges.append(GraphEdge(source_id, target_id, meaning))

            if neighbor_id not in seen_nodes:
                neighbor = brain.get_entity(neighbor_id)
                if neighbor is not None:
                    add_node(neighbor, d + 1)
                else:
                    add_placeholder(neighbor_id, d + 1)
                queue.append((neighbor_id, d + 1))

    # Drop edges whose endpoints didn't make it into the node set (beyond depth).
    graph.nodes = list(seen_nodes.values())
    node_ids = set(seen_nodes)
    graph.edges = [
        e for e in graph.edges if e.source in node_ids and e.target in node_ids
    ]
    return graph
