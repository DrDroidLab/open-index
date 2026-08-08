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
      index: open_index_acme          # optional; defaults to open_index_<name>
      username: "${OPENSEARCH_USER}"    # ${ENV} resolved at connect time
      password: "${OPENSEARCH_PASSWORD}"
      use_ssl: true
      verify_certs: true

Requires: pip install 'open-index[opensearch]'
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from open_index.config import expand_env
from open_index.models import Entity
from open_index.schema import DocType
from open_index.storage.base import (
    NO_EMBEDDING_PROVIDER_WARNING,
    SearchResults,
    iter_semantic_entities,
    semantic_doc_types,
    semantic_fields_in_scope,
    semantic_text_for,
)

logger = logging.getLogger("open_index.storage.opensearch")

# Reserved top-level keys; everything else on an entity is a schema field.
# `embedding` is reserved so user schema fields can never collide with the vector
# payload stored at the top level of each OpenSearch document.
_RESERVED_KEYS = {"id", "doc_type", "name", "related_to", "embedding"}
_MAX_ALL = 10_000  # cap for all_entities / relationship scans


class OpenSearchBackend:
    def __init__(self, config):
        try:
            from opensearchpy import OpenSearch
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise SystemExit(
                "the OpenSearch backend needs opensearch-py: "
                "pip install 'open-index[opensearch]'"
            ) from exc

        sc = config.search
        self._config = config
        self.index = expand_env(sc.index) or f"open_index_{config.name}".lower()
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

        self._embedding_provider = None  # constructed lazily on first semantic need
        self._embedding_provider_initialized = False
        self._warned_no_provider = False

    def _get_embedding_provider(self):
        """Return the configured provider, constructing it lazily on first use."""
        if self._embedding_provider is None and not self._embedding_provider_initialized and self._config is not None:
            from open_index.embeddings import get_embedding_provider

            self._embedding_provider = get_embedding_provider(self._config)
            self._embedding_provider_initialized = True
        return self._embedding_provider

    # -- pure builders (unit-testable without a cluster) ----------------------

    @staticmethod
    def mapping(dimension: int = 384) -> dict:
        """Index mapping with the optional knn_vector embedding field.

        Static default dimension is 384 for `BAAI/bge-small-en-v1.5`. Pass a
        different dimension when the configured provider uses another model."""
        return {
            "settings": {"index": {"knn": True}},
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
                    "embedding": {
                        "type": "knn_vector",
                        "dimension": dimension,
                        "method": {
                            "name": "hnsw",
                            "engine": "lucene",
                            "space_type": "cosinesimil",
                        },
                    },
                }
            }
        }

    def _base_mapping(self) -> dict:
        """Mapping without the embedding field (for keyword-only brains)."""
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

    def _mapping(self) -> dict:
        """Instance mapping: only include embedding when at least one loaded
        doc_type has semantic fields AND a provider is configured. The cheap
        scope check runs first so keyword-only brains never trigger the lazy
        provider construction (model load)."""
        if not semantic_fields_in_scope(self._doc_types):
            return self._base_mapping()
        provider = self._get_embedding_provider()
        if provider is not None:
            return self.mapping(provider.dim)
        return self._base_mapping()

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

    def _doc_with_embedding(self, entity: Entity) -> dict:
        """Build the OpenSearch document including a vector embedding when the
        provider is available and the doc_type has semantic fields."""
        doc = self.entity_to_doc(entity)
        dt = self._doc_types.get(entity.doc_type)
        # Check for embeddable text FIRST: doc_types without semantic fields
        # must not pay for lazy provider construction (model load) on writes.
        text = semantic_text_for(entity, dt) if dt is not None else ""
        if not text:
            return doc
        provider = self._get_embedding_provider()
        if provider is not None:
            doc["embedding"] = provider.encode([text])[0]
        return doc

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
                self._client.indices.create(index=self.index, body=self._mapping())
                return

            # Only consider semantic migrations if there are semantic fields and a provider.
            if not semantic_fields_in_scope(self._doc_types):
                return
            provider = self._get_embedding_provider()
            if provider is None:
                return

            mapping = self._client.indices.get_mapping(index=self.index)
            props = mapping[self.index]["mappings"]["properties"]
            if "embedding" in props:
                existing_dim = props["embedding"].get("dimension")
                if existing_dim is not None and existing_dim != provider.dim:
                    raise SystemExit(
                        f"OpenSearch index {self.index} has embedding dimension {existing_dim}, "
                        f"but the configured provider uses dimension {provider.dim}. "
                        "Recreate the index and run `open-index index --reembed` to reindex."
                    )
                return

            # Live migration: close → enable knn → open → add knn_vector mapping.
            # Wrapped so an error never leaves the index closed.
            closed = False
            try:
                settings = self._client.indices.get_settings(index=self.index)
                knn = settings[self.index]["settings"]["index"].get("knn", "false")
                if str(knn).lower() != "true":
                    self._client.indices.close(index=self.index)
                    closed = True
                    self._client.indices.put_settings(
                        index=self.index, body={"index.knn": True}
                    )
                    self._client.indices.open(index=self.index)
                    closed = False

                self._client.indices.put_mapping(
                    index=self.index,
                    body={
                        "properties": {
                            "embedding": {
                                "type": "knn_vector",
                                "dimension": provider.dim,
                                "method": {
                                    "name": "hnsw",
                                    "engine": "lucene",
                                    "space_type": "cosinesimil",
                                },
                            }
                        }
                    },
                )
            except Exception as exc:
                logger.error(
                    "OpenSearch semantic migration failed for %s: %s", self.index, exc
                )
                if closed:
                    try:
                        self._client.indices.open(index=self.index)
                    except Exception as open_exc:
                        logger.error("Could not reopen index %s: %s", self.index, open_exc)
                raise
        except OSConnectionError as exc:
            raise SystemExit(
                f"cannot reach OpenSearch at {self._client.transport.hosts} — "
                "is the cluster running? (search.backend: opensearch)"
            ) from exc

    def upsert_entity(self, entity: Entity, doc_type: Optional[DocType] = None) -> None:
        if doc_type is not None:
            self._doc_types[doc_type.doc_type] = doc_type
        self._client.index(
            index=self.index, id=entity.id, body=self._doc_with_embedding(entity), refresh=True
        )

    def upsert_many(self, items: list[tuple[Entity, Optional[DocType]]]) -> None:
        """One _bulk request with a single refresh at the end.

        The per-entity path refreshes the index on every write, which on a few
        hundred rows costs far more than the indexing itself."""
        if not items:
            return

        for _entity, doc_type in items:
            if doc_type is not None:
                self._doc_types[doc_type.doc_type] = doc_type

        payload: list[dict] = []
        for entity, _doc_type in items:
            payload.append({"index": {"_index": self.index, "_id": entity.id}})
            payload.append(self._doc_with_embedding(entity))

        response = self._client.bulk(body=payload, refresh=True)
        if response.get("errors"):
            # Surface the first real failure rather than reporting success for a
            # partially-applied batch.
            failed = [
                item[op].get("error")
                for item in response.get("items", [])
                for op in item
                if item[op].get("error")
            ]
            raise RuntimeError(
                f"{len(failed)} of {len(items)} documents failed to index; "
                f"first error: {failed[0]}"
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

    def reembed(self) -> None:
        if self._get_embedding_provider() is None:
            return
        if not semantic_doc_types(self._doc_types):
            return

        # Paginate through the whole index so we never silently skip >10k entities.
        scroll_timeout = "1m"
        filters = []
        body = {
            "size": _MAX_ALL,
            "query": {"match_all": {}},
            "sort": [{"name.kw": "asc"}],
        }
        res = self._client.search(index=self.index, body=body, scroll=scroll_timeout)
        scroll_id = res.get("_scroll_id")
        try:
            while True:
                hits = res["hits"]["hits"]
                if not hits:
                    break
                for entity in iter_semantic_entities(
                    self._doc_types, (self.doc_to_entity(h["_source"]) for h in hits)
                ):
                    self._client.index(
                        index=self.index,
                        id=entity.id,
                        body=self._doc_with_embedding(entity),
                        refresh=True,
                    )
                res = self._client.scroll(scroll_id=scroll_id, scroll=scroll_timeout)
                scroll_id = res.get("_scroll_id")
        finally:
            if scroll_id:
                try:
                    self._client.clear_scroll(scroll_id=scroll_id)
                except Exception as exc:
                    logger.warning("clear_scroll failed: %s", exc)

    def _build_knn_body(
        self, vector: list[float], doc_types: Optional[list[str]], k: int
    ) -> dict:
        knn_clause: dict[str, Any] = {
            "vector": vector,
            "k": k,
        }
        if doc_types:
            knn_clause["filter"] = {"terms": {"doc_type": doc_types}}
        return {
            "size": k,
            "query": {"knn": {"embedding": knn_clause}},
        }

    def _warn_no_embedding_provider(self) -> None:
        if not self._warned_no_provider:
            logger.warning(NO_EMBEDDING_PROVIDER_WARNING)
            self._warned_no_provider = True

    def _run_keyword_search(
        self, query: Optional[str], doc_types: Optional[list[str]],
        limit: int, counts_only: bool,
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

    def search(
        self, query: Optional[str] = None, doc_types: Optional[list[str]] = None,
        limit: int = 20, counts_only: bool = False,
        semantic_weight: Optional[float] = None,
    ) -> SearchResults:
        if counts_only or not query:
            return self._run_keyword_search(query, doc_types, limit, counts_only)

        w = semantic_weight if semantic_weight is not None else (
            self._config.search.semantic_weight if self._config else 0.3)
        semantic_scope = w > 0 and semantic_fields_in_scope(self._doc_types, doc_types)
        provider = self._get_embedding_provider() if semantic_scope else None
        if provider is None:
            if semantic_scope:
                self._warn_no_embedding_provider()
            return self._run_keyword_search(query, doc_types, limit, counts_only)

        # Hybrid search: run the keyword and k-NN queries in parallel, merge by id,
        # normalize each source by its own max _score, and combine with
        # search.semantic_weight. The k-NN filter uses doc_types so the merged
        # candidate set and doc_type_counts stay consistent with the keyword arm.
        k = max(limit, 50)
        kw_body = self.build_search_body(query, doc_types, k, counts_only=False)
        kw_res = self._client.search(index=self.index, body=kw_body)

        vector = provider.encode([query])[0]
        knn_body = self._build_knn_body(vector, doc_types, k)
        knn_res = self._client.search(index=self.index, body=knn_body)

        candidates: dict[str, dict] = {}
        kw_scores: dict[str, float] = {}
        sem_scores: dict[str, float] = {}
        for h in kw_res["hits"]["hits"]:
            candidates[h["_id"]] = h["_source"]
            kw_scores[h["_id"]] = float(h["_score"]) if h.get("_score") is not None else 0.0
        for h in knn_res["hits"]["hits"]:
            candidates[h["_id"]] = h["_source"]
            sem_scores[h["_id"]] = float(h["_score"]) if h.get("_score") is not None else 0.0

        kw_max = max(kw_scores.values()) if kw_scores else 0.0
        sem_max = max(sem_scores.values()) if sem_scores else 0.0

        merged = []
        for eid, src in candidates.items():
            kw_norm = kw_scores.get(eid, 0.0) / kw_max if kw_max else 0.0
            sem_norm = sem_scores.get(eid, 0.0) / sem_max if sem_max else 0.0
            score = (1 - w) * kw_norm + w * sem_norm
            merged.append((score, src))
        merged.sort(key=lambda s: (-s[0], s[1].get("name", "")))
        picked = merged[:limit]
        results = [
            {
                "id": src["id"],
                "doc_type": src["doc_type"],
                "name": src.get("name", ""),
                "score": round(score, 3),
                "entity": self.doc_to_entity(src).to_json(),
            }
            for score, src in picked
        ]
        doc_type_counts = {}
        for src in candidates.values():
            dt = src.get("doc_type")
            doc_type_counts[dt] = doc_type_counts.get(dt, 0) + 1
        total = len(candidates)
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
