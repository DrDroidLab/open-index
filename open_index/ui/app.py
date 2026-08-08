"""Streamlit explorer for a brain.

Launched via `open-index ui`; the brain directory arrives in OPEN_INDEX_DIR.

The UI is for **inspecting** a brain, not editing one — writes go through the
agent (MCP) or the CLI so every change is validated and lands in the source of
truth. Accordingly there are three tabs, and the two questions a newcomer
actually has are answered without clicking anything:

    "what's in here?"   the sidebar always lists every doc_type with its count
    "show me the map"   the map auto-anchors on the most-connected entities

Both used to require finding the right tab and then making a selection before
anything appeared, which read as an empty or broken screen.
"""

from __future__ import annotations

import os

import streamlit as st

from open_index.brain import Brain
from open_index.graph import ContextGraph, build_graph
from open_index.ui import view

st.set_page_config(page_title="Open Index", page_icon="🧠", layout="wide")


@st.cache_resource
def _open_brain(brain_dir: str) -> Brain:
    return Brain.open(brain_dir)


# Buttons styled as full-width list rows, so results and neighbours read as a
# list rather than a wall of chrome.
ROW_CSS = """
<style>
div[data-testid='stButton'] > button{
  width:100%; text-align:left; justify-content:flex-start;
  border:1px solid #ececec; border-radius:6px; background:#fff;
  padding:7px 12px; font-weight:400; font-size:0.92rem; margin-bottom:-1px;
}
div[data-testid='stButton'] > button:hover{background:#f5f6f8;border-color:#dcdcdc;color:inherit}
div[data-testid='stButton'] > button:focus{box-shadow:none;color:inherit}
</style>
"""


def _dot(color: str) -> str:
    """An inline colored dot matching a doc_type's map color."""
    return (f"<span style='display:inline-block;width:9px;height:9px;"
            f"border-radius:50%;background:{color};margin-right:6px'></span>")


def _esc(text) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# --------------------------------------------------------------------------- #
# Sidebar — the brain at a glance. Always visible, on every tab.
# --------------------------------------------------------------------------- #

def render_sidebar(brain: Brain) -> dict:
    """Identity, structure and settings. Returns the chosen search options."""
    summary = view.summarize(brain)

    with st.sidebar:
        st.markdown(f"### 🧠 {_esc(summary.name)}")
        if summary.description:
            st.caption(summary.description)
        st.caption(f"**{summary.total_entities:,}** entities · "
                   f"**{len(summary.doc_types)}** doc_types")

        st.divider()
        st.markdown("**Doc types**")
        if not summary.has_schema:
            st.caption("None yet — define one with `open-index add-doc-type`, "
                       "or ask your agent.")
        for row in summary.doc_types:
            st.markdown(
                f"{_dot(row.color)} `{_esc(row.name)}` &nbsp; "
                f"<span style='opacity:.6'>{row.count:,} · {row.storage}</span>",
                unsafe_allow_html=True,
            )

        st.divider()
        with st.expander("Search settings"):
            mode = st.radio(
                "Mode", list(view.SEARCH_MODES), horizontal=True,
                help=("Hybrid: keyword matches dominate, semantic similarity "
                      "rescues differently-worded queries. Keyword: text only. "
                      "Semantic: embedding similarity only."),
            )
            if mode == "Semantic":
                from open_index.embeddings import embedding_provider_available

                if not embedding_provider_available():
                    st.warning("No embedding provider — results stay keyword-only. "
                               "Install `open-index[semantic]`.")

        with st.expander("Connect an agent"):
            st.caption("Editing happens through your agent or the CLI, never here — "
                       "so every write is validated.")
            st.code(f"open-index mcp-config --brain {os.environ.get('OPEN_INDEX_DIR', '.')}",
                    language="bash")
            st.caption("Paste the output into `.mcp.json`, then ask the agent to "
                       "read `navigation_guidelines` and add what you need.")

    return {"semantic_weight": view.semantic_weight_for(mode)}


# --------------------------------------------------------------------------- #
# Explore — search and browse in one place (they were duplicate screens).
# --------------------------------------------------------------------------- #

def _goto(entity_id) -> None:
    st.session_state["open_entity"] = entity_id
    st.rerun()


