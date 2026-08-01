"""MCP server exposing a brain to any LLM/agent.

Tools, mirroring the brain's query surface:
  1. get_brain_structure - textual explanation of what the brain stores
  2. search_brain        - boosted full-text search with optional doc_type filter
  3. get_entity          - fetch one entity by doc_type + name
  4. create_entity       - add or update an entity

Run locally (stdio, for MCP clients on the same machine):
    droid-brain mcp <brain>

Serve it to other machines over HTTP:
    droid-brain mcp <brain> --http --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import json

from mcp.server.mcpserver import MCPServer

from . import store

MAX_LIMIT = 100


def run(brain_name: str, http: bool = False, host: str = "127.0.0.1", port: int = 8000) -> None:
    brain = store.open_brain(brain_name)
    server = MCPServer(name=f"droid-brain-{brain_name}")

    @server.tool(description="Textual explanation of the data stored in the brain: doc_types, entity counts, descriptions, schema fields and example values.")
    def get_brain_structure() -> str:
        return brain.structure_text()

    @server.tool(description="Full-text search over all entities in the brain. Results are ranked with field boosters (name matches count most) and per-doc_type boosters.")
    def search_brain(query: str, doc_type: str | None = None, limit: int = 10) -> str:
        if not query:
            raise ValueError("Missing required argument 'query'")
        results = brain.search(query, doc_type=doc_type, limit=max(1, min(limit, MAX_LIMIT)))
        return json.dumps(results, indent=2) if results else "No results."

    @server.tool(description="Fetch a specific entity by its doc_type and name.")
    def get_entity(doc_type: str, name: str) -> str:
        entity = brain.get_entity(doc_type, name)
        if not entity:
            raise ValueError(f"No entity {name!r} of doc_type {doc_type!r} in brain {brain.name!r}")
        return json.dumps(entity, indent=2)

    @server.tool(description="Create (or update) an entity in the brain. The doc_type must already exist; data must be a JSON object satisfying the doc_type's schema.")
    def create_entity(doc_type: str, name: str, data: dict) -> str:
        entity_id = brain.upsert_entity(doc_type, name, data)
        return json.dumps({"id": entity_id, "doc_type": doc_type, "name": name.strip()})

    try:
        if http:
            print(f"Serving brain '{brain_name}' over MCP (streamable HTTP) on http://{host}:{port}/mcp")
            server.run(transport="streamable-http", host=host, port=port)
        else:
            server.run(transport="stdio")
    finally:
        brain.close()
