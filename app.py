"""Streamlit UI for Droid Brain.

Run with: streamlit run app.py
"""

from __future__ import annotations

import json
import os
from datetime import datetime

import streamlit as st

from droid_brain.core import DroidBrain

OPENSEARCH_URL = os.environ.get("DROID_BRAIN_OPENSEARCH_URL", "http://localhost:9200")

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Droid Brain",
    page_icon="🧠",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Session state init
# ---------------------------------------------------------------------------
if "db" not in st.session_state:
    st.session_state.db = DroidBrain(opensearch_url=OPENSEARCH_URL)
if "selected_brain" not in st.session_state:
    st.session_state.selected_brain = None
if "refresh_counter" not in st.session_state:
    st.session_state.refresh_counter = 0


def refresh() -> None:
    st.session_state.refresh_counter += 1


# ---------------------------------------------------------------------------
# Sidebar — Brain management
# ---------------------------------------------------------------------------
st.sidebar.title("🧠 Droid Brain")

db: DroidBrain = st.session_state.db

# List brains
brains = db.list_brains()
brain_names = [b["name"] for b in brains]

# Brain selector
if brain_names:
    selected = st.sidebar.selectbox(
        "Select brain",
        brain_names,
        index=(
            brain_names.index(st.session_state.selected_brain)
            if st.session_state.selected_brain in brain_names
            else 0
        ),
        key=f"brain_select_{st.session_state.refresh_counter}",
    )
    st.session_state.selected_brain = selected
else:
    st.session_state.selected_brain = None

# Create brain
st.sidebar.markdown("---")
st.sidebar.subheader("Create new brain")
new_brain_name = st.sidebar.text_input("Brain name", key="new_brain_name")
new_brain_desc = st.sidebar.text_input("Description", key="new_brain_desc")
if st.sidebar.button("Create Brain", width="stretch"):
    if new_brain_name.strip():
        db.create_brain(new_brain_name.strip(), new_brain_desc.strip())
        st.session_state.selected_brain = new_brain_name.strip()
        refresh()
        st.rerun()
    else:
        st.sidebar.error("Brain name is required.")

# Seed demo brain button
st.sidebar.markdown("---")
if st.sidebar.button("🌱 Seed Demo Brain", width="stretch"):
    from droid_brain.cli import seed_demo as _seed

    _seed.callback(_seed.make_context("seed-demo", ["demo"]))
    st.session_state.selected_brain = "demo"
    refresh()
    st.rerun()

# Refresh button
if st.sidebar.button("🔄 Refresh", width="stretch"):
    refresh()
    st.rerun()

# Delete brain
if st.session_state.selected_brain:
    st.sidebar.markdown("---")
    with st.sidebar.expander("⚠️ Danger zone"):
        if st.button(
            f"Delete '{st.session_state.selected_brain}'",
            width="stretch",
            type="primary",
        ):
            db.delete_brain(st.session_state.selected_brain)
            st.session_state.selected_brain = None
            refresh()
            st.rerun()

# ---------------------------------------------------------------------------
# Main area
# ---------------------------------------------------------------------------
st.title("Droid Brain")
st.caption("Structured organisational knowledge for AI agents")

if not st.session_state.selected_brain:
    st.info("👈 Select or create a brain from the sidebar to get started.")
    st.stop()

brain_name = st.session_state.selected_brain

# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
tab_structure, tab_entities, tab_doctypes, tab_search, tab_mcp = st.tabs(
    ["📊 Structure", "📋 Entities", "📐 Doc Types", "🔍 Search", "🔌 MCP / CLI"]
)

# ===========================================================================
# Tab 1: Brain Structure
# ===========================================================================
with tab_structure:
    st.subheader(f"Brain: {brain_name}")
    structure = db.get_brain_structure(brain_name)

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Doc Types", len(structure.doc_types))
    with col2:
        st.metric("Total Entities", structure.total_entities)
    with col3:
        st.metric("Index", f"droid_brain__{brain_name}")

    st.markdown("---")

    if not structure.doc_types:
        st.info("No doc types defined yet. Create one in the **Doc Types** tab.")
    else:
        for dt in structure.doc_types:
            with st.expander(
                f"📁 {dt['name']} ({dt['entity_count']} entities)"
            ):
                if dt.get("description"):
                    st.caption(dt["description"])

                if dt.get("schema_fields"):
                    st.markdown("**Schema fields:**")
                    fields_data = []
                    for f in dt["schema_fields"]:
                        fields_data.append(
                            {
                                "Name": f.get("name", ""),
                                "Type": f.get("field_type", "string"),
                                "Required": "✅" if f.get("required") else "",
                                "Search": f.get("search_type", ""),
                                "Processing": f.get("processing_type", ""),
                            }
                        )
                    st.dataframe(fields_data, width="stretch", hide_index=True)

                if dt.get("examples"):
                    st.markdown("**Example entities:**")
                    for ex in dt["examples"]:
                        st.json(ex)

