"""Extract entities into a brain from MCP servers ("connectors").

Given a set of sources — an MCP server command plus a list of tool specs —
this calls each tool, transforms the returned items, and upserts them as
entities in the brain. Transformations available per tool spec:

    name_field   dotted path in each item to use as the entity name (required)
    fields       optional {entity_field: dotted_path_in_item} remapping;
                 omitted -> the whole item is stored (nested JSON kept as-is)
    constants    optional {field: value} added to every extracted entity
    items_path   dotted path to the list when the tool result is an object
    arguments    optional tool call arguments
    doc_type_description / boost / schema   used when the doc_type is auto-created

Run it with: droid-brain extract <brain> <config.json>
or against the bundled fake servers: droid-brain extract <brain> --demo
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

from .store import Brain

DEMO_SOURCES: list[dict[str, Any]] = [
    {
        "name": "grafana",
        "command": [sys.executable, "-m", "droid_brain.demo_servers", "grafana"],
        "tools": [
            {
                "tool": "list_dashboards",
                "doc_type": "dashboard",
                "doc_type_description": "A monitoring dashboard and the panels it contains.",
                "name_field": "title",
                "fields": {
                    "url": "url",
                    "folder": "folder",
                    "owner": "owner",
                    "panels": "panels",
                },
                "constants": {"source": "grafana"},
            }
        ],
    },
    {
        "name": "github",
        "command": [sys.executable, "-m", "droid_brain.demo_servers", "github"],
        "tools": [
            {
                "tool": "list_repositories",
                "doc_type": "repository",
                "doc_type_description": "A code repository: ownership, stack and CI status.",
                "boost": 1.5,
                "name_field": "name",
                "fields": {
                    "description": "description",
                    "url": "url",
                    "team": "team",
                    "language": "language",
                    "topics": "topics",
                    "ci": "ci",
                },
                "constants": {"source": "github"},
            }
        ],
    },
    {
        "name": "aws",
        "command": [sys.executable, "-m", "droid_brain.demo_servers", "aws"],
        "tools": [
            {
                "tool": "list_databases",
                "doc_type": "database",
                "doc_type_description": "A database instance: engine, endpoint and replicas.",
                "name_field": "identifier",
                "constants": {"source": "aws"},
            }
        ],
    },
]


def _dig(item: dict[str, Any], path: str) -> Any:
    """Fetch a dotted path ('endpoint.host') from a nested dict."""
    value: Any = item
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            raise ValueError(f"field {path!r} not found in item {json.dumps(item)[:120]}")
        value = value[part]
    return value


def transform_item(item: dict[str, Any], spec: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Turn one tool-result item into (entity_name, entity_data)."""
    name = _dig(item, spec["name_field"])
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"name_field {spec['name_field']!r} did not resolve to a non-empty string")
    if "fields" in spec:
        data = {target: _dig(item, path) for target, path in spec["fields"].items()}
    else:
        data = dict(item)
    data.update(spec.get("constants") or {})
    return name, data


def _parse_items(result_text: str, spec: dict[str, Any]) -> list[dict[str, Any]]:
    result = json.loads(result_text)
    if isinstance(result, list):
        items = result
    elif isinstance(result, dict) and spec.get("items_path"):
        items = _dig(result, spec["items_path"])
    else:
        raise ValueError(
            "tool result is a JSON object; set 'items_path' in the tool spec to locate the list"
        )
    if not isinstance(items, list):
        raise ValueError(f"items at {spec.get('items_path')!r} are not a list")
    return items


async def extract_async(brain: Brain, sources: list[dict[str, Any]]) -> dict[str, Any]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    summary: dict[str, Any] = {"sources": 0, "entities": 0, "by_doc_type": {}}

    for source in sources:
        command = source["command"]
        params = StdioServerParameters(
            command=command[0], args=command[1:], env=dict(os.environ)
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                available = {t.name for t in (await session.list_tools()).tools}
                for spec in source.get("tools", []):
                    if spec["tool"] not in available:
                        raise ValueError(
                            f"server {source['name']!r} has no tool {spec['tool']!r} "
                            f"(available: {', '.join(sorted(available))})"
                        )
                    result = await session.call_tool(spec["tool"], spec.get("arguments") or {})
                    if result.is_error:
                        raise ValueError(
                            f"tool {spec['tool']!r} on {source['name']!r} failed: {result.content[0].text}"
                        )
                    items = _parse_items(result.content[0].text, spec)

                    doc_type = spec["doc_type"]
                    if not brain.doc_type_exists(doc_type):
                        brain.create_doc_type(
                            doc_type,
                            description=spec.get("doc_type_description", f"Extracted from {source['name']}:{spec['tool']}"),
                            boost=spec.get("boost", 1.0),
                            schema=spec.get("schema"),
                        )
                    for item in items:
                        name, data = transform_item(item, spec)
                        brain.upsert_entity(doc_type, name, data)
                        summary["entities"] += 1
                        summary["by_doc_type"][doc_type] = summary["by_doc_type"].get(doc_type, 0) + 1
        summary["sources"] += 1
    return summary


def extract(brain: Brain, sources: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        return asyncio.run(extract_async(brain, sources))
    except BaseExceptionGroup as group:
        # anyio task groups wrap errors raised inside client sessions; unwrap
        # to the underlying error for a clean message.
        exc: BaseException = group
        while isinstance(exc, BaseExceptionGroup):
            exc = exc.exceptions[0]
        raise exc from None
