"""MCP server that exposes Droid Brain tools to LLMs/agents.

Provides three tools:
  1. brain_structure  — get a textual overview of doc_types, counts, examples
  2. search_brain     — full-text search with optional doc_type filter
  3. fetch_entity     — retrieve a single entity by ID

Start with:
  droid-brain-mcp                     # stdio transport
  droid-brain-mcp --transport sse     # SSE transport on port 8000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    TextContent,
    Tool,
)

from droid_brain.core import DroidBrain

OPENSEARCH_URL = os.environ.get("DROID_BRAIN_OPENSEARCH_URL", "http://localhost:9200")

_DB_URL = OPENSEARCH_URL


def _get_db() -> DroidBrain:
    return DroidBrain(opensearch_url=_DB_URL)


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    Tool(
        name="brain_structure",
        title="Brain Structure",
        description="Get a textual explanation of the data stored within a brain. "
        "Returns doc_types, how many entities of each, example values, and "
        "descriptions — everything an agent needs to understand the brain's schema.",
        input_schema={
            "type": "object",
            "properties": {
                "brain_name": {
                    "type": "string",
                    "description": "Name of the brain to inspect.",
                }
            },
            "required": ["brain_name"],
        },
    ),
    Tool(
        name="search_brain",
        title="Search Brain",
        description="Search entities in a brain by full-text query, with optional doc_type filter.",
        input_schema={
            "type": "object",
            "properties": {
                "brain_name": {
                    "type": "string",
                    "description": "Name of the brain to search.",
                },
                "query": {
                    "type": "string",
                    "description": "Search query text.",
                },
                "doc_type": {
                    "type": "string",
                    "description": "Optional doc_type name to filter results.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (default 20).",
                    "default": 20,
                },
            },
            "required": ["brain_name", "query"],
        },
    ),
    Tool(
        name="fetch_entity",
        title="Fetch Entity",
        description="Fetch a specific entity by its ID.",
        input_schema={
            "type": "object",
            "properties": {
                "brain_name": {
                    "type": "string",
                    "description": "Name of the brain.",
                },
                "entity_id": {
                    "type": "string",
                    "description": "The entity's unique ID.",
                },
            },
            "required": ["brain_name", "entity_id"],
        },
    ),
]

# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


async def _tool_brain_structure(arguments: dict) -> str:
    brain_name = arguments["brain_name"]
    db = _get_db()
    structure = db.get_brain_structure(brain_name)

    lines = [f"Brain: {structure.brain_name}"]
    lines.append(f"Total entities: {structure.total_entities}")
    lines.append(f"Doc types: {len(structure.doc_types)}")
    lines.append("-" * 40)

    for dt in structure.doc_types:
        lines.append(f"\n📁 {dt['name']} ({dt['entity_count']} entities)")
        if dt.get("description"):
            lines.append(f"   Description: {dt['description']}")
        if dt.get("schema_fields"):
            lines.append("   Fields:")
            for f in dt["schema_fields"]:
                extra = []
                if f.get("required"):
                    extra.append("required")
                if f.get("search_type"):
                    extra.append(f"search={f['search_type']}")
                extra_str = f" ({', '.join(extra)})" if extra else ""
                lines.append(
                    f"     • {f['name']} : {f.get('field_type', 'string')}{extra_str}"
                )
        if dt.get("examples"):
            lines.append("   Examples:")
            for ex in dt["examples"]:
                lines.append(f"     {json.dumps(ex)}")

    return "\n".join(lines)


async def _tool_search_brain(arguments: dict) -> str:
    brain_name = arguments["brain_name"]
    query_text = arguments["query"]
    doc_type = arguments.get("doc_type") or None
    max_results = arguments.get("max_results", 20)

    db = _get_db()
    results = db.search(
        brain_name=brain_name,
        query_text=query_text,
        doc_type=doc_type,
        size=max_results,
    )

    if not results:
        return "No results found."

    lines = [
        f"Found {len(results)} result(s) for '{query_text}' in brain '{brain_name}':"
    ]
    for i, r in enumerate(results, 1):
        entity_id = r.get("entity_id", "?")
        dt = r.get("doc_type", "?")
        data = r.get("data", {})
        lines.append(f"\n--- Result {i} ---")
        lines.append(f"  entity_id: {entity_id}")
        lines.append(f"  doc_type:  {dt}")
        lines.append(f"  data:      {json.dumps(data)}")
    return "\n".join(lines)


async def _tool_fetch_entity(arguments: dict) -> str:
    brain_name = arguments["brain_name"]
    entity_id = arguments["entity_id"]

    db = _get_db()
    entity = db.get_entity(brain_name, entity_id)
    if not entity:
        return f"Entity '{entity_id}' not found in brain '{brain_name}'."
    return json.dumps(entity, indent=2)


# ---------------------------------------------------------------------------
# Handler registration (must be before Server construction)
# ---------------------------------------------------------------------------


async def _on_list_tools(request: Any, params: Any) -> ListToolsResult:
    return ListToolsResult(tools=TOOLS)


async def _on_call_tool(request: Any, params: CallToolRequestParams) -> CallToolResult:
    name = params.name
    arguments = params.arguments or {}

    try:
        if name == "brain_structure":
            text = await _tool_brain_structure(arguments)
        elif name == "search_brain":
            text = await _tool_search_brain(arguments)
        elif name == "fetch_entity":
            text = await _tool_fetch_entity(arguments)
        else:
            text = f"Unknown tool: {name}"
    except Exception as exc:
        return CallToolResult(
            content=[TextContent(type="text", text=f"Error: {exc}")],
        )

    return CallToolResult(
        content=[TextContent(type="text", text=text)],
    )


# ---------------------------------------------------------------------------
# Server construction
# ---------------------------------------------------------------------------

server = Server(
    name="droid-brain",
    version="0.1.0",
    title="Droid Brain MCP Server",
    description="Query structured organisational knowledge stored in a Droid Brain.",
    on_list_tools=_on_list_tools,
    on_call_tool=_on_call_tool,
)

# ---------------------------------------------------------------------------
# Runner helpers
# ---------------------------------------------------------------------------


async def _run_stdio() -> None:
    """Run the server over stdio transport."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


async def _run_sse(port: int = 8000) -> None:
    """Run the server over SSE transport using uvicorn."""
    import uvicorn

    app = server.streamable_http_app()
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server_uvicorn = uvicorn.Server(config)
    await server_uvicorn.serve()


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point for `droid-brain-mcp` console script."""
    global _DB_URL

    parser = argparse.ArgumentParser(description="Droid Brain MCP Server")
    parser.add_argument(
        "--opensearch-url",
        default=OPENSEARCH_URL,
        help="OpenSearch URL (default: http://localhost:9200)",
    )
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "sse"],
        help="Transport mode (default: stdio)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for SSE transport (default: 8000)",
    )
    args = parser.parse_args()

    _DB_URL = args.opensearch_url

    if args.transport == "sse":
        asyncio.run(_run_sse(port=args.port))
    else:
        asyncio.run(_run_stdio())


if __name__ == "__main__":
    main()