# ===========================================================================
# Tab 2: Entities
# ===========================================================================
with tab_entities:
    st.subheader("Entities")

    # Filters
    doctypes = db.list_doctypes(brain_name)
    dt_names = [dt["name"] for dt in doctypes]

    col_filter, col_create = st.columns([3, 2])

    with col_filter:
        dt_filter = st.selectbox(
            "Filter by doc type", ["All"] + dt_names, key="entity_dt_filter"
        )

    # List entities
    entities = db.list_entities(
        brain_name,
        doc_type=dt_filter if dt_filter != "All" else None,
        size=100,
    )

    if not entities:
        st.info("No entities yet. Create one below or seed the demo brain.")
    else:
        for entity in entities:
            with st.expander(
                f"🔹 {entity['entity_id']} — {entity['doc_type']} — {entity.get('data', {}).get(list(entity.get('data', {}).keys())[0]) if entity.get('data') else '...'}"
            ):
                col_id, col_type, col_ts = st.columns(3)
                with col_id:
                    st.caption(f"**ID:** `{entity['entity_id']}`")
                with col_type:
                    st.caption(f"**Type:** {entity['doc_type']}")
                with col_ts:
                    st.caption(f"**Created:** {entity.get('created_at', '?')[:19]}")
                st.json(entity.get("data", {}))
                if st.button("🗑️ Delete", key=f"del_{entity['entity_id']}"):
                    db.delete_entity(brain_name, entity["entity_id"])
                    refresh()
                    st.rerun()

    # Create entity form
    st.markdown("---")
    st.subheader("Create Entity")

    if not dt_names:
        st.warning("Create a doc type first in the **Doc Types** tab.")
    else:
        create_dt = st.selectbox("Doc type", dt_names, key="create_entity_dt")
        # Get schema fields for this doc type
        schema = db.get_doctype(brain_name, create_dt)
        schema_fields = schema.get("schema_fields", []) if schema else []

        with st.form("create_entity_form"):
            data_dict = {}
            if schema_fields:
                st.markdown("**Fields:**")
                # Pre-define common layout with 2 columns for compact forms
                for field in schema_fields:
                    field_name = field["name"]
                    field_type = field.get("field_type", "string")
                    label = f"{field_name}{' *' if field.get('required') else ''}"
                    if field_type in ("number",):
                        data_dict[field_name] = st.number_input(label, step=1)
                    elif field_type in ("boolean",):
                        data_dict[field_name] = st.checkbox(label)
                    else:
                        data_dict[field_name] = st.text_input(label)
            else:
                st.markdown("**Raw JSON data (no schema defined):**")
                raw_json = st.text_area("Entity data (JSON)", '{\n  \n}', height=150)

            submitted = st.form_submit_button("Create Entity", width="stretch")

            if submitted:
                if schema_fields:
                    # Remove empty optional fields
                    data_dict = {k: v for k, v in data_dict.items() if v != ""}
                else:
                    try:
                        data_dict = json.loads(raw_json)
                    except json.JSONDecodeError:
                        st.error("Invalid JSON.")
                        st.stop()

                entity = db.create_entity(brain_name, create_dt, data_dict)
                st.success(f"Entity `{entity['entity_id']}` created!")
                refresh()
                st.rerun()

