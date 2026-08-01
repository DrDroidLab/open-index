"""MCP server (stdio) exposing a brain to any LLM/agent.

Tools, mirroring the brain's query surface:
  1. get_brain_structure - textual explanation of what the brain stores
  2. search_brain        - boosted full-text search with optional doc_type filter
  3. get_entity          - fetch one entity by doc_type + name
  4. create_entity       - add or update an entity

Run with: droid-brain mcp <brain>
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolRequestParams, CallToolResult, ListToolsResult, TextContent, Tool

from . import store

_TOOLS = [
    Tool(
        name="get_brain_structure",
        description="Textual explanation of the data stored in the brain: doc_types, entity counts, descriptions and example values.",
        input_schema={"type": "object", "properties": {}},
    ),
    Tool(
        name="search_brain",
        description="Full-text search over all entities in the brain. Results are ranked with field boosters (name matches count most) and per-doc_type boosters.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search terms (matched with prefix matching across all fields)"},
                "doc_type": {"type": "string", "description": "Optional doc_type to filter on"},
                "limit": {"type": "integer", "description": "Max results (default 10)", "default": 10},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="get_entity",
        description="Fetch a specific entity by its doc_type and name.",
        input_schema={
            "type": "object",
            "properties": {
                "doc_type": {"type": "string"},
                "name": {"type": "string"},
            },
            "required": ["doc_type", "name"],
        },
    ),
    Tool(
        name="create_entity",
        description="Create (or update) an entity in the brain. The doc_type must already exist; data must be a JSON object.",
        input_schema={
            "type": "object",
            "properties": {
                "doc_type": {"type": "string"},
                "name": {"type": "string"},
                "data": {"type": "object", "description": "The entity's fields as a JSON object"},
            },
            "required": ["doc_type", "name", "data"],
        },
    ),
]


def run(brain_name: str) -> None:
    brain = store.open_brain(brain_name)

    def _dispatch(name: str, arguments: dict[str, Any]) -> str:
        if name == "get_brain_structure":
            return brain.structure_text()
        if name == "search_brain":
            results = brain.search(
                arguments["query"],
                doc_type=arguments.get("doc_type"),
                limit=int(arguments.get("limit") or 10),
            )
            return json.dumps(results, indent=2) if results else "No results."
        if name == "get_entity":
            entity = brain.get_entity(arguments["doc_type"], arguments["name"])
            if not entity:
                raise ValueError(
                    f"No entity {arguments['name']!r} of doc_type {arguments['doc_type']!r} in brain {brain.name!r}"
                )
            return json.dumps(entity, indent=2)
        if name == "create_entity":
            entity_id = brain.upsert_entity(arguments["doc_type"], arguments["name"], arguments["data"])
            return json.dumps({"id": entity_id, "doc_type": arguments["doc_type"], "name": arguments["name"]})
        raise ValueError(f"Unknown tool {name!r}")

    async def _on_list_tools(ctx: Any, params: Any) -> ListToolsResult:
        return ListToolsResult(tools=_TOOLS)

    async def _on_call_tool(ctx: Any, params: CallToolRequestParams) -> CallToolResult:
        try:
            text = _dispatch(params.name, params.arguments or {})
        except Exception as e:
            return CallToolResult(content=[TextContent(type="text", text=str(e))], is_error=True)
        return CallToolResult(content=[TextContent(type="text", text=text)])

    server = Server(
        name=f"droid-brain-{brain_name}",
        on_list_tools=_on_list_tools,
        on_call_tool=_on_call_tool,
    )

    async def _main() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    asyncio.run(_main())
