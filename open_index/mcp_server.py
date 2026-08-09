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

Run with `open-index mcp --brain <dir>`; it speaks MCP over stdio so a
domain-specialized agent can both read context and write durable knowledge back
through the same connection.
"""

from __future__ import annotations

import json
import inspect
from time import perf_counter
from typing import Any, Optional

from pathlib import Path

from open_index.brain import Brain
from open_index.schema import DocType, DocTypeDisplay, FieldSpec, RelationshipSpec
from open_index.models import Entity, Provenance, Relationship


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

    Read+write is the default. read_only=True registers only the read tools (navigation_guidelines,
    search_brain, get_entity) — no put_entity / create_doc_type. Use it for a
    public/shared endpoint that agents may query but not mutate; run a separate
    authenticated read+write endpoint for writers."""
    server_cls = _load_server_class()
    suffix = " (read-only)" if read_only else ""
    name = f"open-index:{brain.config.name}{suffix}"
    # MCP clients that honor server instructions inject this domain context
    # prompt before the first turn, avoiding an orientation tool call.
    guide_started = perf_counter()
    guide = brain.navigation_guidelines(source=None, include_writes=not read_only)
    if "instructions" in inspect.signature(server_cls).parameters:
        server = server_cls(name, instructions=guide)
        # Record delivery only when this SDK can actually publish instructions.
        brain.record_fetch(
            source="prompt", operation="navigation_guidelines",
            started=guide_started, result_count=sum(brain.counts().values()),
            result_doc_types=brain.counts(),
        )
    else:
        server = server_cls(name)

    # ---- read ------------------------------------------------------------- #

    @server.tool()
    def navigation_guidelines() -> str:
        """Refresh this brain's domain context instructions — the complete guide
        to this brain; you should not need any other documentation.

        Returns markdown covering the doc_type/entity/relationship model, the
        entity id convention, every doc_type with its full field schema and
        relationship vocabulary, example entities, and worked `put_entity` /
        `put_entities` / `create_doc_type` calls with the schema vocabulary.

        MCP hosts pre-inject this through server instructions; call it after the
        index changes, or when the host does not support server instructions."""
        return brain.navigation_guidelines(source="mcp", include_writes=not read_only)

    @server.tool()
    def search_brain(
        query: Optional[str] = None,
        doc_types: Optional[list[str]] = None,
        limit: int = 20,
    ) -> str:
        """Search the brain. `query` is free text; `doc_types` optionally filters
        to specific concepts (e.g. ["product", "issue"]). Returns matching
        entities ranked by relevance, plus per-doc_type counts."""
        results = brain.search(
            query=query, doc_types=doc_types, limit=limit, source="mcp"
        )
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
        entity = brain.get_entity(entity_id, source="mcp")
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
        # dict[str, Any], not dict[str, str]: an edge may carry a nested
        # "provenance" object. The MCP SDK builds this tool's input schema from
        # these annotations, so a str-valued hint makes the server reject
        # per-edge provenance before the body ever runs.
        related_to: Optional[list[dict[str, Any]]] = None,
        provenance: Optional[dict[str, Any]] = None,
        valid_from: Optional[str] = None,
        valid_to: Optional[str] = None,
    ) -> str:
        """Add or update an entity (an upsert — the same id replaces it).

        Args:
            doc_type: An EXISTING doc_type. Call navigation_guidelines() to see
                them; create_doc_type() first if none fits.
            id: Must be "<doc_type>:<slug>", e.g. "issue:payment-declined". The
                prefix has to match `doc_type`. Chars: a-z A-Z 0-9 . _ -
            name: Human-readable label.
            fields: The doc_type's schema fields, e.g.
                {"severity": "high", "status": "open"}. Do NOT put id/doc_type/
                name/related_to in here — they are separate arguments.
            related_to: Edges to other entities, each
                {"target": "<entity_id>", "relationship_edge_meaning": "<text>"}.
                Prefer a meaning the doc_type already declares or uses. Targets
                may not exist yet. Always set these — the edges are the point.
                Each edge may carry its own "provenance" block: a well-attributed
                entity can still carry a guessed edge.
            provenance: Where the claim came from and how far to trust it —
                {"asserted_by": "agent:<name>", "asserted_at": "<ISO-8601>",
                 "confidence": 0.0-1.0, "evidence": "<what justified it, verbatim>"}.
                Supply it whenever you INFER something rather than reading it
                directly, and set `confidence` honestly — readers filter on it,
                and an unscored claim is treated as untrusted, not certain.
            valid_from / valid_to: When the claim is true OF THE WORLD, which is
                not when you asserted it. A memory limit that was 25Gi until a
                deploy has `valid_to` at the deploy, while the assertion about it
                is `asserted_at` now. Omit both if the claim has no time bound.

        Whether a JSON file is written follows the doc_type's `storage` policy
        ("file" writes one, "index" keeps it in the DB); you don't choose."""
        dt = brain.config.doc_type(doc_type)
        if dt is None:
            known = list(brain.config.doc_types)
            return json.dumps({
                "error": f"unknown doc_type '{doc_type}'",
                "known_doc_types": known,
                "hint": (
                    "Use one of known_doc_types, or define this concept first with "
                    "create_doc_type(...)."
                    if known else
                    "This brain has no doc_types yet. Define one with create_doc_type(...) "
                    "before adding entities."
                ),
            })

        expected_prefix = f"{doc_type}:"
        if not id.startswith(expected_prefix):
            return json.dumps({
                "error": f"id '{id}' does not match doc_type '{doc_type}'",
                "hint": f"ids are '<doc_type>:<slug>' — use '{expected_prefix}<slug>'.",
            })

        # Build provenance first, so a malformed attribution block is reported as
        # such instead of surfacing as a confusing schema-field error below. A
        # claim that looks attributed but is not is worse than a plainly
        # unattributed one, so this must fail loudly rather than drop silently.
        try:
            entity_provenance = Provenance(**provenance) if provenance else None
            edge_provenance = [
                Provenance(**r["provenance"])
                if isinstance(r.get("provenance"), dict) else None
                for r in (related_to or [])
            ]
        except (TypeError, ValueError) as exc:
            return json.dumps({
                "error": f"invalid provenance: {exc}",
                "hint": '{"asserted_by": "agent:<name>", "asserted_at": "<ISO-8601>", '
                        '"confidence": 0.0-1.0, "evidence": "<what justified it>"}',
            })

        try:
            entity = Entity(
                id=id,
                doc_type=doc_type,
                name=name,
                fields=fields or {},
                related_to=[
                    Relationship(
                        target=r["target"],
                        relationship_edge_meaning=r.get("relationship_edge_meaning", ""),
                        # Per-edge provenance: a well-attributed entity can still
                        # carry a guessed edge, and the two need separate trust.
                        provenance=p,
                    )
                    for r, p in zip(related_to or [], edge_provenance)
                ],
                provenance=entity_provenance,
                valid_from=valid_from,
                valid_to=valid_to,
            )
            path = brain.put_entity(entity)
        except KeyError:
            return json.dumps({
                "error": "each related_to entry needs a 'target'",
                "hint": '[{"target": "product:checkout", '
                        '"relationship_edge_meaning": "affects"}]',
            })
        except ValueError as exc:  # includes pydantic validation errors
            return json.dumps({
                "error": str(exc),
                "known_fields": [f.name for f in dt.fields],
                "hint": "See navigation_guidelines() for this doc_type's schema.",
            })

        # path is None for index-backed types (DB is the source of truth).
        return json.dumps({"ok": True, "id": entity.id, "path": str(path) if path else None})

    @server.tool()
    def put_entities(
        entities: list[dict[str, Any]],
        provenance: Optional[dict[str, Any]] = None,
    ) -> str:
        """Add or update MANY entities in one call. Use this instead of calling
        put_entity repeatedly — importing a list, backfilling from a document,
        or recording a batch of findings.

        Args:
            entities: A list of entity objects, each shaped like a put_entity
                call: {"doc_type": "...", "id": "<doc_type>:<slug>",
                "name": "...", "fields": {...}, "related_to": [...]}. Fields may
                also be written flat alongside the reserved keys. Each may carry
                its own "provenance", "valid_from" and "valid_to".
            provenance: Attribution applied to every entity that does not carry
                its own — attribute the batch once rather than per row. Same
                shape as put_entity's: {"asserted_by": ..., "asserted_at": ...,
                "confidence": 0.0-1.0, "evidence": ...}.

        A row that fails validation does not abort the rest. The reply reports
        `written`, `failed`, and per-entity `errors`, so check it rather than
        assuming everything landed."""
        if not entities:
            return json.dumps({"ok": True, "written": 0, "failed": 0, "errors": []})

        try:
            shared = Provenance(**provenance) if provenance else None
        except (TypeError, ValueError) as exc:
            return json.dumps({"error": f"invalid provenance: {exc}"})

        # Non-dict elements are rejected by the tool's input schema before this
        # runs (entities is list[dict[str, Any]]), so every raw here is a dict.
        parsed: list[Entity] = []
        errors: list[str] = []
        for i, raw in enumerate(entities):
            doc_type = raw.get("doc_type")
            if doc_type is None:
                errors.append(f"[{i}]: missing 'doc_type'")
                continue
            if brain.config.doc_type(doc_type) is None:
                errors.append(
                    f"[{i}] {raw.get('id', '?')}: unknown doc_type '{doc_type}' "
                    f"(known: {list(brain.config.doc_types)})"
                )
                continue
            try:
                # from_dict so callers may write schema fields flat, matching the
                # entity JSON files on disk.
                parsed.append(Entity.from_dict(raw))
            except (TypeError, ValueError, KeyError) as exc:
                errors.append(f"[{i}] {raw.get('id', '?')}: {exc}")

        result = brain.put_entities(parsed, provenance=shared)
        errors.extend(result.errors)
        return json.dumps({
            "ok": not errors,
            "written": result.written,
            "failed": len(errors),
            "errors": errors[:50],
            "paths": [str(p) for p in result.paths],
        }, indent=2)

    @server.tool()
    def create_doc_type(
        doc_type: str,
        description: str = "",
        fields: Optional[list[dict[str, Any]]] = None,
        relationships: Optional[list[dict[str, str]]] = None,
        color: str = "#6b7280",
        label_field: str = "name",
        storage: str = "index",
    ) -> str:
        """Define a new concept, persisted to doc_types/<name>.yaml.

        Only when no existing doc_type fits — call navigation_guidelines() first;
        reusing a type beats adding a near-duplicate.

        Args:
            doc_type: Singular, lowercase concept name, e.g. "runbook".
            description: What one of these is — shown to future agents.
            fields: Field specs. Only "name" is required in each. Keys:
                name       — field name
                type       — string | text | number | boolean | timestamp
                processing — keyword | text | timestamp  (keyword = exact/filter)
                search     — syntactic | semantic | none
                             (syntactic = keyword match, semantic = vector search)
                boost      — number > 0, default 1. Search weight: a hit in a
                             boost-6 field outranks a boost-1 hit 6-to-1.
                required   — true to reject entities missing it
                Convention: a high-boost `name` field, plus a `description` field
                with search "semantic" so entities are findable by meaning.
            relationships: The edge vocabulary for this type, each
                {"name": "<meaning>", "target_doc_type": "<other_type>"}.
                Makes correlations discoverable and lightly validated.
            storage: "index" (default) — the DB owns these entities, no files
                written; right for generated/high-volume/temporal data.
                "file" — JSON under entities/<doc_type>/ is the git-tracked
                source of truth; right for curated data.
            color: Hex color for this type's nodes on the map.
            label_field: Which field labels a node. Defaults to "name".

        Example:
            create_doc_type(
                doc_type="runbook",
                description="A procedure for handling a known failure.",
                storage="file",
                fields=[
                    {"name": "name", "type": "string", "search": "syntactic", "boost": 6},
                    {"name": "steps", "type": "text", "search": "semantic"},
                ],
                relationships=[{"name": "resolves", "target_doc_type": "alert"}],
            )"""
        try:
            dt = DocType(
                doc_type=doc_type,
                description=description,
                storage=storage,
                display=DocTypeDisplay(label_field=label_field, color=color),
                fields=[FieldSpec.model_validate(f) for f in (fields or [])],
                relationships=[
                    RelationshipSpec.model_validate(r) for r in (relationships or [])
                ],
            )
            path = brain.create_doc_type(dt)
        except ValueError as exc:
            return json.dumps({
                "error": str(exc),
                "hint": (
                    "Check the field spec vocabulary in navigation_guidelines(). "
                    "storage must be 'index' or 'file'; search must be "
                    "'syntactic', 'semantic', or 'none'."
                ),
            })
        return json.dumps({
            "ok": True,
            "doc_type": doc_type,
            "path": str(path),
            "next": f'Add entities with put_entity(doc_type="{doc_type}", '
                    f'id="{doc_type}:<slug>", ...).',
        })

    return server


