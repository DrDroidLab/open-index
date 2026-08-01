"""Droid Brain UI (Streamlit). Launched via `droid-brain` / `droid-brain ui [brain]`.

On launch: opens the requested (or most recent) brain; if none exists yet,
shows the create-a-brain screen.
"""

from __future__ import annotations

import json
import sys

import streamlit as st

from droid_brain import store
from droid_brain.seed import seed_demo

NEW_BRAIN = "+ New brain"

st.set_page_config(page_title="Droid Brain", layout="wide")


def _initial_brain() -> str | None:
    # Brain name is passed after `--` by the CLI: streamlit run ui.py -- <brain>
    return sys.argv[1] if len(sys.argv) > 1 else None


@st.cache_resource
def _get_brain(name: str) -> store.Brain:
    """One shared connection per brain across reruns (WAL + busy timeout make it safe)."""
    return store.open_brain(name)


def _create_brain_form() -> None:
    st.title("Create your first brain")
    st.caption("A brain is a local knowledge index for your agents: doc_types, entities and boosted search — all in one file.")
    with st.form("create_brain"):
        name = st.text_input("Brain name (this is its index)", placeholder="acme-infra")
        description = st.text_input("Description (optional)", placeholder="Production infrastructure knowledge")
        seed = st.checkbox("Seed with demo entities", value=True)
        if st.form_submit_button("Create brain", type="primary"):
            try:
                brain = store.create_brain(name, description=description)
                if seed:
                    seed_demo(brain)
                brain.close()
            except ValueError as e:
                st.error(str(e))
            else:
                st.success(f"Created brain '{name}'")
                st.rerun()


