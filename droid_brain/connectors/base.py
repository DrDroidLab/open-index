"""The Connector base class.

A connector is a small, user-authored script that turns data from some source
(an MCP server, an API, a file) into entities in the brain. The pattern mirrors
the reference metadata-extractors: subclass `Connector`, implement one or more
`extract_*()` methods; the runner auto-discovers and runs them. Each method
`yield`s `EntitySpec`s (or dicts), which the runner maps to typed entities —
including their `related_to` edges — and upserts.

Minimal example::

    class MyConnector(Connector):
        name = "linear-issues"

        def extract_issues(self):
            for item in self.paginate("list_issues", result_key="issues"):
                yield EntitySpec(
                    doc_type="issue",
                    id=f"issue:{item['id']}",
                    name=item["title"],
                    fields={"status": item["state"]},
                    related_to=[(f"product:{item['project']}", "belongs to product")],
                )

`self.mcp` is an initialized `McpClient` when the connector declares an
`mcp_url` (or one is supplied by the runner); otherwise it is None.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from droid_brain.connectors.mcp_client import McpClient, extract_json
from droid_brain.models import Entity, Relationship


@dataclass
class EntitySpec:
    """A connector's declaration of one entity to create."""

    doc_type: str
    id: str
    name: str = ""
    fields: dict[str, Any] = field(default_factory=dict)
    # Edges as (target_id, relationship_edge_meaning) tuples.
    related_to: list[tuple[str, str]] = field(default_factory=list)

    def to_entity(self) -> Entity:
        return Entity(
            id=self.id,
            doc_type=self.doc_type,
            name=self.name,
            fields=dict(self.fields),
            related_to=[
                Relationship(target=t, relationship_edge_meaning=m)
                for (t, m) in self.related_to
            ],
        )


class Connector:
    """Base class for ingestion scripts."""

    # Human-readable connector name (used by `droid-brain ingest <name>`).
    name: str = "connector"
    # If set, the runner opens an McpClient to this URL and assigns self.mcp.
    # May reference env vars: "${MY_MCP_URL}".
    mcp_url: Optional[str] = None
    # Auth headers for the MCP server, e.g. {"Authorization": "Bearer ${TOKEN}"}.
    mcp_auth_headers: Optional[dict[str, str]] = None
    # When `droid-brain run` should auto-run this connector: "manual" (default),
    # "hourly"/"daily"/"weekly", or an interval like "6h"/"30m"/"1d"/"1w".
    schedule: str = "manual"

    def __init__(self, mcp: Optional[McpClient] = None, **config: Any):
        self.mcp = mcp
        self.config = config

    # -- helpers connectors can use --------------------------------------------

    def paginate(
        self,
        tool_name: str,
        arguments: Optional[dict] = None,
        result_key: Optional[str] = None,
        cursor_key: str = "cursor",
        max_results: int = 1000,
    ) -> list[Any]:
        """Call an MCP tool repeatedly, following cursor pagination.

        Accumulates records from each page's content until no cursor remains or
        `max_results` is reached. Mirrors the reference `_call_tool_with_pagination`.
        """
        if self.mcp is None:
            raise RuntimeError(f"{self.name}: paginate() needs an MCP client (set mcp_url)")

        results: list[Any] = []
        args = dict(arguments or {})
        cursor: Optional[str] = None

        while len(results) < max_results:
            if cursor:
                args[cursor_key] = cursor
            response = self.mcp.call_tool(tool_name, args)
            content = response.get("content", [])
            records = extract_json(content)

            next_cursor = None
            for rec in records:
                if isinstance(rec, dict) and result_key and result_key in rec:
                    items = rec[result_key]
                    results.extend(items if isinstance(items, list) else [items])
                    next_cursor = rec.get(cursor_key)
                else:
                    results.append(rec)
            cursor = next_cursor
            if not cursor:
                break

        return results[:max_results]

    def call(self, tool_name: str, arguments: Optional[dict] = None) -> list[Any]:
        """Single tool call → flattened JSON records."""
        if self.mcp is None:
            raise RuntimeError(f"{self.name}: call() needs an MCP client (set mcp_url)")
        response = self.mcp.call_tool(tool_name, arguments or {})
        return extract_json(response.get("content", []))

    # -- extractor discovery ---------------------------------------------------

    def extractor_methods(self) -> list[str]:
        """Names of this connector's `extract_*` methods (auto-discovered)."""
        return sorted(
            m
            for m in dir(self)
            if m.startswith("extract_") and callable(getattr(self, m))
        )

    def run(self) -> Iterable[EntitySpec]:
        """Run every extract_* method, yielding all EntitySpecs."""
        for method_name in self.extractor_methods():
            method = getattr(self, method_name)
            produced = method()
            if produced is None:
                continue
            for spec in produced:
                if isinstance(spec, EntitySpec):
                    yield spec
                elif isinstance(spec, dict):
                    yield _spec_from_dict(spec)


def _spec_from_dict(d: dict) -> EntitySpec:
    related = []
    for r in d.get("related_to", []) or []:
        if isinstance(r, (list, tuple)):
            related.append((r[0], r[1] if len(r) > 1 else ""))
        elif isinstance(r, dict):
            related.append((r["target"], r.get("relationship_edge_meaning", "")))
    return EntitySpec(
        doc_type=d["doc_type"],
        id=d["id"],
        name=d.get("name", ""),
        fields=d.get("fields", {}),
        related_to=related,
    )
