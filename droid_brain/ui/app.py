"""Streamlit explorer for a droid-brain: Structure | Search | Map.

Launched via `droid-brain ui`; the brain directory arrives in DROID_BRAIN_DIR.
The Map tab is the point of the whole thing — pick an anchor entity, choose a
depth, and see the context graph, with click-to-re-anchor for exploration.
"""

from __future__ import annotations

import os

import streamlit as st

from droid_brain.brain import Brain
from droid_brain.graph import ContextGraph, build_graph

st.set_page_config(page_title="Droid Brain", page_icon="🧠", layout="wide")


@st.cache_resource
def _open_brain(brain_dir: str) -> Brain:
    return Brain.open(brain_dir)


def _color(brain: Brain, doc_type: str) -> str:
    dt = brain.config.doc_type(doc_type)
    return dt.display.color if dt else "#6b7280"


def render_graph(brain: Brain, graph: ContextGraph) -> None:
    """Draw the context graph with streamlit-agraph, falling back to a table."""
    try:
        from streamlit_agraph import Config, Edge, Node, agraph
    except ImportError:
        st.warning("Install the map renderer: pip install 'droid-brain[ui]'")
        st.write({"nodes": [n.__dict__ for n in graph.nodes],
                  "edges": [e.__dict__ for e in graph.edges]})
        return

    nodes = [
        Node(
            id=n.id,
            label=n.label,
            color=n.color,
            size=22 if n.is_anchor else 16,
            shape="dot",
            title=f"{n.doc_type} · {n.id}",
        )
        for n in graph.nodes
    ]
    edges = [
        Edge(source=e.source, target=e.target, label=e.meaning)
        for e in graph.edges
    ]
    config = Config(
        width="100%",
        height=650,
        directed=True,
        physics=True,
        hierarchical=False,
        collapsible=False,
    )
    clicked = agraph(nodes=nodes, edges=edges, config=config)
    # Clicking a node expands it: add it to the anchor set and redraw.
    if clicked and clicked not in st.session_state.get("expanded", set()):
        st.session_state.setdefault("expanded", set()).add(clicked)
        st.rerun()


def main() -> None:
    brain = _open_brain(os.environ.get("DROID_BRAIN_DIR", "."))

    st.title(f"🧠 {brain.config.name}")
    if brain.config.description:
        st.caption(brain.config.description)

    st.markdown(ROW_CSS, unsafe_allow_html=True)  # buttons styled as list rows
    tab_structure, tab_search, tab_map, tab_jobs, tab_edit = st.tabs(
        ["Structure", "Search", "Map", "Jobs", "Edit"]
    )

    # ---- Structure — clickable doc_type folders → entity hub ------------- #
    with tab_structure:
        render_structure(brain)

    # ---- Search — search box + clickable results ------------------------ #
    with tab_search:
        render_search(brain)

    # ---- Map -------------------------------------------------------------- #
    with tab_map:
        render_map(brain)

    # ---- Jobs — connectors + scheduled ingestion jobs ------------------- #
    with tab_jobs:
        render_connectors(brain)

    # ---- Edit (guidance only — editing happens via agent/CLI) ------------- #
    with tab_edit:
        render_edit_guide(brain)


def _esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# Colored-square emojis, so button-row labels carry the per-doc_type color.
_SQUARES = {
    "\U0001F7E5": (208, 48, 47), "\U0001F7E7": (230, 126, 34), "\U0001F7E8": (241, 196, 15),
    "\U0001F7E9": (46, 204, 113), "\U0001F7E6": (52, 152, 219), "\U0001F7EA": (155, 89, 182),
    "\U0001F7EB": (120, 80, 40), "⬛": (30, 30, 30), "⬜": (235, 235, 235),
}


def _square(hex_color: str) -> str:
    """Nearest colored-square emoji to a hex color."""
    try:
        r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    except (ValueError, IndexError):
        return "⬜"
    return min(_SQUARES.items(),
              key=lambda kv: sum((c - v) ** 2 for c, v in zip((r, g, b), kv[1])))[0]