def _doc_types_tab(brain: store.Brain) -> None:
    doc_types = brain.list_doc_types()
    if doc_types:
        st.dataframe(
            [
                {
                    "doc_type": dt["name"],
                    "boost": dt["boost"],
                    "entities": dt["entities"],
                    "description": dt["description"],
                    "schema fields": ", ".join(store.schema_field_paths(dt["schema"])),
                }
                for dt in doc_types
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No doc_types yet. Create one below.")

    st.subheader("New doc_type")
    with st.form("create_doc_type"):
        name = st.text_input("Name", placeholder="service")
        description = st.text_input("Description", placeholder="A production service and its operational metadata")
        boost = st.slider("Type booster", 0.1, 5.0, 1.0, 0.1,
                          help="Entities of this type are ranked boost x higher in search results")
        schema_text = st.text_area(
            "Schema (optional, nested JSON)",
            placeholder='{"properties": {"team": {"type": "string"}, "spec": {"type": "object", "properties": {"replicas": {"type": "integer"}}}}, "required": ["team"]}',
            height=120,
            help="JSON-Schema-ish structure for this doc_type's entities. Nested objects/arrays allowed. 'required' fields are enforced when saving entities.",
        )
        if st.form_submit_button("Create doc_type", type="primary"):
            try:
                schema = json.loads(schema_text) if schema_text.strip() else None
                brain.create_doc_type(name, description=description, boost=boost, schema=schema)
            except (ValueError, json.JSONDecodeError) as e:
                st.error(str(e))
            else:
                st.success(f"Created doc_type '{name}'")
                st.rerun()


def _entities_tab(brain: store.Brain) -> None:
    doc_types = [dt["name"] for dt in brain.list_doc_types()]
    if not doc_types:
        st.info("Create a doc_type first — or load the demo content.")
        if st.button("Load demo entities"):
            summary = seed_demo(brain)
            st.success(f"Loaded {summary['entities']} demo entities across {summary['doc_types']} doc_types")
            st.rerun()
        return

    st.subheader("New entity")
    doc_type = st.selectbox("doc_type", doc_types)
    schema = (brain.get_doc_type(doc_type) or {}).get("schema")
    template = json.dumps(store.entity_template(schema) if schema else {"description": ""}, indent=2)
    with st.form("create_entity"):
        name = st.text_input("Entity name", placeholder="api-gateway")
        data = st.text_area("Data (JSON object)", value=template, height=180, key=f"entity-data-{doc_type}")
        if st.form_submit_button("Save entity", type="primary"):
            try:
                parsed = json.loads(data)
                brain.upsert_entity(doc_type, name, parsed)
            except (ValueError, json.JSONDecodeError) as e:
                st.error(str(e))
            else:
                st.success(f"Saved entity '{name}'")
                st.rerun()

    st.subheader("Entities")
    for dt in doc_types:
        entities = brain.list_entities(doc_type=dt)
        if not entities:
            continue
        st.markdown(f"**{dt}** ({len(entities)})")
        for entity in entities:
            with st.expander(entity["name"]):
                st.json(entity["data"])
                if st.button("Delete", key=f"del-{entity['id']}"):
                    brain.delete_entity(entity["id"])
                    st.rerun()


def _search_tab(brain: store.Brain) -> None:
    doc_types = ["All"] + [dt["name"] for dt in brain.list_doc_types()]
    query = st.text_input("Search the brain", placeholder="payments latency")
    doc_type = st.selectbox("Filter by doc_type", doc_types)
    if not query:
        st.caption("Results are ranked with field boosters (name matches count most) and per-doc_type boosters.")
        return
    try:
        results = brain.search(query, doc_type=None if doc_type == "All" else doc_type)
    except Exception as e:
        st.error(f"Search failed: {e}")
        return
    if not results:
        st.info("No results.")
        return
    for r in results:
        with st.expander(f"**{r['name']}** — {r['doc_type']} (score {r['score']})"):
            st.json(r["data"])


def _connect_tab(brain: store.Brain) -> None:
    st.subheader("Use this brain from the terminal")
    st.code(f"droid-brain search {brain.name} \"your query\"", language="bash")
    st.subheader("Use this brain from any LLM (MCP)")
    st.markdown(f"Run `droid-brain mcp {brain.name}`, or point your MCP client config at it:")
    st.code(
        json.dumps(
            {
                "mcpServers": {
                    f"droid-brain-{brain.name}": {
                        "command": "droid-brain",
                        "args": ["mcp", brain.name],
                    }
                }
            },
            indent=2,
        ),
        language="json",
    )
    st.markdown("From another machine, serve it over HTTP with "
                f"`droid-brain mcp {brain.name} --http --host 0.0.0.0 --port 8000` "
                "and use the URL instead:")
    st.code(
        json.dumps({"mcpServers": {f"droid-brain-{brain.name}": {"url": "http://brain-host:8000/mcp"}}}, indent=2),
        language="json",
    )
    st.caption("Works with Claude Desktop, Cursor, Claude Code and any other MCP client.")


def main() -> None:
    brains = store.list_brains()
    names = [b["name"] for b in brains]
    initial = _initial_brain()

    st.sidebar.title("Droid Brain")
    options = names + [NEW_BRAIN]
    default_index = names.index(initial) if initial in names else 0
    choice = st.sidebar.selectbox("Brain", options, index=default_index)

    if choice == NEW_BRAIN:
        _create_brain_form()
        return

    brain = _get_brain(choice)
    st.title(choice)
    description = brain.get_meta("description")
    if description:
        st.caption(description)

    doc_types = brain.list_doc_types()
    total = sum(dt["entities"] for dt in doc_types)
    col1, col2 = st.columns(2)
    col1.metric("doc_types", len(doc_types))
    col2.metric("entities", total)

    tab_types, tab_entities, tab_search, tab_connect = st.tabs(
        ["Doc Types", "Entities", "Search", "Connect"]
    )
    with tab_types:
        _doc_types_tab(brain)
    with tab_entities:
        _entities_tab(brain)
    with tab_search:
        _search_tab(brain)
    with tab_connect:
        _connect_tab(brain)


main()