def render_explore(brain: Brain, options: dict) -> None:
    summary = view.summarize(brain)

    if summary.is_empty:
        st.info("This brain has no entities yet.")
        st.caption("Add some with `open-index import <file>`, a connector, or by "
                   "asking your agent — then run `open-index index`.")
        return

    query = st.text_input(
        "Search", label_visibility="collapsed",
        placeholder="Search the brain…  e.g. payment, checkout, redis",
    )
    type_filter = st.multiselect(
        "Limit to doc_types", [r.name for r in summary.doc_types],
        label_visibility="collapsed", placeholder="All doc types",
    )

    if st.session_state.get("open_entity"):
        render_entity(brain, st.session_state["open_entity"])
        return

    if query:
        _render_results(brain, query, type_filter or None, options)
    else:
        _render_browse(brain, summary, type_filter or None)


def _render_results(brain: Brain, query: str, doc_types, options: dict) -> None:
    try:
        results = brain.search(query=query, doc_types=doc_types, limit=50,
                               semantic_weight=options["semantic_weight"])
    except Exception as exc:  # a backend that's down shouldn't blank the page
        st.error(f"Search failed: {exc}")
        return

    st.caption(f"{results.total} result(s) — click one to open")
    if not results.results:
        st.info("No matches. Try fewer words, or switch to Semantic mode in the sidebar.")
        return
    for r in results.results:
        color = view.color_for(brain, r["doc_type"])
        if st.button(f"{r['name']}  ·  {r['doc_type']}  ·  {r['id']}",
                     key=f"res_{r['id']}"):
            _goto(r["id"])


def _render_browse(brain: Brain, summary: view.BrainSummary, doc_types) -> None:
    """No query: list entities by type so the brain is never a blank page."""
    shown = [r for r in summary.doc_types
             if r.count and (not doc_types or r.name in doc_types)]
    if not shown:
        st.info("No entities in the selected doc_types.")
        return

    for row in shown:
        with st.expander(f"{row.name} · {row.count:,}",
                         expanded=len(shown) == 1):
            if row.description:
                st.caption(row.description)
            for entity in brain.backend.all_entities([row.name])[:200]:
                description = entity.fields.get("description", "")
                label = entity.name + (f"  ·  {description[:70]}" if description else "")
                if st.button(label, key=f"br_{entity.id}"):
                    _goto(entity.id)
            if row.count > 200:
                st.caption(f"showing the first 200 of {row.count:,} — search to narrow.")


def render_entity(brain: Brain, entity_id: str) -> None:
    """One entity: its fields, its attribution, and both directions of its edges."""
    if st.button("← back", key="entity_back"):
        _goto(None)

    entity = brain.get_entity(entity_id)
    if entity is None:
        st.warning(f"No entity `{entity_id}`.")
        return

    st.markdown(
        f"{_dot(view.color_for(brain, entity.doc_type))} **{_esc(entity.name)}** "
        f"&nbsp;<span style='opacity:.6'>`{_esc(entity.id)}`</span>",
        unsafe_allow_html=True,
    )

    rows = view.field_rows(entity)
    if rows:
        st.table(rows)

    provenance = view.provenance_row(entity)
    if provenance:
        with st.expander("Provenance"):
            st.table([provenance])
    if entity.valid_from or entity.valid_to:
        st.caption(f"valid: {entity.valid_from or '—'} → {entity.valid_to or 'now'}")

    links = view.neighbours(brain, entity_id)
    st.markdown(f"**Relationships** ({len(links)})")
    if not links:
        st.caption("None yet. Edges are what make this a graph — ask your agent to "
                   "link this entity to related ones.")
        return
    for i, link in enumerate(links):
        suffix = "" if link.exists else "  ·  (missing)"
        if st.button(f"{link.label}  ·  {link.other_id}{suffix}",
                     key=f"nb_{i}_{link.other_id}"):
            _goto(link.other_id)


# --------------------------------------------------------------------------- #
# Map — draws immediately, no selection required.
# --------------------------------------------------------------------------- #