def _dt_square(brain: "Brain", doc_type: str) -> str:
    return _square(_color(brain, doc_type))


# Style Streamlit buttons as clean full-width list rows (whole row clickable,
# no selection checkbox, no page reload). Injected once per run.
ROW_CSS = (
    "<style>"
    "div[data-testid='stButton'] > button{width:100%;text-align:left;"
    "justify-content:flex-start;border:1px solid #ececec;border-radius:6px;"
    "background:#fff;padding:7px 12px;font-weight:400;font-size:0.92rem;margin-bottom:-1px}"
    "div[data-testid='stButton'] > button:hover{background:#f5f6f8;border-color:#dcdcdc;color:inherit}"
    "div[data-testid='stButton'] > button:focus{box-shadow:none;color:inherit}"
    "</style>"
)


# --- clickable explorer shared by Structure + Search (separate namespaces) --- #

def _nav(ns: str, dt=None, e=None):
    st.session_state[f"{ns}_dt"] = dt
    st.session_state[f"{ns}_e"] = e
    st.rerun()


def _explorer_folders(brain: Brain, ns: str) -> None:
    counts = brain.counts()
    for name in sorted(brain.config.doc_types, key=lambda n: -counts.get(n, 0)):
        if st.button(f"{_dt_square(brain, name)} {name} · {counts.get(name, 0):,} entities",
                     key=f"{ns}_fold_{name}"):
            _nav(ns, dt=name)


def _explorer_folder(brain: Brain, ns: str, doc_type: str) -> None:
    dt = brain.config.doc_type(doc_type)
    count = brain.counts().get(doc_type, 0)
    col = st.columns([1, 5])
    if col[0].button("← back", key=f"{ns}_back_{doc_type}"):
        _nav(ns)
    st.markdown(f"**{_dt_square(brain, doc_type)} {_esc(doc_type)}**  ·  {count} entities  ·  storage `{dt.storage}`")
    if dt.description:
        st.caption(dt.description)
    with st.expander("schema & relationships", expanded=True):
        if dt.fields:
            st.table([{"field": f.name, "type": f.type, "search": f.search, "boost": f.boost}
                      for f in dt.fields])
        if dt.relationships:
            st.caption("declared: " + ", ".join(
                f"“{r.name}”" + (f" → {r.target_doc_type}" if r.target_doc_type else "")
                for r in dt.relationships))
        observed = brain.observed_relationships(doc_type)
        if observed:
            st.caption("in use: " + ", ".join(
                f"“{m}” ×{n}" for m, n in sorted(observed.items(), key=lambda x: -x[1])))
    entities = brain.backend.all_entities([doc_type])
    if not entities:
        st.info("Empty — no entities of this type yet.")
        return
    for en in entities:
        desc = en.fields.get("description", "")
        label = f"\U0001F4C4 {en.name}" + (f" · {desc[:60]}" if desc else "")
        if st.button(label, key=f"{ns}_ent_{en.id}"):
            _nav(ns, dt=doc_type, e=en.id)


def _explorer_entity(brain: Brain, ns: str, entity_id: str, doc_type) -> None:
    """Entity hub view — fields + related nodes grouped by meaning, each a
    clickable row that jumps to its own hub."""
    entity = brain.get_entity(entity_id)
    col = st.columns([1, 5])
    if col[0].button("← back", key=f"{ns}_hback_{entity_id}"):
        _nav(ns, dt=doc_type)
    if entity is None:
        st.warning(f"no entity `{entity_id}`")
        return
    st.markdown(f"**{_dt_square(brain, entity.doc_type)} {_esc(entity.name)}**  ·  `{entity.id}`")
    if entity.fields:
        st.table([{"field": k, "value": str(v)} for k, v in entity.fields.items()])
    outgoing = [("→", m, t) for _s, t, m in brain.backend.relationships_from(entity_id)]
    incoming = [("←", m, s) for s, _t, m in brain.backend.relationships_to(entity_id)]
    rels = outgoing + incoming
    if not rels:
        st.caption("no relationships yet")
        return
    st.markdown("**Relationships**")
    for i, (arrow, meaning, other_id) in enumerate(rels):
        other = brain.get_entity(other_id)
        nm = other.name if other else other_id.split(":", 1)[-1]
        sq = _dt_square(brain, other.doc_type) if other else "⬜"
        label = f"{arrow}  {meaning or '(related)'}: {sq} {nm} · {other_id}"
        if st.button(label, key=f"{ns}_rel_{i}_{other_id}"):
            _nav(ns, dt=(other.doc_type if other else None), e=other_id)


