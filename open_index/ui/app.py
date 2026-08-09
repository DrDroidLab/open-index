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
from time import perf_counter

import streamlit as st

from open_index.brain import Brain
from open_index.graph import ContextGraph, build_graph, build_overview_graph
from open_index.ui import view

st.set_page_config(page_title="Open Index", page_icon="🧠", layout="wide")


@st.cache_resource
def _open_brain(brain_dir: str) -> Brain:
    return Brain.open(brain_dir)




def _theme_type() -> str:
    """Whether the viewer is in light or dark mode.

    Streamlit reports this per-session, so it follows the browser preference
    even when the server was never configured with a theme. Older versions
    don't expose it at all, hence the guarded lookup.
    """
    try:
        return getattr(st.context.theme, "type", None) or "light"
    except Exception:
        return "light"


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
        # source="ui" so browsing here shows up in the Analytics tab alongside
        # CLI and agent traffic — otherwise the usage picture has a hole in it.
        results = brain.search(query=query, doc_types=doc_types, limit=50,
                               semantic_weight=options["semantic_weight"],
                               source="ui")
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

    entity = brain.get_entity(entity_id, source="ui")
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
    """The whole index at a glance: every entity, coloured by doc_type.

    Anchoring on one entity is the wrong default for someone who has never seen
    the index — they have no entity in mind. This shows the shape of the whole
    thing and lets them subtract from it.
    """
    summary = view.summarize(brain)
    populated = [r.name for r in summary.doc_types if r.count]
    if not populated:
        st.info("No entities to map yet. Add some and run `open-index index`.")
        return

    chosen = st.multiselect(
        "Doc types shown", populated, default=populated,
        help="Uncheck a type to drop it and its edges from the map.",
    )
    scope = chosen or populated

    focus = st.session_state.get("map_focus")
    if focus:
        cols = st.columns([5, 1])
        cols[0].caption(f"Focused on `{focus}` and its immediate neighbours.")
        if cols[1].button("↩ show all"):
            st.session_state["map_focus"] = None
            st.rerun()
        graph = build_graph(brain, [focus], depth=1)
    else:
        graph = build_overview_graph(brain, scope, limit=view.MAX_GRAPH_NODES)

    total = sum(r.count for r in summary.doc_types if r.name in scope)
    if not focus and len(graph.nodes) < total:
        st.warning(
            f"Showing the {len(graph.nodes)} most-connected of {total} entities. "
            "Narrow the doc types above to see the rest."
        )

    canvas, legend = st.columns([4, 1])
    with canvas:
        st.caption(f"{len(graph.nodes)} nodes · {len(graph.edges)} edges — "
                   "hover for detail, click a node to focus on it")
        render_graph(brain, graph)
    with legend:
        st.markdown("**Legend**")
        for row in view.legend_rows(brain, graph):
            st.markdown(
                f"{_dot(row['color'])} `{_esc(row['doc_type'])}`"
                f" <span style='opacity:.6'>{row['count']}</span>",
                unsafe_allow_html=True,
            )
        if graph.edges:
            st.markdown("---")
            st.caption("Edges are `related_to` links. Hover one to see which "
                       "relationship it is.")

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

    palette = view.graph_theme(_theme_type())

    nodes = [Node(**spec) for spec in view.graph_node_specs(graph)]
    edges = [Edge(**spec) for spec in view.graph_edge_specs(graph, palette["edge"])]

    busy = len(graph.nodes) > view.BUSY_GRAPH_NODES
    config = Config(
        # An int, not "100%": the library formats this as f"{width}px", so a CSS
        # string yields "100%px" and the canvas never sizes — which is what left
        # the graph stranded in a corner instead of centred.
        width=view.GRAPH_WIDTH, height=view.GRAPH_HEIGHT, directed=True,
        # Physics stays on even for busy graphs: vis only fits the viewport to
        # the content as part of stabilisation, so disabling it means the map
        # never centres. Slow big graphs down instead of freezing them.
        physics=True, stabilization=True, fit=True,
        maxVelocity=15 if busy else 50,
        hierarchical=False, collapsible=False,
    )
    # The canvas is a fixed pixel width, so on a wide page it would sit flush
    # left. Centre it in the content area rather than letting it hug the edge.
    _left, middle, _right = st.columns([1, 20, 1])
    with middle:
        clicked = agraph(nodes=nodes, edges=edges, config=config)
    if clicked and clicked != st.session_state.get("map_focus"):
        st.session_state["map_focus"] = clicked
        st.rerun()


