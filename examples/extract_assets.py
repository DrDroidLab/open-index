"""Demo: extract assets from fake MCP servers into a Droid Brain.

This script:
  1. Registers MCP tool handlers from fake MCP servers
  2. Defines connectors — mappings from MCP tools → brain doc_types
  3. Extracts assets directly (in-process, no subprocess needed)
  4. Shows the populated brain structure

Run: python examples/extract_assets.py
"""

from __future__ import annotations

import asyncio
import sys

# Add droid_brain to path
sys.path.insert(0, ".")

from droid_brain.core import DroidBrain
from droid_brain.connectors import extract_from_handlers
from examples.github_mcp import list_repos
from examples.infra_mcp import list_services, list_dashboards


# Register all available MCP tool handlers
TOOL_HANDLERS = {
    "github.list_repos": list_repos,
    "infra.list_services": list_services,
    "infra.list_dashboards": list_dashboards,
}

# Define connectors: MCP tool → brain doc_type with field mapping
CONNECTORS = [
    {
        "tool": "github.list_repos",
        "brain": "infra_brain",
        "doc_type": "repository",
        "arguments": {},
        "field_mapping": {
            "name": "name",
            "org": "org",
            "language": "language",
            "stars": "stars",
            "description": "description",
            "team": "team",
            "deployment": "deployment",
        },
        # Transform: rename + add org prefix
        "transform": '{"name": item["org"] + "/" + item["name"], '
                     '"org": item["org"], '
                     '"language": item["language"], '
                     '"stars": item["stars"], '
                     '"description": item["description"], '
                     '"team": item["team"], '
                     '"deployment": item["deployment"]}',
    },
    {
        "tool": "infra.list_services",
        "brain": "infra_brain",
        "doc_type": "service",
        "arguments": {},
        "field_mapping": {
            "name": "name",
            "tier": "tier",
            "team": "team",
            "p99_latency_ms": "p99_latency_ms",
            "error_rate_pct": "error_rate_pct",
            "instance_count": "instance_count",
            "dependencies": "dependencies",
        },
    },
    {
        "tool": "infra.list_dashboards",
        "brain": "infra_brain",
        "doc_type": "dashboard",
        "arguments": {},
        "field_mapping": {
            "title": "title",
            "url": "url",
            "service": "linked_service",
            "panels": "panels",
        },
    },
]


async def main():
    db = DroidBrain()
    brain_name = "infra_brain"

    # Clean slate
    brains = {b["name"] for b in db.list_brains()}
    if brain_name in brains:
        db.delete_brain(brain_name)

    db.create_brain(brain_name, "Extracted infrastructure brain — repos, services, dashboards")

    total = 0
    for cfg in CONNECTORS:
        handler = TOOL_HANDLERS[cfg["tool"]]
        result = await extract_from_handlers(
            tool_handlers={cfg["tool"]: handler},
            tool_name=cfg["tool"],
            arguments=cfg.get("arguments", {}),
            brain_name=cfg["brain"],
            doc_type=cfg["doc_type"],
            field_mapping=cfg["field_mapping"],
            transform=cfg.get("transform"),
        )
        print(f"  ✅ {cfg['doc_type']}: {result['entities_created']} entities")
        total += result["entities_created"]

    print(f"\n🎉 Extracted {total} entities into '{brain_name}'.")

    # Show structure
    s = db.get_brain_structure(brain_name)
    print(f"\nBrain structure:")
    for dt in s.doc_types:
        print(f"  📁 {dt['name']} ({dt['entity_count']} entities)")
        for ex in dt["examples"]:
            print(f"     {ex}")

    print(f"\nSearch test:")
    results = db.search(brain_name, "gateway")
    for r in results:
        print(f"  🔹 {r['entity_id'][:8]} [{r['doc_type']}] {r['data'].get('name', r['data'].get('title', '?'))}")

    db.close()


if __name__ == "__main__":
    asyncio.run(main())