def render_structure(brain: Brain) -> None:
    """Doc_type structure + counts; whole rows clickable to drill in."""
    ns = "struct"
    st.markdown(f"**\U0001F9E0 {_esc(brain.config.name)}**")
    total = sum(brain.counts().values())
    st.caption(f"{total:,} entities · {len(brain.config.doc_types)} doc_types · click a row")
    e = st.session_state.get(f"{ns}_e")
    dt = st.session_state.get(f"{ns}_dt")
    if e:
        _explorer_entity(brain, ns, e, dt)
    elif dt:
        _explorer_folder(brain, ns, dt)
    else:
        _explorer_folders(brain, ns)


def render_search(brain: Brain) -> None:
    """Search box on top; idle browses folders, typing shows clickable results."""
    ns = "search"
    query = st.text_input(
        "Search", label_visibility="collapsed",
        placeholder="Search the brain…  e.g. payment, checkout, redis",
    )
    e = st.session_state.get(f"{ns}_e")
    dt = st.session_state.get(f"{ns}_dt")
    if e:
        _explorer_entity(brain, ns, e, dt)
        return
    if query:
        results = brain.search(query=query, limit=50)
        st.caption(f"{results.total} result(s) · click one to open")
        if not results.results:
            st.info("No matches.")
            return
        for r in results.results:
            label = f"{_dt_square(brain, r['doc_type'])} {r['name']} · {r['id']} · {r['doc_type']}"
            if st.button(label, key=f"{ns}_res_{r['id']}"):
                _nav(ns, dt=r["doc_type"], e=r["id"])
        return
    if dt:
        _explorer_folder(brain, ns, dt)
    else:
        st.caption("browse by type, or search above")
        _explorer_folders(brain, ns)


def render_map(brain: Brain) -> None:
    counts = brain.counts()
    doc_types = [dt for dt in brain.config.doc_types if counts.get(dt)]
    if not doc_types:
        st.info("No entities yet. Add some and run `droid-brain index`.")
        return

    # 1) Anchor on a doc_type. Changing it resets the selection.
    col1, col2 = st.columns([1, 2])
    with col1:
        anchor_type = st.selectbox(
            "Anchor doc_type",
            doc_types,
            format_func=lambda d: f"{d}  ({counts.get(d, 0)})",
        )
    if st.session_state.get("anchor_type") != anchor_type:
        st.session_state.anchor_type = anchor_type
        st.session_state.expanded = set()

    # 2) Pick the entities of that type you care about (catalog).
    catalog = brain.backend.all_entities([anchor_type])
    cat_options = {f"{e.name}  ({e.id})": e.id for e in catalog}
    with col2:
        chosen_labels = st.multiselect(
            f"{anchor_type} entities to map",
            list(cat_options),
            default=list(cat_options),  # start with all; trim to what matters
            help="Each selected entity is an anchor. Click any node to expand it.",
        )
    selected_ids = [cat_options[l] for l in chosen_labels]

    expanded = st.session_state.get("expanded", set())
    anchors = list(dict.fromkeys(selected_ids + list(expanded)))

    if expanded:
        st.caption(f"expanded: {', '.join(sorted(expanded))}")
        if st.button("↺ reset expansions"):
            st.session_state.expanded = set()
            st.rerun()

    if not anchors:
        st.info(f"Select one or more {anchor_type} entities above to draw the map.")
        return

    graph = build_graph(brain, anchors, depth=1)
    st.caption(f"{len(graph.nodes)} nodes · {len(graph.edges)} edges  ·  "
               "click a node to expand its relationships")
    render_graph(brain, graph)

    # streamlit-agraph doesn't paint on its very first render inside a tab (the
    # canvas mounts with zero size). Force one rerun so it remounts with the tab
    # active — the same thing a manual widget toggle does.
    if not st.session_state.get("_map_primed"):
        st.session_state["_map_primed"] = True
        st.rerun()