# --------------------------------------------------------------------------- #
# Jobs — connectors and their schedules.
# --------------------------------------------------------------------------- #

def render_analytics(brain: Brain) -> None:
    """Which context was fetched, by whom, and how often — across CLI, MCP and UI.

    The number worth watching is zero-result searches: those are the questions
    this brain was asked and could not answer, i.e. what to model next.
    """
    summary = brain.analytics_summary()
    if not summary.get("available", True):
        st.warning("Analytics are unavailable — the local state directory is not writable.")
        return

    st.caption(
        "Stored in `~/.local/state/open-index/`, outside the brain checkout. "
        "Search text and entity ids stay on this machine."
    )

    cols = st.columns(4)
    cols[0].metric("fetches", summary["total_fetches"])
    cols[1].metric("failed", summary["failed_fetches"])
    cols[2].metric("zero-result searches", summary["zero_result_searches"])
    cols[3].metric("avg latency", f"{summary['average_duration_ms']:.0f} ms")

    if not summary["total_fetches"]:
        st.info("Nothing recorded yet. Search here, or query the brain from the "
                "CLI or your agent, and the usage shows up in this tab.")
        return

    left, right = st.columns(2)
    with left:
        st.markdown("**By client**")
        st.bar_chart([{"client": k, "fetches": v}
                      for k, v in summary["by_source"].items()],
                     x="client", y="fetches")
    with right:
        st.markdown("**By operation**")
        st.bar_chart([{"operation": k, "fetches": v}
                      for k, v in summary["by_operation"].items()],
                     x="operation", y="fetches")

    st.markdown("**Most requested context**")
    if summary["by_context"]:
        st.dataframe([{"context": c, "fetches": n}
                      for c, n in summary["by_context"].items()],
                     hide_index=True, use_container_width=True)
    else:
        st.caption("No named context fetched yet.")

    with st.expander("Recent fetches"):
        events = brain.analytics_events(limit=100)
        if not events:
            st.caption("No events yet.")
            return
        st.dataframe([{
            "time": e["fetched_at"],
            "client": e["source"],
            "operation": e["operation"],
            "context": e["query"] or e["entity_id"] or "navigation guide",
            "results": e["result_count"],
            "ms": e["duration_ms"],
            "ok": bool(e["success"]),
        } for e in events], hide_index=True, use_container_width=True)


def render_schema(brain: Brain) -> None:
    """Every doc_type and the shape of it — the reference before you write."""
    summary = view.summarize(brain)
    if not summary.has_schema:
        st.info("No doc_types defined yet.")
        st.caption("A doc_type is a concept this index tracks, plus the fields it "
                   "stores. Create one with `open-index add-doc-type`, or ask an "
                   "agent to call `create_doc_type`.")
        return

    st.caption(
        f"{len(summary.doc_types)} doc_types · {summary.total_entities:,} entities. "
        "Every entity id is `<doc_type>:<slug>`, and any entity can link to any "
        "other through `related_to`."
    )

    for row in summary.doc_types:
        doc_type = brain.config.doc_type(row.name)
        noun = "entity" if row.count == 1 else "entities"
        with st.expander(f"{row.name} · {row.count:,} {noun}",
                         expanded=len(summary.doc_types) <= 3):
            if row.description:
                st.markdown(row.description)
            st.caption(
                f"{_dot(row.color)} source of truth: "
                + ("**files** — JSON under `entities/`, git-trackable"
                   if row.storage == "file"
                   else "**search index** — DB-owned, not written to files"),
                unsafe_allow_html=True,
            )

            fields = view.schema_field_rows(doc_type)
            if fields:
                st.markdown("**Fields**")
                st.table(fields)
            else:
                st.caption("No fields declared.")

            relationships = view.schema_relationship_rows(brain, row.name)
            st.markdown("**Relationships (optional)**")
            if relationships:
                st.table(relationships)
                st.caption("Declared edges are validated against their target "
                           "doc_type. Undeclared ones still work — they just "
                           "aren't checked.")
            else:
                st.caption("None declared or in use. Entities of this type are "
                           "valid without any; edges are what make the index "
                           "traversable rather than just searchable.")


