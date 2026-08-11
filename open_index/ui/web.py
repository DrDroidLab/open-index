"""The explorer, served as ordinary web pages.

Replaces the Streamlit app. The view-model in `view.py` is unchanged and still
decides *what* is shown; this module only renders it, and `templates/` decides
how it looks.

Two things follow from being real pages rather than a widget script:

  every view has a URL      /<index>/schema, /<index>/entity/<id> — so a tab, an
                            entity, a filtered map are all linkable, the back
                            button works, and a demo can be sent as a link.
  the page is HTML          no websocket, no rerun-the-whole-script model, and
                            the design is CSS rather than framework selectors.

One process serves every brain under OPEN_INDEX_BRAINS_ROOT, selected by the
first path segment exactly as the MCP endpoint at /<index>/mcp is. A single
brain (OPEN_INDEX_DIR) is served at the root instead.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from open_index.brain import Brain
from open_index.ui import view

HERE = Path(__file__).parent
TEMPLATES = HERE / "templates"
STATIC = HERE / "static"

# Tab slug -> (label, template). The label comes from view.TAB_GUIDE so the nav
# and the "what each tab does" list cannot drift apart.
TABS = [
    ("", view.HELP_TAB, "help.html"),
    ("schema", "Schema", "schema.html"),
    ("explore", "Explore", "explore.html"),
    ("map", "Map", "map.html"),
    ("analytics", "Analytics", "analytics.html"),
    ("jobs", "Jobs", "jobs.html"),
]


def inline_markdown(text: str):
    """The `**bold**` and `` `code` `` used in view.py's guide strings, as HTML.

    Those strings are written as markdown because they are also read as plain
    text (the MCP navigation guide), so the UI has to render the little of it
    that appears. Escaping happens first and the result is marked safe, so the
    only HTML that can reach the page is the two tags produced here — a full
    markdown library would be a dependency and a much wider surface.
    """
    import re

    from markupsafe import Markup, escape

    out = str(escape(text))
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"`([^`]+?)`", r"<code>\1</code>", out)
    return Markup(out)


@lru_cache(maxsize=64)
def open_brain(brain_dir: str) -> Brain:
    """Opened once per directory: opening a brain loads an embedding model."""
    return Brain.open(brain_dir)


@lru_cache(maxsize=1)
def discover(root: str) -> dict[str, str]:
    from open_index.config import discover_brains

    return {name: str(path) for name, path in discover_brains(root).items()}


def available_brains() -> dict[str, str]:
    """Every brain this process serves, keyed by its URL segment.

    A single-brain deployment has no segment, so it is keyed by "" and lives at
    the root.
    """
    root = os.environ.get("OPEN_INDEX_BRAINS_ROOT")
    if root:
        return discover(root)
    return {"": os.environ.get("OPEN_INDEX_DIR", ".")}


def is_read_only() -> bool:
    return os.environ.get("OPEN_INDEX_READ_ONLY", "").lower() in ("1", "true", "yes")


def directory_hidden() -> bool:
    """Whether this host refuses to enumerate the indexes it serves.

    Default off: a self-hosted instance wants a home page listing its indexes.
    Set OPEN_INDEX_HIDE_DIRECTORY=1 when the index names are themselves
    sensitive — several unrelated tenants or prospects on one host, where
    knowing that /acme-index exists is the leak, not its contents. Then only
    someone already holding a name can reach it: / is a 404, an unknown name is
    a 404 that names nothing, and no page links to a sibling.

    This closes the UI only. A reverse proxy that publishes its own directory
    (a generated /indexes.json, say) has to be dealt with there too.
    """
    return os.environ.get("OPEN_INDEX_HIDE_DIRECTORY", "").lower() in ("1", "true", "yes")


def mcp_url_for_request(request, name: str) -> str:
    """This index's MCP endpoint, from the URL the browser actually used.

    Derived rather than configured for the same reason as before: one shared
    process cannot be handed a single correct URL, and deriving it keeps the
    value right behind any proxy. Honours X-Forwarded-Proto so a TLS-terminating
    proxy does not turn an https page into an http endpoint.
    """
    forwarded = request.headers.get("x-forwarded-proto")
    scheme = (forwarded.split(",")[0].strip() if forwarded else request.url.scheme)
    host = request.headers.get("host") or request.url.netloc
    base = f"{scheme}://{host}"
    return f"{base}/{name}/mcp" if name else f"{base}/mcp"


# --------------------------------------------------------------------------- #
# Page data — each returns the context its template renders.
# --------------------------------------------------------------------------- #

def _base_context(request, name: str, brain: Brain, active: str) -> dict[str, Any]:
    summary = view.summarize(brain)
    return {
        "request": request,
        "brain": brain,
        "name": name,
        "summary": summary,
        "active": active,
        "tabs": TABS,
        "base": f"/{name}" if name else "",
        # Drives the "all indexes" link, which must not appear on a host that
        # refuses to enumerate them.
        "multi": (not directory_hidden()) and (len(available_brains()) > 1 or bool(name)),
        "read_only": is_read_only(),
    }


def page_help(request, name: str, brain: Brain) -> dict[str, Any]:
    ctx = _base_context(request, name, brain, "")
    mcp_url = mcp_url_for_request(request, name) or os.environ.get(
        "OPEN_INDEX_PUBLIC_URL", "")
    ctx.update(
        mcp_url=mcp_url,
        client_config=view.mcp_client_config(
            mcp_url, server_name=ctx["summary"].name) if mcp_url else "",
        model_guide=view.MODEL_GUIDE,
        read_tools=view.READ_TOOLS,
        write_tools=view.WRITE_TOOLS,
        tab_guide=view.TAB_GUIDE,
        guide=brain.navigation_guidelines(include_writes=not ctx["read_only"]),
    )
    return ctx


def page_schema(request, name: str, brain: Brain) -> dict[str, Any]:
    ctx = _base_context(request, name, brain, "schema")
    blocks = []
    for row in ctx["summary"].doc_types:
        doc_type = brain.config.doc_type(row.name)
        blocks.append({
            "row": row,
            "fields": view.schema_field_rows(doc_type) if doc_type else [],
            "relationships": view.schema_relationship_rows(brain, row.name),
        })
    ctx["blocks"] = blocks
    return ctx


def page_explore(request, name: str, brain: Brain) -> dict[str, Any]:
    ctx = _base_context(request, name, brain, "explore")
    params = request.query_params
    query = (params.get("q") or "").strip()
    mode = params.get("mode") or "Hybrid"
    if mode not in view.SEARCH_MODES:
        mode = "Hybrid"
    selected = [t for t in params.getlist("t") if t]

    ctx.update(query=query, mode=mode, modes=list(view.SEARCH_MODES),
               selected=selected, results=None, browse=None, error=None)

    if query:
        try:
            found = brain.search(
                query=query, doc_types=selected or None, limit=50,
                mode=view.backend_mode_for(mode), source="ui")
            ctx["results"] = {
                "total": found.total,
                "rows": [
                    {**r,
                     "color": view.color_for(brain, r["doc_type"]),
                     "badge": view.match_badge(r.get("match"))}
                    for r in found.results
                ],
            }
        except Exception as exc:      # a backend that is down must not blank the page
            ctx["error"] = str(exc)
        return ctx

    # No query: list by doc_type, so the page is never blank.
    groups = []
    for row in ctx["summary"].doc_types:
        if not row.count or (selected and row.name not in selected):
            continue
        entities = brain.backend.all_entities([row.name])[:200]
        groups.append({
            "row": row,
            "entities": [
                {"id": e.id, "name": e.name,
                 "description": str(e.fields.get("description", ""))[:120]}
                for e in entities
            ],
            "truncated": row.count > 200,
        })
    ctx["browse"] = groups
    return ctx


def page_entity(request, name: str, brain: Brain, entity_id: str) -> dict[str, Any]:
    ctx = _base_context(request, name, brain, "explore")
    entity = brain.get_entity(entity_id, source="ui")
    ctx.update(entity_id=entity_id, entity=entity)
    if entity is None:
        return ctx
    ctx.update(
        color=view.color_for(brain, entity.doc_type),
        fields=view.field_rows(entity),
        provenance=view.provenance_row(entity),
        links=view.neighbours(brain, entity_id),
        # "What keeps retrieving this?" — the question when a document turns up
        # in an agent's context where it should not.
        retrievals=brain.retrievals_of(entity_id, limit=25),
    )
    return ctx


def page_map(request, name: str, brain: Brain) -> dict[str, Any]:
    ctx = _base_context(request, name, brain, "map")
    populated = [r.name for r in ctx["summary"].doc_types if r.count]
    selected = [t for t in request.query_params.getlist("t") if t in populated]
    focus = request.query_params.get("focus") or None
    ctx.update(populated=populated, selected=selected or populated, focus=focus)
    return ctx


def graph_payload(brain: Brain, scope: list[str], focus: Optional[str]) -> dict[str, Any]:
    """Nodes and edges for the map, shaped for the client-side renderer."""
    from open_index.graph import build_graph, build_overview_graph

    if focus:
        graph = build_graph(brain, [focus], depth=1)
    else:
        graph = build_overview_graph(brain, scope, limit=view.MAX_GRAPH_NODES)

    total = sum(count for dt, count in brain.counts().items() if dt in scope)
    return {
        "nodes": [
            {
                "id": n.id,
                "label": n.label,
                "doc_type": n.doc_type,
                "color": n.color,
                "anchor": bool(n.is_anchor),
                "tooltip": view.node_tooltip(
                    n.id, n.label, n.doc_type,
                    {k: v for k, v in (n.data or {}).items()
                     if k not in ("id", "doc_type", "related_to")}),
            }
            for n in graph.nodes
        ],
        "edges": [
            {"source": e.source, "target": e.target, "meaning": e.meaning,
             "tooltip": view.edge_tooltip(e.source, e.target, e.meaning)}
            for e in graph.edges
        ],
        "legend": view.legend_rows(brain, graph),
        "total": total,
        "capped": (not focus) and len(graph.nodes) < total,
    }


def page_analytics(request, name: str, brain: Brain) -> dict[str, Any]:
    ctx = _base_context(request, name, brain, "analytics")
    summary = brain.analytics_summary()
    ctx["stats"] = summary
    ctx["events"] = brain.analytics_events(limit=100) if summary.get(
        "total_fetches") else []

    # Trace lookup: the whole point of recording the id is being able to ask
    # "what did this turn actually retrieve?" afterwards.
    wanted = (request.query_params.get("trace") or "").strip()
    ctx["trace_query"] = wanted
    ctx["trace_events"] = brain.analytics_by_trace(wanted) if wanted else None
    return ctx


def page_jobs(request, name: str, brain: Brain) -> dict[str, Any]:
    import inspect

    from open_index.connectors.runner import discover_connectors
    from open_index.scheduling import RunState

    ctx = _base_context(request, name, brain, "jobs")
    found = discover_connectors(brain)
    state = RunState(brain.config.root) if brain.config.root else None
    jobs = []
    for job_name, cls in sorted(found.items()):
        meta = (state._data.get(job_name, {}) if state else {})
        try:
            source = inspect.getsource(cls)
        except (OSError, TypeError):
            source = ""
        jobs.append({
            "name": job_name,
            "schedule": cls.schedule,
            "mcp_url": cls.mcp_url,
            "last_run": (meta.get("last_run") or "never")[:19],
            "last_count": meta.get("last_count", "—"),
            "last_status": meta.get("last_status", "—"),
            "source": source,
        })
    ctx["jobs"] = jobs
    return ctx


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

def build_app():
    """The Starlette app serving the explorer."""
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse, RedirectResponse
    from starlette.routing import Mount, Route
    from starlette.staticfiles import StaticFiles
    from starlette.templating import Jinja2Templates

    from open_index.tracing import TRACE_HEADER, trace, trace_from_headers

    class TraceMiddleware(BaseHTTPMiddleware):
        """Bind X-Trace-Id for the life of the request.

        Set here rather than in each handler so every read a page performs —
        including ones several calls deep — is attributed to the same turn. The
        context manager restores the previous value on the way out: a worker
        outlives its request, and a leaked id would credit the next caller's
        retrievals to the previous one.
        """

        async def dispatch(self, request, call_next):
            with trace(trace_from_headers(request.headers)) as tid:
                response = await call_next(request)
            if tid:
                # Echoed so a caller can confirm the id actually took, rather
                # than discovering weeks later that a malformed one was dropped.
                response.headers[TRACE_HEADER] = tid
            return response

    templates = Jinja2Templates(directory=str(TEMPLATES))
    templates.env.filters["comma"] = lambda n: f"{n:,}"
    templates.env.filters["md"] = inline_markdown

    def resolve(request):
        """(url_segment, Brain) for this request, or None when it cannot be served."""
        brains = available_brains()
        if not brains:
            return None, None
        wanted = request.path_params.get("name")
        if wanted is None:                     # single-brain deployment
            key = "" if "" in brains else sorted(brains)[0]
        elif wanted in brains:
            key = wanted
        else:
            return None, None
        return key, open_brain(brains[key])

    def render(request, template: str, ctx: dict):
        # request first: the two-argument form is the deprecated Starlette
        # signature, and on current versions it reads the context as the
        # template name.
        return templates.TemplateResponse(request, template, ctx)

    def not_found(request, wanted: str):
        # The listing is the whole point of hiding the directory: a 404 that
        # helpfully names every other index would hand over exactly what the
        # flag exists to withhold.
        brains = [] if directory_hidden() else sorted(
            n for n in available_brains() if n)
        return templates.TemplateResponse(
            request, "missing.html", {"wanted": wanted, "brains": brains},
            status_code=404,
        )

    def make(page_fn, template):
        def endpoint(request):
            name, brain = resolve(request)
            if brain is None:
                return not_found(request, request.path_params.get("name", ""))
            return render(request, template, page_fn(request, name, brain))
        return endpoint

    def root(request):
        brains = available_brains()
        if "" in brains:                        # single brain: serve it here
            return make(page_help, "help.html")(request)
        if directory_hidden():
            # Not a redirect even when there is only one: on a hidden host the
            # root must not reveal which index that is.
            return not_found(request, "")
        if len(brains) == 1:
            return RedirectResponse(f"/{next(iter(brains))}")
        return templates.TemplateResponse(
            request, "directory.html",
            {"brains": sorted(brains),
             "summaries": {n: view.summarize(open_brain(p))
                           for n, p in sorted(brains.items())}},
        )

    def entity(request):
        name, brain = resolve(request)
        if brain is None:
            return not_found(request, request.path_params.get("name", ""))
        ctx = page_entity(request, name, brain, request.path_params["entity_id"])
        # A link to a deleted entity is a broken link, and should read as one to
        # anything crawling or checking these pages — not as a successful page
        # that happens to say nothing is there.
        status = 200 if ctx["entity"] is not None else 404
        return templates.TemplateResponse(request, "entity.html", ctx,
                                          status_code=status)

    def graph_json(request):
        name, brain = resolve(request)
        if brain is None:
            return JSONResponse({"error": "unknown index"}, status_code=404)
        populated = [r.name for r in view.summarize(brain).doc_types if r.count]
        scope = [t for t in request.query_params.getlist("t") if t in populated]
        return JSONResponse(graph_payload(
            brain, scope or populated, request.query_params.get("focus") or None))

    def healthz(request):
        from starlette.responses import PlainTextResponse

        return PlainTextResponse("ok")

    pages = [
        ("schema", page_schema, "schema.html"),
        ("explore", page_explore, "explore.html"),
        ("map", page_map, "map.html"),
        ("analytics", page_analytics, "analytics.html"),
        ("jobs", page_jobs, "jobs.html"),
    ]

    routes = [
        Route("/healthz", healthz),
        Mount("/static", app=StaticFiles(directory=str(STATIC)), name="static"),
        Route("/", root),
    ]
    # Single-brain routes live at the root; the multi-brain ones carry /{name}.
    for slug, fn, tpl in pages:
        routes.append(Route(f"/{slug}", make(fn, tpl)))
    routes.append(Route("/entity/{entity_id:path}", entity))
    routes.append(Route("/api/graph", graph_json))

    # The JSON API, mounted beside the explorer so one process and one port
    # serve the UI, the API and MCP for every brain. Registered before the
    # /{name} page routes: /api/... must not be read as an index named "api".
    from open_index.api import build_routes as api_routes

    routes += api_routes(resolve, prefix="/api/v1")
    routes += api_routes(resolve, prefix="/{name}/api/v1")

    routes.append(Route("/{name}", make(page_help, "help.html")))
    for slug, fn, tpl in pages:
        routes.append(Route(f"/{{name}}/{slug}", make(fn, tpl)))
    routes.append(Route("/{name}/entity/{entity_id:path}", entity))
    routes.append(Route("/{name}/api/graph", graph_json))

    return Starlette(routes=routes, middleware=[Middleware(TraceMiddleware)])


def serve(host: str = "0.0.0.0", port: int = 8501) -> None:
    import uvicorn

    uvicorn.run(build_app(), host=host, port=port)