def render_edit_guide(brain: Brain) -> None:
    """Read-only guidance: how to edit the brain via an agent or the CLI.

    Editing deliberately does NOT happen in this UI — the brain's source of truth
    is files + the validated write path. This tab tells you how to connect."""
    from pathlib import Path

    brain_dir = os.environ.get("DROID_BRAIN_DIR", ".")
    st.info(
        "**Recommended: humans don't edit the brain directly.** Let it be written "
        "either by an **agent** (via MCP or the CLI) or by your code via the **API** "
        "(programmatically). That keeps every write validated, consistent, and "
        "traceable — and this UI stays read-only on purpose."
    )

    st.subheader("Let an agent edit it (recommended)")
    skill_path = Path(brain_dir) / ".claude" / "skills" / "edit-brain" / "SKILL.md"
    skill_note = (
        "It follows the bundled **`edit-brain` skill** "
        "(`.claude/skills/edit-brain/SKILL.md`)"
        if skill_path.exists()
        else "Run `droid-brain init` on a new brain to also scaffold an **`edit-brain` "
             "skill** the agent follows"
    )
    st.markdown(
        "Connect the brain to Claude Code / any MCP client, then just ask it to add "
        f"or update knowledge. {skill_note}, and it reads `navigation_guidelines` "
        "first, so it reuses your existing doc_types and relationship vocabulary."
    )
    st.code(
        '// .mcp.json (droid-brain init writes this for you)\n'
        '{\n'
        '  "mcpServers": {\n'
        '    "droid-brain": { "command": "droid-brain", "args": ["mcp", "--brain", "."] }\n'
        '  }\n'
        '}',
        language="json",
    )
    st.caption("Then, in the agent: “read navigation_guidelines, then add a service "
               "entity for checkout and link it to the postgres datastore.”")

    # Copy-pastable skill — drop it into any other Claude Code terminal.
    st.markdown("**Copy the `edit-brain` skill** into another agent "
                "(paste as `.claude/skills/edit-brain/SKILL.md`, or just paste the "
                "text into the chat):")
    if skill_path.exists():
        skill_text = skill_path.read_text()
    else:
        from droid_brain.scaffold import SKILL_MD
        skill_text = SKILL_MD
    st.code(skill_text, language="markdown")  # st.code shows a copy button

    st.subheader("Or edit from the CLI / files")
    st.code(
        f"# define a concept\n"
        f"droid-brain add-doc-type service --brain {brain_dir}\n"
        f"#   → edit doc_types/service.yaml (fields, boosts, relationships)\n\n"
        f"# add an entity (file-backed types): write entities/<type>/<slug>.json, then\n"
        f"droid-brain index --brain {brain_dir}\n"
        f"droid-brain validate --brain {brain_dir}\n\n"
        f"# pull entities from a tool on a schedule\n"
        f"droid-brain ingest <connector> --brain {brain_dir}",
        language="bash",
    )

    st.subheader("Or update programmatically (API / webhook / triggers)")
    st.markdown(
        "For deterministic, no-agent updates from your own code — a webhook, a "
        "post-deploy step, a cron job — write directly. Two ways:"
    )
    st.markdown("**In-process (co-located with the brain files):**")
    st.code(
        "from droid_brain import Brain, Entity\n\n"
        f'brain = Brain.open("{brain_dir}")\n'
        'e = brain.get_entity("service:api") or Entity(id="service:api", '
        'doc_type="service", name="API")\n'
        'e.fields["status"] = "degraded"      # update knowledge\n'
        "brain.put_entity(e)                   # validated upsert (honors storage policy)",
        language="python",
    )
    st.markdown("**Over HTTP (remote brain running `droid-brain serve`):** call the "
                "`put_entity` MCP tool as plain JSON-RPC — no LLM involved.")
    st.code(
        "curl -X POST https://brain.example.com/mcp \\\n"
        '  -H "Authorization: Bearer $DROID_BRAIN_TOKEN" \\\n'
        '  -H "Content-Type: application/json" \\\n'
        '  -H "Accept: application/json, text/event-stream" \\\n'
        "  -d '{\"jsonrpc\":\"2.0\",\"id\":\"1\",\"method\":\"tools/call\",\n"
        "       \"params\":{\"name\":\"put_entity\",\"arguments\":{\n"
        "         \"doc_type\":\"service\",\"id\":\"service:api\",\"name\":\"API\",\n"
        "         \"fields\":{\"status\":\"degraded\"}}}}'",
        language="bash",
    )
    st.caption("Serve it with:  droid-brain serve --brain <dir> --token $DROID_BRAIN_TOKEN")

    with st.expander("What can I change, and where does it live?"):
        st.markdown(
            "- **doc_types** (concepts/schemas) → `doc_types/*.yaml` — always in git.\n"
            "- **entities** (instances) → `storage: file` types live in "
            "`entities/**/*.json` (git); `storage: index` types live only in the "
            "search DB (written by agents/connectors).\n"
            "- **relationships** → the `related_to` field on each entity; declare the "
            "vocabulary under `relationships:` in the doc_type.\n"
            "- **connectors** (ingestion scripts) → `connectors/*.py` (see the Jobs tab)."
        )