# ===========================================================================
# Tab 3: Doc Types
# ===========================================================================
with tab_doctypes:
    st.subheader("Doc Types")

    doctypes = db.list_doctypes(brain_name)

    if not doctypes:
        st.info("No doc types defined yet.")

    for dt in doctypes:
        with st.expander(f"📐 {dt['name']}"):
            if dt.get("description"):
                st.caption(dt["description"])
            st.json(dt.get("schema_fields", []))

    # Create doc type form
    st.markdown("---")
    st.subheader("Create Doc Type")

    with st.form("create_doctype_form"):
        col_name, col_desc = st.columns(2)
        with col_name:
            new_dt_name = st.text_input("Name", placeholder="e.g. service, dashboard, runbook")
        with col_desc:
            new_dt_desc = st.text_input("Description", placeholder="What this doc type represents")

        st.markdown("**Schema fields** (define as JSON array):")
        fields_json = st.text_area(
            "Fields",
            value=json.dumps(
                [
                    {
                        "name": "title",
                        "field_type": "string",
                        "required": True,
                        "search_type": "syntactic",
                    }
                ],
                indent=2,
            ),
            height=200,
            help="JSON array of field objects. Each field: name, field_type (string/number/boolean), required, processing_type (keyword/text), search_type (syntactic/semantic)",
        )

        submitted_dt = st.form_submit_button("Create Doc Type", width="stretch")

        if submitted_dt:
            if not new_dt_name.strip():
                st.error("Name is required.")
            else:
                try:
                    fields_data = json.loads(fields_json)
                except json.JSONDecodeError:
                    st.error("Invalid JSON for fields.")
                else:
                    db.create_doctype(brain_name, new_dt_name.strip(), new_dt_desc.strip(), fields_data)
                    st.success(f"Doc type '{new_dt_name}' created!")
                    refresh()
                    st.rerun()

# ===========================================================================
# Tab 4: Search
# ===========================================================================
with tab_search:
    st.subheader("Search Entities")

    col_search, col_search_dt, col_search_size = st.columns([4, 2, 1])
    with col_search:
        search_query = st.text_input("Search query", placeholder="Enter search terms...")
    with col_search_dt:
        search_dt = st.selectbox("Doc type", ["All"] + dt_names, key="search_dt_filter")
    with col_search_size:
        search_size = st.number_input("Max results", min_value=1, max_value=100, value=20)

    if st.button("🔍 Search", width="stretch") and search_query.strip():
        results = db.search(
            brain_name,
            query_text=search_query.strip(),
            doc_type=search_dt if search_dt != "All" else None,
            size=search_size,
        )

        if not results:
            st.info(f"No results for '{search_query}'.")
        else:
            st.success(f"Found {len(results)} result(s)")
            for r in results:
                with st.expander(
                    f"🔹 {r['entity_id']} — {r.get('doc_type', '?')}"
                ):
                    st.json(r.get("data", {}))

# ===========================================================================
# Tab 5: MCP / CLI
# ===========================================================================
with tab_mcp:
    st.subheader("Connect an LLM / Agent")

    st.markdown("### MCP Server")
    st.markdown(
        """
        The Droid Brain MCP server exposes three tools to any MCP-compatible LLM:

        | Tool | Description |
        |------|-------------|
        | `brain_structure` | Get a textual overview of all doc types, counts, and examples |
        | `search_brain` | Full-text search across entities with optional doc_type filter |
        | `fetch_entity` | Retrieve a specific entity by ID |
        """
    )

    st.markdown("#### Start the MCP server")
    st.code(
        "droid-brain mcp-server --transport stdio",
        language="bash",
    )

    st.markdown("#### Or with SSE transport (HTTP):")
    st.code(
        "droid-brain mcp-server --transport sse --port 8000",
        language="bash",
    )

    st.markdown("#### MCP Client config (e.g. Claude Desktop, Cursor, etc.)")
    config = {
        "mcpServers": {
            "droid-brain": {
                "command": "droid-brain",
                "args": ["mcp-server", "--transport", "stdio"],
            }
        }
    }
    st.code(json.dumps(config, indent=2), language="json")

    st.markdown("---")
    st.markdown("### CLI Commands")
    st.markdown("Once the brain is set up, you can also query it from the terminal:")

    cli_commands = [
        ("list all brains", "droid-brain list-brains"),
        ("show brain structure", f"droid-brain structure {brain_name}"),
        ("list entities", f"droid-brain list-entities {brain_name}"),
        ("search entities", f"droid-brain search {brain_name} gateway"),
        ("get a specific entity", f"droid-brain get-entity {brain_name} <entity_id>"),
    ]
    for desc, cmd in cli_commands:
        st.markdown(f"**{desc}:**")
        st.code(cmd, language="bash")

    st.markdown("---")
    st.markdown("### OpenSearch Index")
    st.markdown(f"Entities are stored in the OpenSearch index `droid_brain__{brain_name}`.")
    st.code(
        f'curl -s "http://localhost:9200/droid_brain__{brain_name}/_search" | python3 -m json.tool | head -40',
        language="bash",
    )
