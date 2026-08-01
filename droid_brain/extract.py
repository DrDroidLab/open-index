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


def _dig(item: dict[str, Any], path: str, default: Any = None) -> Any:
    """Fetch a dotted path ('endpoint.host') from a nested dict; ``default`` if absent."""
    value: Any = item
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return default
        value = value[part]
    return value


class ItemSkipped(ValueError):
    """An item cannot become an entity (no usable name) and is skipped."""


def transform_item(item: dict[str, Any], spec: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Turn one tool-result item into (entity_name, entity_data).

    Missing ``fields`` paths become None (real-world tools have optional
    fields); a missing/empty ``name_field`` skips the item.
    """
    name = _dig(item, spec["name_field"])
    if not isinstance(name, str) or not name.strip():
        raise ItemSkipped(
            f"name_field {spec['name_field']!r} did not resolve to a non-empty string "
            f"in item {json.dumps(item)[:120]}"
        )
    if "fields" in spec:
        data = {target: _dig(item, path) for target, path in spec["fields"].items()}
    else:
        data = dict(item)
    data.update(spec.get("constants") or {})
    return name, data


def _parse_items(result_text: str, spec: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        result = json.loads(result_text)
    except json.JSONDecodeError as e:
        raise ValueError(f"tool result is not valid JSON: {e}") from None
    if isinstance(result, list):
        items = result
    else:
        items_path = spec.get("items_path")
        if not items_path:
            raise ValueError(
                "tool result is a JSON object; set 'items_path' in the tool spec to locate the list"
            )
        items = _dig(result, items_path)
    if not isinstance(items, list):
        raise ValueError(f"items at {spec.get('items_path')!r} are not a list")
    if not all(isinstance(item, dict) for item in items):
        raise ValueError("tool result items must be JSON objects")
    return items


def _result_text(result: Any) -> str:
    """Concatenate text from a tool result's content items."""
    texts = [getattr(item, "text", None) for item in (result.content or [])]
    texts = [t for t in texts if t]
    if not texts:
        raise ValueError("tool returned no text content to extract from")
    return "\n".join(texts)


def _validate_source(source: dict[str, Any], index: int) -> None:
    name = source.get("name", f"#{index}")
    command = source.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(c, str) for c in command):
        raise ValueError(f"source {name!r}: 'command' must be a non-empty list of strings")
    tools = source.get("tools")
    if not isinstance(tools, list) or not tools:
        raise ValueError(f"source {name!r}: 'tools' must be a non-empty list")
    for spec in tools:
        if not isinstance(spec, dict):
            raise ValueError(f"source {name!r}: each tool spec must be an object")
        for key in ("tool", "doc_type", "name_field"):
            if not spec.get(key):
                raise ValueError(f"source {name!r}: tool spec is missing {key!r}")
        if "fields" in spec and not isinstance(spec["fields"], dict):
            raise ValueError(f"source {name!r}: 'fields' must be an object of target: dotted_path")


def _unwrap(exc: BaseException) -> BaseException:
    """Unwrap anyio task-group wrapping to the underlying error."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


async def _extract_source(brain: Brain, source: dict[str, Any], summary: dict[str, Any]) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    command = source["command"]
    params = StdioServerParameters(command=command[0], args=command[1:], env=dict(os.environ))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            available = {t.name for t in (await session.list_tools()).tools}
            for spec in source["tools"]:
                if spec["tool"] not in available:
                    raise ValueError(
                        f"server has no tool {spec['tool']!r} (available: {', '.join(sorted(available))})"
                    )
                result = await session.call_tool(spec["tool"], spec.get("arguments") or {})
                if result.is_error:
                    raise ValueError(f"tool {spec['tool']!r} failed: {_result_text(result)}")
                items = _parse_items(_result_text(result), spec)

                doc_type = spec["doc_type"]
                if not brain.doc_type_exists(doc_type):
                    try:
                        brain.create_doc_type(
                            doc_type,
                            description=spec.get("doc_type_description", f"Extracted from {source['name']}:{spec['tool']}"),
                            boost=spec.get("boost", 1.0),
                            schema=spec.get("schema"),
                        )
                    except ValueError as e:
                        if "already exists" not in str(e):  # created concurrently
                            raise
                for item in items:
                    try:
                        name, data = transform_item(item, spec)
                    except ItemSkipped:
                        summary["skipped"] += 1
                        continue
                    brain.upsert_entity(doc_type, name, data)
                    summary["entities"] += 1
                    summary["by_doc_type"][doc_type] = summary["by_doc_type"].get(doc_type, 0) + 1


async def extract_async(brain: Brain, sources: list[dict[str, Any]]) -> dict[str, Any]:
    for index, source in enumerate(sources):
        _validate_source(source, index)

    summary: dict[str, Any] = {"sources": 0, "entities": 0, "skipped": 0, "by_doc_type": {}, "errors": []}
    for source in sources:
        try:
            await _extract_source(brain, source, summary)
            summary["sources"] += 1
        except Exception as e:
            # One failing server must not lose the others; entities already
            # extracted stay committed. Errors surface in the summary/CLI.
            summary["errors"].append(f"{source['name']}: {_unwrap(e)}")
    return summary


def extract(brain: Brain, sources: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        return asyncio.run(extract_async(brain, sources))
    except BaseExceptionGroup as group:
        raise _unwrap(group) from None