def serve(brain_dir: str, read_only: bool = False) -> None:
    """Run the MCP server over stdio for a local domain-specialized agent."""
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


def _host_of(url: str) -> Optional[str]:
    """The host[:port] part of a URL, or None if it isn't parseable."""
    from urllib.parse import urlparse

    parsed = urlparse(url if "://" in url else f"http://{url}")
    return parsed.netloc or None


def build_transport_security(
    public_url: Optional[str] = None, allowed_hosts: Optional[list[str]] = None
):
    """Host allow-list for the MCP SDK's DNS-rebinding protection.

    The SDK enables that protection whenever the app is built for a localhost
    bind, with a hardcoded localhost-only allow-list. Anything reaching the
    server through a reverse proxy or load balancer therefore arrives with a
    foreign `Host` header and is rejected with `421 Invalid Host header` — the
    server looks up, and every proxied request fails.

    Rather than switch the protection off (which is what passing a non-localhost
    bind address to the SDK quietly does), state which hosts are legitimate:
    loopback, plus whatever `--public-url` and `--allowed-host` name.

    A literal `*` disables the check — for a brain behind a trusted proxy that
    already validates Host. Returns settings, or None when the SDK's own default
    is appropriate.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    entries = [e.strip() for e in (allowed_hosts or []) if e.strip()]
    if "*" in entries:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)

    if public_url:
        derived = _host_of(public_url)
        if derived:
            entries.append(derived)

    hosts = {"127.0.0.1:*", "localhost:*", "[::1]:*", "127.0.0.1", "localhost", "[::1]"}
    origins = {"http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"}
    for entry in entries:
        bare = entry.split(":", 1)[0] if not entry.startswith("[") else entry
        # Both forms: proxies drop the port when it is the scheme default, so
        # "example.com" and "example.com:8080" must both pass.
        hosts.update({entry, bare, f"{bare}:*"})
        for scheme in ("http", "https"):
            origins.update({f"{scheme}://{entry}", f"{scheme}://{bare}",
                            f"{scheme}://{bare}:*"})

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(hosts),
        allowed_origins=sorted(origins),
    )


def discover_brains(root: str) -> dict[str, "Path"]:
    """Every brain directory directly under `root`, keyed by directory name.

    A "brain" is any subdirectory holding a brain.yaml, so a root can sit
    alongside unrelated folders without confusing anything.
    """
    from pathlib import Path

    base = Path(root).expanduser().resolve()
    if not base.is_dir():
        raise SystemExit(f"no such directory: {base}")
    return {
        child.name: child
        for child in sorted(base.iterdir())
        if (child / "brain.yaml").exists()
    }


def token_for(name: str, default: Optional[str] = None) -> Optional[str]:
    """Per-brain bearer token from the environment, falling back to a shared one.

    `OPEN_INDEX_TOKEN_SALES_EU` gates the brain in `sales-eu/`. Without one, the
    shared `--token` applies — which is fine for a private host and wrong for a
    shared one, hence the per-brain override.
    """
    import os as _os

    key = "OPEN_INDEX_TOKEN_" + name.upper().replace("-", "_").replace(".", "_")
    return _os.environ.get(key) or default


def build_multi_app(
    brains: dict[str, "Path"],
    *,
    token: Optional[str] = None,
    read_only: bool = False,
    public_base_url: Optional[str] = None,
    allowed_hosts: Optional[list[str]] = None,
    host: str = "0.0.0.0",
):
    """One ASGI app serving many brains, each mounted at `/<name>/mcp`.

    The point is what is *not* duplicated. A process per brain re-loads the
    Python runtime and a ~250MB resident embedding model every time, which caps
    a modest host at a handful of brains. Here the model is loaded once — the
    provider cache is keyed on model configuration, not on brain — so the
    marginal cost of a brain is its config and doc_types, and hundreds fit where
    a handful did.

    Each brain keeps its own token, its own read/write policy and its own
    storage, so this is a packaging change rather than a shared-tenancy one.
    """
    from contextlib import AsyncExitStack, asynccontextmanager

    from starlette.applications import Starlette
    from starlette.responses import JSONResponse, PlainTextResponse
    from starlette.routing import Mount, Route

    base = (public_base_url or "").rstrip("/")
    mounted: list = []          # the Starlette children, for their lifespans
    routes: list = []
    listing: dict[str, dict] = {}

    for name, path in brains.items():
        brain = Brain.open(path)
        server = build_server(brain, read_only=read_only)
        public_url = f"{base}/{name}/mcp" if base else None
        child = server.streamable_http_app(
            transport_security=build_transport_security(public_url, allowed_hosts),
            host=host,
        )
        mounted.append(child)

        brain_token = token_for(name, token)
        app = _bearer_auth_middleware(child, brain_token) if brain_token else child
        routes.append(Mount(f"/{name}", app=app))

        listing[name] = {
            "mcp": public_url or f"/{name}/mcp",
            "description": brain.config.description,
            "entities": sum(brain.counts().values()),
            "doc_types": sorted(brain.config.doc_types),
            "read_only": read_only,
            "authenticated": bool(brain_token),
        }

    async def directory(_request):
        return JSONResponse(listing)

    async def healthz(_request):
        return PlainTextResponse("ok")

    routes += [Route("/", directory), Route("/healthz", healthz)]

    @asynccontextmanager
    async def lifespan(_app):
        # Starlette does not run a mounted app's lifespan, and the MCP transport
        # needs its session manager started — without this every request to a
        # mounted brain fails with "Task group is not initialized".
        async with AsyncExitStack() as stack:
            for child in mounted:
                await stack.enter_async_context(
                    child.router.lifespan_context(child))
            yield

    return Starlette(routes=routes, lifespan=lifespan)


def serve_http(
    brain_dir: str, host: str = "0.0.0.0", port: int = 8080,
    token: Optional[str] = None, read_only: bool = False,
    warn_unauthenticated: bool = True,
    public_url: Optional[str] = None,
    allowed_hosts: Optional[list[str]] = None,
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
    app = server.streamable_http_app(
        transport_security=build_transport_security(public_url, allowed_hosts),
        host=host,
    )
    if token:
        app = _bearer_auth_middleware(app, token)
    elif warn_unauthenticated:
        # The CLI prints its own (richer) warning in the startup banner and passes
        # warn_unauthenticated=False; this covers programmatic callers.
        import sys
        print(
            "WARNING: serving with no token; the endpoint is unauthenticated. "
            "Set OPEN_INDEX_TOKEN or --token for anything networked.",
            file=sys.stderr,
        )
    uvicorn.run(app, host=host, port=port)


def serve_http_multi(
    brains_root: str, host: str = "0.0.0.0", port: int = 8080,
    token: Optional[str] = None, read_only: bool = False,
    public_base_url: Optional[str] = None,
    allowed_hosts: Optional[list[str]] = None,
) -> None:
    """Serve every brain under `brains_root` from one process."""
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise SystemExit(
            "the HTTP endpoint needs uvicorn: pip install 'open-index[serve]'"
        ) from exc

    brains = discover_brains(brains_root)
    if not brains:
        raise SystemExit(
            f"no brains found under {brains_root} — a brain is a directory "
            "containing brain.yaml (create one with `open-index init`)"
        )
    app = build_multi_app(
        brains, token=token, read_only=read_only,
        public_base_url=public_base_url, allowed_hosts=allowed_hosts, host=host,
    )
    uvicorn.run(app, host=host, port=port)
