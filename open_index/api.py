"""A JSON HTTP API over a brain — the same operations MCP exposes, over HTTP.

MCP is for agents; this is for everything else: a script, a job, a service, a
`curl`. It deliberately mirrors the MCP tools rather than inventing a second
vocabulary, and calls the same `Brain` methods, so the two cannot disagree about
what a search means or what a write validates.

Mounted at `/<index>/api/v1` alongside the explorer, so one process and one port
serve the UI, the API and the MCP endpoint for every brain on the host.

Auth is off unless a token is configured. `OPEN_INDEX_TOKEN` (or the per-brain
`OPEN_INDEX_TOKEN_<NAME>`) gates *writes* only — reads stay open, matching how
the MCP endpoint already behaves. A deployment holding real data sets the token;
one serving public demo data does not, and nothing changes for it.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from open_index.brain import Brain


def token_for(name: Optional[str]) -> Optional[str]:
    """The write token for this brain, if one is configured."""
    if name:
        specific = os.environ.get(
            "OPEN_INDEX_TOKEN_" + name.upper().replace("-", "_").replace(".", "_"))
        if specific:
            return specific
    return os.environ.get("OPEN_INDEX_TOKEN") or None


def _entity_payload(brain: Brain, entity) -> dict[str, Any]:
    """One entity as the API returns it: the document plus both edge directions."""
    payload = entity.to_json()
    payload["relationships"] = {
        "outgoing": [{"target": t, "meaning": m}
                     for (_s, t, m) in brain.backend.relationships_from(entity.id)],
        "incoming": [{"source": s, "meaning": m}
                     for (s, _t, m) in brain.backend.relationships_to(entity.id)],
    }
    return payload


def build_routes(resolve, prefix: str = ""):
    """Routes for the JSON API.

    `resolve(request)` returns `(name, Brain)` — supplied by the caller so the
    API and the explorer agree on which index a path refers to instead of each
    working it out separately.
    """
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    def error(message: str, status: int, **extra):
        return JSONResponse({"error": message, **extra}, status_code=status)

    def authorized(request, name: Optional[str]) -> bool:
        expected = token_for(name)
        if not expected:
            return True                     # no token configured: writes are open
        header = request.headers.get("authorization", "")
        scheme, _, value = header.partition(" ")
        return scheme.lower() == "bearer" and value == expected

    def with_brain(handler, *, write: bool = False):
        async def endpoint(request):
            name, brain = resolve(request)
            if brain is None:
                return error("unknown index", 404)
            if write and not authorized(request, name):
                # 401 with a challenge, not 403: the caller can fix this by
                # presenting a token, and should be told how.
                return JSONResponse(
                    {"error": "a bearer token is required to write to this index"},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Bearer realm="open-index"'},
                )
            return await handler(request, brain)
        return endpoint

    # -- reads ----------------------------------------------------------------

    async def search(request, brain: Brain):
        params = request.query_params
        try:
            limit = int(params.get("limit", 20))
        except ValueError:
            return error("limit must be an integer", 400)

        filters: dict[str, Any] = {}
        # filter.<field>=<value>, so a filter needs no JSON body on a GET.
        for key, value in params.multi_items():
            if key.startswith("filter."):
                filters[key[len("filter."):]] = value

        try:
            results = brain.search(
                query=params.get("q"),
                doc_types=[t for t in params.getlist("doc_type") if t] or None,
                limit=limit,
                mode=params.get("mode", "hybrid"),
                filters=filters or None,
                source="api",
            )
        except ValueError as exc:
            # An unknown mode or a filter on an undeclared field. The caller can
            # fix both, and the message says how — so it is a 400, not a 500.
            return error(str(exc), 400)

        return JSONResponse({
            "query": params.get("q"),
            "mode": params.get("mode", "hybrid"),
            "filters": filters,
            "total": results.total,
            "doc_type_counts": results.doc_type_counts,
            "limited": results.limited,
            "results": results.results,
        })

    async def get_one(request, brain: Brain):
        entity_id = request.path_params["entity_id"]
        entity = brain.get_entity(entity_id, source="api")
        if entity is None:
            return error(f"no entity '{entity_id}'", 404)
        return JSONResponse(_entity_payload(brain, entity))

    async def get_many(request, brain: Brain):
        ids = [i for i in request.query_params.getlist("id") if i]
        found = brain.get_entities(ids, source="api")
        return JSONResponse({
            "requested": len(ids),
            "found": len(found),
            "missing": sorted(set(ids) - {e.id for e in found}),
            "entities": [_entity_payload(brain, e) for e in found],
        })

    async def by_external(request, brain: Brain):
        external_id = request.path_params["external_id"]
        entity = brain.get_by_external_id(external_id, source="api")
        if entity is None:
            return error(f"no entity with external_id '{external_id}'", 404)
        return JSONResponse(_entity_payload(brain, entity))

    async def schema(request, brain: Brain):
        from open_index.config import doc_type_to_yaml_dict

        counts = brain.counts()
        return JSONResponse({
            "name": brain.config.name,
            "description": brain.config.description,
            "doc_types": [
                {**doc_type_to_yaml_dict(dt), "count": counts.get(name, 0)}
                for name, dt in brain.config.doc_types.items()
            ],
        })

    async def trace_lookup(request, brain: Brain):
        trace_id = request.path_params["trace_id"]
        return JSONResponse({"trace_id": trace_id,
                             "reads": brain.analytics_by_trace(trace_id)})

    # -- writes ---------------------------------------------------------------

    async def put_one(request, brain: Brain):
        from open_index.models import Entity

        entity_id = request.path_params["entity_id"]
        try:
            body = await request.json()
        except Exception:
            return error("body must be JSON", 400)
        if not isinstance(body, dict):
            return error("body must be a JSON object", 400)

        body = dict(body)
        # The URL is the authority on which entity this is. Accepting a body id
        # that disagrees would let PUT /entities/a write entity b.
        if body.get("id") not in (None, entity_id):
            return error("id in the body does not match the URL", 400,
                         url_id=entity_id, body_id=body.get("id"))
        body["id"] = entity_id
        body.setdefault("doc_type", entity_id.split(":", 1)[0])

        try:
            entity = Entity.from_dict(body)
        except Exception as exc:
            return error(f"invalid entity: {exc}", 400)
        try:
            path = brain.put_entity(entity)
        except ValueError as exc:
            return error(str(exc), 422)
        return JSONResponse({"written": entity.id,
                             "file": str(path) if path else None})

    async def delete_one(request, brain: Brain):
        entity_id = request.path_params["entity_id"]
        try:
            deleted = brain.delete_entity(entity_id, source="api")
        except RuntimeError as exc:
            return error(str(exc), 500)
        if not deleted:
            return error(f"no entity '{entity_id}'", 404)
        return JSONResponse({"deleted": entity_id})

    p = prefix
    return [
        Route(f"{p}/search", with_brain(search)),
        Route(f"{p}/schema", with_brain(schema)),
        Route(f"{p}/entities", with_brain(get_many)),
        Route(f"{p}/entities/by-external-id/{{external_id:path}}", with_brain(by_external)),
        Route(f"{p}/traces/{{trace_id}}", with_brain(trace_lookup)),
        Route(f"{p}/entities/{{entity_id:path}}", with_brain(get_one), methods=["GET"]),
        Route(f"{p}/entities/{{entity_id:path}}", with_brain(put_one, write=True),
              methods=["PUT"]),
        Route(f"{p}/entities/{{entity_id:path}}", with_brain(delete_one, write=True),
              methods=["DELETE"]),
    ]