def render_how_to_use(brain: Brain) -> None:
    """Connecting an agent, the tools it gets, and what the other tabs are for.

    Deliberately the rightmost tab: it is the page people come back to, not the
    one they start on.
    """
    summary = view.summarize(brain)
    mcp_url = os.environ.get("OPEN_INDEX_PUBLIC_URL", "")
    read_only = os.environ.get("OPEN_INDEX_READ_ONLY", "").lower() in ("1", "true", "yes")

    st.markdown("### What an index holds")
    st.caption(
        "Three ideas, and the whole thing follows from them."
    )
    for term, what in view.MODEL_GUIDE:
        st.markdown(f"- **{term}** — {what}")

    st.divider()
    st.markdown(f"### Connect an agent to `{_esc(summary.name)}`")
    st.caption(
        "This index speaks MCP, so any MCP-capable agent can query it — and, "
        "unless the endpoint is read-only, keep it current."
    )

    if mcp_url:
        st.markdown("**1. Point your agent at this URL**")
        st.code(view.mcp_client_config(mcp_url, server_name=summary.name), language="json")
        st.caption("Paste into `.mcp.json` (Claude Code), `.cursor/mcp.json` (Cursor), "
                   "or any MCP client's server config. Or generate it:")
        st.code(f"open-index mcp-config --url {mcp_url} --name {summary.name} > .mcp.json",
                language="bash")
    else:
        st.info("This explorer isn't configured with a public MCP URL "
                "(`OPEN_INDEX_PUBLIC_URL`), so the connection block can't be shown.")
        st.code("open-index mcp-config --brain <brain-dir> > .mcp.json", language="bash")

    st.markdown("**2. Ask it something**")
    st.caption("No briefing needed — the navigation guide below is injected into the "
               "MCP handshake, so the agent knows this index's doc_types and "
               "relationship vocabulary before its first turn.")

    st.divider()
    st.markdown("### Tools the agent gets")

    st.markdown("**Reading**")
    for name, what in view.READ_TOOLS:
        st.markdown(f"- `{name}` — {what}")

    st.markdown("**Writing**")
    if read_only:
        st.info("This endpoint is **read-only** — the write tools below are not "
                "registered on it. Serve without `--read-only` to enable them.")
    for name, what in view.WRITE_TOOLS:
        st.markdown(f"- `{name}` — {what}")
    st.caption("Entity ids are always `<doc_type>:<slug>`. Writes are validated "
               "against the doc_type schema, and land in the search index (and on "
               "disk, for `storage: file` types).")

    st.divider()
    st.markdown("### What each tab does")
    for name, what in view.TAB_GUIDE:
        st.markdown(f"- **{name}** — {what}")

    st.divider()
    st.markdown("### What's in this index right now")
    st.caption(f"{summary.total_entities:,} entities across "
               f"{len(summary.doc_types)} doc_types.")
    if summary.doc_types:
        st.table([{"doc_type": r.name, "entities": r.count,
                   "source of truth": "files (git)" if r.storage == "file" else "search index",
                   "what it holds": r.description or "—"}
                  for r in summary.doc_types])

    with st.expander("The full navigation guide the agent receives"):
        st.code(brain.navigation_guidelines(include_writes=not read_only),
                language="markdown")


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
    st.markdown(view.ROW_CSS, unsafe_allow_html=True)

    options = render_sidebar(brain)
    # Streamlit opens the first tab, so the help page leftmost means a visitor
    # lands on the explanation rather than having to find it.
    tab_help, tab_schema, tab_explore, tab_map, tab_analytics, tab_jobs = st.tabs(
        [name for name, _ in view.TAB_GUIDE]
    )
    with tab_help:
        render_how_to_use(brain)
    with tab_schema:
        render_schema(brain)
    with tab_explore:
        render_explore(brain, options)
    with tab_map:
        render_map(brain)
    with tab_analytics:
        render_analytics(brain)
    with tab_jobs:
        render_jobs(brain)


main()
