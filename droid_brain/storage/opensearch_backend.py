"""OpenSearch backend — the production path for a networked brain.

Use this when you expose the brain as a shared MCP server / API endpoint that
many agents hit concurrently: OpenSearch gives real per-field boosting, fuzzy
matching, and multi-writer scale that SQLite (single-writer, local file) can't.

It implements the same `SearchBackend` interface as SQLite, so nothing else in
the codebase changes — flip `search.backend: opensearch` in brain.yaml.

Enable in brain.yaml:

    search:
      backend: opensearch
      hosts: ["https://my-opensearch:9200"]
      index: droid_brain_acme          # optional; defaults to droid_brain_<name>
      username: "${OPENSEARCH_USER}"    # ${ENV} resolved at connect time
      password: "${OPENSEARCH_PASSWORD}"
      use_ssl: true
      verify_certs: true

Requires: pip install 'droid-brain[opensearch]'
"""

from __future__ import annotations

from typing import Any, Optional

from droid_brain.config import expand_env
from droid_brain.models import Entity
from droid_brain.schema import DocType
from droid_brain.storage.base import SearchResults

# Reserved top-level keys; everything else on an entity is a schema field.
_RESERVED_KEYS = {"id", "doc_type", "name", "related_to"}
_MAX_ALL = 10_000  # cap for all_entities / relationship scans


