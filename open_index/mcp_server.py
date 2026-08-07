"""MCP server exposing the brain to an agent — read AND write.

Read (orient + query):
    navigation_guidelines() — a markdown guide to what this brain knows and how
                              to search/write it (the `build_navigation_guidelines`
                              pattern). Call this first.
    search_brain(query, doc_types, limit) — search/filter entities.
    get_entity(id) — one entity with its relationships.

Write (grow the brain — this is what makes it continuously improving):
    put_entity(...) — add/update an entity (validated, persisted to disk).
    create_doc_type(...) — define a new concept.

Run with `open-index mcp --brain <dir>`; speaks MCP over stdio, so an agent like
Claude Code can both read context from the brain and write learnings back to it
through the same connection.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from open_index.brain import Brain
from open_index.models import Entity, Relationship
from open_index.schema import DocType, DocTypeDisplay, FieldSpec


def _load_server_class():
    """Return the MCP server class across SDK versions.

    mcp >= 2.0 exposes it as `mcp.server.MCPServer`; mcp 1.x as
    `mcp.server.fastmcp.FastMCP`. Both share the `.tool()` decorator and
    `.run()` API used below.
    """
    try:
        from mcp.server import MCPServer  # mcp >= 2.0

        return MCPServer
    except ImportError:
        pass
    try:
        from mcp.server.fastmcp import FastMCP  # mcp 1.x

        return FastMCP
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise SystemExit(
            "the MCP server needs the 'mcp' package: pip install 'open-index[mcp]'"
        ) from exc


def build_server(brain: Brain, read_only: bool = False):
    """Construct an MCP server bound to an open brain.

    read_only=True registers only the read tools (navigation_guidelines,
    search_brain, get_entity) — no put_entity / create_doc_type. Use it for a
    public/shared endpoint that agents may query but not mutate; run a separate
    authenticated read+write endpoint for writers."""
    server_cls = _load_server_class()
    suffix = " (read-only)" if read_only else ""
    server = server_cls(f"open-index:{brain.config.name}{suffix}")

    # ---- read ------------------------------------------------------------- #

    @server.tool()
    def navigation_guidelines() -> str:
        """Orient yourself in this brain. Returns a markdown guide: every
        doc_type, its counts, searchable fields, example entities, and how to
        search and write. CALL THIS FIRST before searching or writing."""
        return brain.navigation_guidelines()

    @server.tool()
    def search_brain(
        query: Optional[str] = None,
        doc_types: Optional[list[str]] = None,
        limit: int = 20,
    ) -> str:
        """Search the brain. `query` is free text; `doc_types` optionally filters
        to specific concepts (e.g. ["product", "issue"]). Returns matching
        entities ranked by relevance, plus per-doc_type counts."""
        results = brain.search(query=query, doc_types=doc_types, limit=limit)
        return json.dumps(
            {
                "total": results.total,
                "doc_type_counts": results.doc_type_counts,
                "results": results.results,
            },
            indent=2,
        )

    @server.tool()
    def get_entity(entity_id: str) -> str:
        """Fetch a single entity by id (e.g. "product:checkout"), including its
        outgoing and incoming relationships with their edge meanings."""
        entity = brain.get_entity(entity_id)
        if entity is None:
            return json.dumps({"error": f"no entity '{entity_id}'"})
        payload = entity.to_json()
        payload["relationships"] = {
            "outgoing": [
                {"target": t, "meaning": m}
                for (_s, t, m) in brain.backend.relationships_from(entity_id)
            ],
            "incoming": [
                {"source": s, "meaning": m}
                for (s, _t, m) in brain.backend.relationships_to(entity_id)
            ],
        }
        return json.dumps(payload, indent=2)

    # ---- write ------------------------------------------------------------ #

    if read_only:
        return server

    @server.tool()
    def put_entity(
        doc_type: str,
        id: str,
        name: str = "",
        fields: Optional[dict[str, Any]] = None,
        related_to: Optional[list[dict[str, str]]] = None,
    ) -> str:
        """Add or update an entity in the brain (validated + persisted to disk).

        `id` must look like "<doc_type>:<slug>" (e.g. "issue:payment-declined").
        `fields` are the schema fields for the doc_type. `related_to` is a list of
        {"target": "<entity_id>", "relationship_edge_meaning": "<free text>"}
        edges, e.g. {"target": "product:checkout", "relationship_edge_meaning":
        "has common issue"}. Use this to record new knowledge or corrections."""
        if brain.config.doc_type(doc_type) is None:
            return json.dumps(
                {"error": f"unknown doc_type '{doc_type}'. Create it first with "
                          "create_doc_type, or use an existing one: "
                          f"{list(brain.config.doc_types)}"}
            )
        entity = Entity(
            id=id,
            doc_type=doc_type,
            name=name,
            fields=fields or {},
            related_to=[
                Relationship(
                    target=r["target"],
                    relationship_edge_meaning=r.get("relationship_edge_meaning", ""),
                )
                for r in (related_to or [])
            ],
        )
        try:
            path = brain.put_entity(entity)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        # path is None for index-backed types (DB is the source of truth).
        return json.dumps({"ok": True, "id": entity.id, "path": str(path) if path else None})

    @server.tool()
    def create_doc_type(
        doc_type: str,
        description: str = "",
        fields: Optional[list[dict[str, Any]]] = None,
        color: str = "#6b7280",
        label_field: str = "name",
        storage: str = "index",
    ) -> str:
        """Define a new doc_type (concept) in the brain, persisted to
        doc_types/<name>.yaml. `fields` is a list of field specs, each like
        {"name": "severity", "type": "string", "processing": "keyword",
        "search": "syntactic", "boost": 2}. `storage` is "index" (DB is the
        source of truth — default, for generated/high-volume data) or "file"
        (JSON entities tracked in git — for curated data). Only create a new
        doc_type when no existing one fits — check navigation_guidelines first."""
        try:
            dt = DocType(
                doc_type=doc_type,
                description=description,
                storage=storage,
                display=DocTypeDisplay(label_field=label_field, color=color),
                fields=[FieldSpec.model_validate(f) for f in (fields or [])],
            )
            path = brain.create_doc_type(dt)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        return json.dumps({"ok": True, "doc_type": doc_type, "path": str(path)})

    return server


def serve(brain_dir: str, read_only: bool = False) -> None:
    """Run the MCP server over stdio (for local agents / Claude Code)."""
    brain = Brain.open(brain_dir)
    server = build_server(brain, read_only=read_only)
    server.run()


def _bearer_auth_middleware(app, token: str):
    """Wrap an ASGI app so every HTTP request must carry `Authorization: Bearer
    <token>`. Minimal gate for a networked, writable endpoint."""
    expected = f"Bearer {token}"

    async def wrapped(scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            if headers.get(b"authorization", b"").decode() != expected:
                await send({
                    "type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"text/plain")],
                })
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await app(scope, receive, send)

    return wrapped


def serve_http(
    brain_dir: str, host: str = "0.0.0.0", port: int = 8080,
    token: Optional[str] = None, read_only: bool = False,
) -> None:
    """Run the MCP server over streamable HTTP so remote/cloud agents can connect
    by URL. Register `http://<host>:<port>/mcp` as a remote MCP server in the
    agent (with `Authorization: Bearer <token>` if a token is set).

    This is the production shape — pair it with the OpenSearch backend for a
    shared, multi-writer brain."""
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise SystemExit(
            "the HTTP endpoint needs uvicorn: pip install 'open-index[serve]'"
        ) from exc

    brain = Brain.open(brain_dir)
    server = build_server(brain, read_only=read_only)
    app = server.streamable_http_app()
    if token:
        app = _bearer_auth_middleware(app, token)
    else:
        import sys
        print(
            "WARNING: serving with no --token; the writable endpoint is unauthenticated. "
            "Set OPEN_INDEX_TOKEN or --token for anything networked.",
            file=sys.stderr,
        )
    uvicorn.run(app, host=host, port=port)