def render_map(brain: Brain) -> None:
    summary = view.summarize(brain)
    populated = [r.name for r in summary.doc_types if r.count]
    if not populated:
        st.info("No entities to map yet. Add some and run `open-index index`.")
        return

    chosen_types = st.multiselect(
        "Doc types to include", populated, default=populated,
        help="Narrow the map to the concepts you care about.",
    )
    scope = chosen_types or populated

    # Auto-anchor rather than waiting for a selection. The old UI drew nothing
    # until you picked entities, which looked like a broken canvas.
    auto = view.default_anchors(brain, scope)
    catalog = {f"{e.name}  ({e.id})": e.id
               for e in brain.backend.all_entities(scope)}
    auto_labels = [label for label, eid in catalog.items() if eid in auto]

    picked = st.multiselect(
        "Anchors", list(catalog), default=auto_labels,
        help="Starting points. Click any node in the map to expand it.",
    )
    anchors = [catalog[label] for label in picked]

    expanded = st.session_state.get("expanded", set())
    if expanded:
        cols = st.columns([4, 1])
        cols[0].caption(f"expanded: {', '.join(sorted(expanded))}")
        if cols[1].button("↺ reset"):
            st.session_state["expanded"] = set()
            st.rerun()

    anchors = list(dict.fromkeys(anchors + list(expanded)))
    if not anchors:
        st.info("Select at least one anchor above.")
        return

    graph = build_graph(brain, anchors, depth=1)
    st.caption(f"{len(graph.nodes)} nodes · {len(graph.edges)} edges — "
               "click a node to expand its relationships")
    render_graph(brain, graph)

    # streamlit-agraph doesn't paint on its first render inside a tab (the canvas
    # mounts with zero size). One forced rerun remounts it with the tab active.
    if not st.session_state.get("_map_primed"):
        st.session_state["_map_primed"] = True
        st.rerun()


def render_graph(brain: Brain, graph: ContextGraph) -> None:
    try:
        from streamlit_agraph import Config, Edge, Node, agraph
    except ImportError:
        st.warning("Install the map renderer: pip install 'open-index[ui]'")
        st.write({"nodes": [n.__dict__ for n in graph.nodes],
                  "edges": [e.__dict__ for e in graph.edges]})
        return

    nodes = [
        Node(id=n.id, label=n.label, color=n.color,
             size=22 if n.is_anchor else 16, shape="dot",
             title=f"{n.doc_type} · {n.id}")
        for n in graph.nodes
    ]
    edges = [Edge(source=e.source, target=e.target, label=e.meaning)
             for e in graph.edges]
    config = Config(
        width="100%", height=650, directed=True,
        # Physics keeps large graphs drifting (and the canvas can stay blank
        # while hundreds of nodes settle) — freeze the layout past ~150 nodes.
        physics=len(graph.nodes) <= 150,
        hierarchical=False, collapsible=False,
    )
    clicked = agraph(nodes=nodes, edges=edges, config=config)
    if clicked and clicked not in st.session_state.get("expanded", set()):
        st.session_state.setdefault("expanded", set()).add(clicked)
        st.rerun()


# --------------------------------------------------------------------------- #
# Jobs — connectors and their schedules.
# --------------------------------------------------------------------------- #

def render_jobs(brain: Brain) -> None:
    from open_index.connectors.runner import discover_connectors
    from open_index.scheduling import RunState

    st.caption("Ingestion scripts in `connectors/*.py` that pull entities from an "
               "MCP server on a schedule.")
    found = discover_connectors(brain)
    if not found:
        st.info("No connectors yet.")
        st.caption("Add `connectors/*.py` to pull entities from an MCP server — "
                   "see docs/deployment.md.")
        return

    import inspect

    state = RunState(brain.config.root) if brain.config.root else None
    for name, cls in sorted(found.items()):
        meta = (state._data.get(name, {}) if state else {})
        with st.container(border=True):
            st.markdown(f"**⚙️ {name}** · schedule `{cls.schedule}`")
            cols = st.columns(3)
            cols[0].metric("source", "live MCP" if cls.mcp_url else "offline/demo")
            cols[1].metric("last run", (meta.get("last_run") or "never")[:19])
            cols[2].metric("entities", meta.get("last_count", "—"))
            st.caption(f"endpoint: `{cls.mcp_url or 'offline/demo'}` · "
                       f"last status: {meta.get('last_status', '—')}")
            with st.expander("view script"):
                try:
                    st.code(inspect.getsource(cls), language="python")
                except (OSError, TypeError):
                    st.caption("(source unavailable)")
            st.caption(f"Run now: `open-index ingest {name}` · "
                       "`open-index run` for everything due.")


def main() -> None:
    brain = _open_brain(os.environ.get("OPEN_INDEX_DIR", "."))
    st.markdown(ROW_CSS, unsafe_allow_html=True)

    options = render_sidebar(brain)
    tab_explore, tab_map, tab_jobs = st.tabs(["Explore", "Map", "Jobs"])
    with tab_explore:
        render_explore(brain, options)
    with tab_map:
        render_map(brain)
    with tab_jobs:
        render_jobs(brain)


main()