class OpenSearchBackend:
    def __init__(self, config):
        try:
            from opensearchpy import OpenSearch
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise SystemExit(
                "the OpenSearch backend needs opensearch-py: "
                "pip install 'droid-brain[opensearch]'"
            ) from exc

        sc = config.search
        self.index = expand_env(sc.index) or f"droid_brain_{config.name}".lower()
        self._doc_types: dict[str, DocType] = {}

        auth = None
        user, password = expand_env(sc.username), expand_env(sc.password)
        if user:
            auth = (user, password or "")

        self._client = OpenSearch(
            hosts=expand_env(sc.hosts),
            http_auth=auth,
            use_ssl=sc.use_ssl,
            verify_certs=sc.verify_certs,
            ssl_show_warn=False,
        )

    # -- pure builders (unit-testable without a cluster) ----------------------

    @staticmethod
    def mapping() -> dict:
        """Index mapping: entity fields are dynamic; related_to is nested so we
        can query incoming edges (target == id)."""
        return {
            "mappings": {
                "properties": {
                    "id": {"type": "keyword"},
                    "doc_type": {"type": "keyword"},
                    "name": {"type": "text", "fields": {"kw": {"type": "keyword"}}},
                    "related_to": {
                        "type": "nested",
                        "properties": {
                            "target": {"type": "keyword"},
                            "meaning": {"type": "keyword"},
                        },
                    },
                    "fields": {"type": "object", "dynamic": True},
                }
            }
        }

    @staticmethod
    def entity_to_doc(entity: Entity) -> dict:
        return {
            "id": entity.id,
            "doc_type": entity.doc_type,
            "name": entity.name,
            "related_to": [
                {"target": r.target, "meaning": r.relationship_edge_meaning}
                for r in entity.related_to
            ],
            "fields": dict(entity.fields),
        }

    @staticmethod
    def doc_to_entity(source: dict) -> Entity:
        flat = {
            "id": source["id"],
            "doc_type": source["doc_type"],
            "name": source.get("name", ""),
            "related_to": [
                {"target": r["target"], "relationship_edge_meaning": r.get("meaning", "")}
                for r in source.get("related_to", [])
            ],
        }
        flat.update(source.get("fields", {}))
        return Entity.from_dict(flat)

    def _search_fields(self, doc_types: Optional[list[str]]) -> list[str]:
        """Build the boosted `fields` list for multi_match from schema boosts.

        This is where OpenSearch honours per-field weighting natively: a
        `boost: 6` title becomes `name^6`. When a field name appears in several
        doc_types with different boosts, the max wins (union). Restricting to a
        single doc_type via `doc_types` yields that type's exact boosts."""
        types = (
            [self._doc_types[t] for t in doc_types if t in self._doc_types]
            if doc_types
            else list(self._doc_types.values())
        )
        boosts: dict[str, float] = {}
        name_boost = 6.0
        for dt in types:
            label = dt.display.label_field
            for f in dt.fields:
                if f.search == "none":
                    continue
                if f.name == label:
                    name_boost = max(name_boost, f.boost)
                boosts[f.name] = max(boosts.get(f.name, 1.0), f.boost)
        fields = [f"name^{name_boost:g}"]
        fields += [f"fields.{name}^{b:g}" for name, b in boosts.items()]
        return fields

    def build_search_body(
        self, query: Optional[str], doc_types: Optional[list[str]], limit: int,
        counts_only: bool,
    ) -> dict:
        filters = [{"terms": {"doc_type": doc_types}}] if doc_types else []
        if query:
            must: dict = {
                "multi_match": {
                    "query": query,
                    "fields": self._search_fields(doc_types),
                    "fuzziness": "AUTO",       # real typo tolerance
                    "type": "best_fields",
                }
            }
            # Deterministic tie-break on equal _score (parity with SQLite's
            # name tie-break); without it tied docs order by Lucene doc id.
            sort = ["_score", {"name.kw": "asc"}]
        else:
            must = {"match_all": {}}
            sort = [{"name.kw": "asc"}]

        return {
            "size": 0 if counts_only else limit,
            "track_total_hits": True,
            "query": {"bool": {"must": must, "filter": filters}},
            "sort": sort,
            "aggs": {"by_doc_type": {"terms": {"field": "doc_type", "size": 200}}},
        }

    # -- SearchBackend interface ----------------------------------------------

    def ensure_schema(self, doc_types: dict[str, DocType]) -> None:
        self._doc_types = dict(doc_types)
        from opensearchpy.exceptions import ConnectionError as OSConnectionError

        try:
            exists = self._client.indices.exists(index=self.index)
            if not exists:
                self._client.indices.create(index=self.index, body=self.mapping())
        except OSConnectionError as exc:
            raise SystemExit(
                f"cannot reach OpenSearch at {self._client.transport.hosts} — "
                "is the cluster running? (search.backend: opensearch)"
            ) from exc

    def upsert_entity(self, entity: Entity, doc_type: Optional[DocType] = None) -> None:
        if doc_type is not None:
            self._doc_types[doc_type.doc_type] = doc_type
        self._client.index(
            index=self.index, id=entity.id, body=self.entity_to_doc(entity), refresh=True
        )

    def get_entity(self, entity_id: str) -> Optional[Entity]:
        from opensearchpy.exceptions import NotFoundError

        try:
            res = self._client.get(index=self.index, id=entity_id)
        except NotFoundError:
            return None
        return self.doc_to_entity(res["_source"])

    def all_entities(self, doc_types: Optional[list[str]] = None) -> list[Entity]:
        filters = [{"terms": {"doc_type": doc_types}}] if doc_types else []
        body = {
            "size": _MAX_ALL,
            "query": {"bool": {"filter": filters}} if filters else {"match_all": {}},
            "sort": [{"name.kw": "asc"}],
        }
        res = self._client.search(index=self.index, body=body)
        return [self.doc_to_entity(h["_source"]) for h in res["hits"]["hits"]]

    def relationships_from(self, entity_id: str) -> list[tuple[str, str, str]]:
        entity = self.get_entity(entity_id)
        if entity is None:
            return []
        return [(entity_id, r.target, r.relationship_edge_meaning) for r in entity.related_to]

    def relationships_to(self, entity_id: str) -> list[tuple[str, str, str]]:
        body = {
            "size": _MAX_ALL,
            "query": {
                "nested": {
                    "path": "related_to",
                    "query": {"term": {"related_to.target": entity_id}},
                    "inner_hits": {"size": 50},
                }
            },
        }
        res = self._client.search(index=self.index, body=body)
        edges: list[tuple[str, str, str]] = []
        for h in res["hits"]["hits"]:
            src = h["_source"]["id"]
            for inner in h["inner_hits"]["related_to"]["hits"]["hits"]:
                r = inner["_source"]
                if r["target"] == entity_id:
                    edges.append((src, entity_id, r.get("meaning", "")))
        return edges

    def counts(self) -> dict[str, int]:
        body = {"size": 0, "aggs": {"by_doc_type": {"terms": {"field": "doc_type", "size": 200}}}}
        res = self._client.search(index=self.index, body=body)
        return {
            b["key"]: b["doc_count"]
            for b in res["aggregations"]["by_doc_type"]["buckets"]
        }

    def search(
        self, query: Optional[str] = None, doc_types: Optional[list[str]] = None,
        limit: int = 20, counts_only: bool = False,
    ) -> SearchResults:
        body = self.build_search_body(query, doc_types, limit, counts_only)
        res = self._client.search(index=self.index, body=body)
        total = res["hits"]["total"]["value"]
        doc_type_counts = {
            b["key"]: b["doc_count"]
            for b in res["aggregations"]["by_doc_type"]["buckets"]
        }
        if counts_only:
            return SearchResults(total=total, results=[], doc_type_counts=doc_type_counts)

        results = []
        for h in res["hits"]["hits"]:
            src = h["_source"]
            results.append({
                "id": src["id"],
                "doc_type": src["doc_type"],
                "name": src.get("name", ""),
                "score": float(h["_score"]) if h.get("_score") is not None else 0.0,
                "entity": self.doc_to_entity(src).to_json(),
            })
        return SearchResults(
            total=total, results=results, doc_type_counts=doc_type_counts, limited=total > limit,
        )

    def delete_by_doc_type(self, doc_types: list[str]) -> None:
        if not doc_types:
            return
        self._client.delete_by_query(
            index=self.index,
            body={"query": {"terms": {"doc_type": doc_types}}},
            refresh=True,
            conflicts="proceed",
        )

    def clear(self) -> None:
        self._client.delete_by_query(
            index=self.index, body={"query": {"match_all": {}}}, refresh=True,
            conflicts="proceed",
        )