def render_connectors(brain: Brain) -> None:
    """Jobs tab — connectors (entity-fetching jobs) and their schedules / last
    runs: the cron/ingestion side of the brain."""
    st.markdown("**Connectors & scheduled jobs**")
    st.caption("Ingestion scripts in `connectors/*.py` that pull entities from an "
               "MCP server into the brain.")
    try:
        from droid_brain.connectors.runner import discover_connectors
        from droid_brain.scheduling import RunState
    except Exception:
        st.caption("connectors unavailable")
        return

    found = discover_connectors(brain)
    if not found:
        st.caption("No connectors. Add `connectors/*.py` to pull entities from an "
                   "MCP server on a schedule.")
        return

    import inspect

    state = RunState(brain.config.root) if brain.config.root else None
    for name, cls in sorted(found.items()):
        meta = (state._data.get(name, {}) if state else {})
        last_run = (meta.get("last_run") or "never")[:19]
        with st.container(border=True):
            st.markdown(f"**⚙️ {name}**  ·  schedule `{cls.schedule}`  ·  "
                        f"target `{getattr(cls, 'target_doc_type', '—')}`")
            c = st.columns(3)
            c[0].metric("source", "live MCP" if cls.mcp_url else "offline/demo")
            c[1].metric("last run", last_run)
            c[2].metric("entities", meta.get("last_count", "—"))
            st.caption(f"endpoint: `{cls.mcp_url or 'offline/demo'}`  ·  "
                       f"last status: {meta.get('last_status', '—')}")
            # The actual job script — visible, inspectable.
            with st.expander("view connector script"):
                try:
                    src = inspect.getsource(cls)
                except (OSError, TypeError):
                    src = "(source unavailable)"
                st.code(src, language="python")
            st.caption(f"Run now: `droid-brain ingest {name}` · or `droid-brain run` "
                       "for all due jobs (wire into cron / CI).")


main()
