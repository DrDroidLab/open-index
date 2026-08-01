"""Core DroidBrain client — wraps OpenSearch for brain/entity management."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from opensearchpy import OpenSearch, exceptions

from droid_brain.models import Brain, BrainStructure, DocType, Entity, SchemaField

META_INDEX = "droid_brain_meta"


class DroidBrain:
    """Client for creating/querying Droid Brains backed by OpenSearch."""

    def __init__(self, opensearch_url: str = "http://localhost:9200"):
        self.client = OpenSearch(opensearch_url)
        self._ensure_meta_index()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_meta_index(self) -> None:
        """Create the meta index if it doesn't exist."""
        if not self.client.indices.exists(index=META_INDEX):
            self.client.indices.create(
                index=META_INDEX,
                body={
                    "mappings": {
                        "properties": {
                            "brain_name": {"type": "keyword"},
                            "record_type": {"type": "keyword"},  # "brain" | "doctype"
                            "doc_type_name": {"type": "keyword"},
                            "data": {"type": "object", "enabled": False},
                        }
                    }
                },
            )

    def _entity_index(self, brain_name: str) -> str:
        """Return the index name for a brain's entities."""
        return f"droid_brain__{brain_name}"

    def _ensure_entity_index(self, brain_name: str) -> None:
        idx = self._entity_index(brain_name)
        if not self.client.indices.exists(index=idx):
            self.client.indices.create(
                index=idx,
                body={
                    "mappings": {
                        "properties": {
                            "entity_id": {"type": "keyword"},
                            "doc_type": {"type": "keyword"},
                            "created_at": {"type": "date"},
                            "updated_at": {"type": "date"},
                        }
                    }
                },
            )

    # ------------------------------------------------------------------
    # Brain management
    # ------------------------------------------------------------------

    def create_brain(self, name: str, description: str = "") -> dict:
        """Create a new brain (logical namespace)."""
        brain = Brain(name=name, description=description)
        doc = {
            "brain_name": name,
            "record_type": "brain",
            "data": brain.model_dump(),
        }
        self.client.index(index=META_INDEX, id=f"brain__{name}", body=doc, refresh=True)
        self._ensure_entity_index(name)
        return brain.model_dump()

    def list_brains(self) -> list[dict]:
        """List all brains."""
        try:
            res = self.client.search(
                index=META_INDEX,
                body={"query": {"term": {"record_type": "brain"}}},
            )
            return [hit["_source"]["data"] for hit in res["hits"]["hits"]]
        except exceptions.NotFoundError:
            return []

    def delete_brain(self, name: str) -> None:
        """Delete a brain and all its entities."""
        try:
            self.client.delete(index=META_INDEX, id=f"brain__{name}", refresh=True)
        except exceptions.NotFoundError:
            raise ValueError(f"Brain '{name}' does not exist.")
        # Delete all doctype records for this brain
        try:
            self.client.delete_by_query(
                index=META_INDEX,
                body={
                    "query": {
                        "bool": {
                            "must": [
                                {"term": {"record_type": "doctype"}},
                                {"term": {"brain_name": name}},
                            ]
                        }
                    }
                },
            )
        except exceptions.NotFoundError:
            pass
        self.client.indices.delete(index=self._entity_index(name), ignore=[404])

    # ------------------------------------------------------------------
    # DocType management
    # ------------------------------------------------------------------

    def create_doctype(
        self,
        brain_name: str,
        name: str,
        description: str = "",
        fields: Optional[list[dict]] = None,
    ) -> dict:
        """Define a new doc_type within a brain."""
        fields = fields or []
        schema_fields = [
            SchemaField(**f) if isinstance(f, dict) else f for f in fields
        ]
        dt = DocType(name=name, description=description, schema_fields=schema_fields)
        doc = {
            "brain_name": brain_name,
            "record_type": "doctype",
            "doc_type_name": name,
            "data": dt.model_dump(),
        }
        self.client.index(
            index=META_INDEX,
            id=f"doctype__{brain_name}__{name}",
            body=doc,
            refresh=True,
        )
        return dt.model_dump()

    def get_doctype(self, brain_name: str, name: str) -> Optional[dict]:
        """Get a single doc_type definition."""
        try:
            res = self.client.get(
                index=META_INDEX, id=f"doctype__{brain_name}__{name}"
            )
            return res["_source"]["data"]
        except exceptions.NotFoundError:
            return None

    def list_doctypes(self, brain_name: str) -> list[dict]:
        """List all doc_types in a brain."""
        try:
            res = self.client.search(
                index=META_INDEX,
                body={
                    "query": {
                        "bool": {
                            "must": [
                                {"term": {"record_type": "doctype"}},
                                {"term": {"brain_name": brain_name}},
                            ]
                        }
                    }
                },
            )
            return [hit["_source"]["data"] for hit in res["hits"]["hits"]]
        except exceptions.NotFoundError:
            return []

    # ------------------------------------------------------------------
    # Entity management
    # ------------------------------------------------------------------

    def create_entity(
        self, brain_name: str, doc_type: str, data: dict[str, Any]
    ) -> dict:
        """Create a new entity instance."""
        self._ensure_entity_index(brain_name)
        entity = Entity(
            entity_id=str(uuid.uuid4()),
            doc_type=doc_type,
            data=data,
        )
        body = entity.model_dump()
        self.client.index(
            index=self._entity_index(brain_name),
            id=entity.entity_id,
            body=body,
            refresh=True,
        )
        return entity.model_dump()

    def get_entity(self, brain_name: str, entity_id: str) -> Optional[dict]:
        """Fetch a single entity by ID."""
        try:
            res = self.client.get(
                index=self._entity_index(brain_name), id=entity_id
            )
            return res["_source"]
        except exceptions.NotFoundError:
            return None

    def update_entity(
        self, brain_name: str, entity_id: str, data: dict[str, Any]
    ) -> Optional[dict]:
        """Update an entity's data."""
        existing = self.get_entity(brain_name, entity_id)
        if not existing:
            return None
        existing["data"] = data
        existing["updated_at"] = datetime.utcnow().isoformat()
        self.client.index(
            index=self._entity_index(brain_name),
            id=entity_id,
            body=existing,
            refresh=True,
        )
        return existing

    def delete_entity(self, brain_name: str, entity_id: str) -> bool:
        """Delete an entity."""
        try:
            self.client.delete(
                index=self._entity_index(brain_name),
                id=entity_id,
                refresh=True,
            )
            return True
        except exceptions.NotFoundError:
            return False

    def list_entities(
        self, brain_name: str, doc_type: Optional[str] = None, size: int = 50
    ) -> list[dict]:
        """List entities, optionally filtered by doc_type."""
        idx = self._entity_index(brain_name)
        if not self.client.indices.exists(index=idx):
            return []
        query: dict = {"match_all": {}}
        if doc_type:
            query = {"term": {"doc_type": doc_type}}
        try:
            res = self.client.search(
                index=idx,
                body={"query": query, "size": size, "sort": [{"created_at": "desc"}]},
            )
            return [hit["_source"] for hit in res["hits"]["hits"]]
        except exceptions.NotFoundError:
            return []

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self,
        brain_name: str,
        query_text: str,
        doc_type: Optional[str] = None,
        size: int = 20,
    ) -> list[dict]:
        """Full-text search across entities in a brain."""
        idx = self._entity_index(brain_name)
        if not self.client.indices.exists(index=idx):
            return []

        must_clauses: list[dict] = [
            {
                "multi_match": {
                    "query": query_text,
                    "fields": ["data.*"],
                }
            }
        ]
        if doc_type:
            must_clauses.append({"term": {"doc_type": doc_type}})

        try:
            res = self.client.search(
                index=idx,
                body={
                    "query": {"bool": {"must": must_clauses}},
                    "size": size,
                },
            )
            return [hit["_source"] for hit in res["hits"]["hits"]]
        except exceptions.NotFoundError:
            return []

    # ------------------------------------------------------------------
    # Brain structure (for MCP tool)
    # ------------------------------------------------------------------

    def get_brain_structure(self, brain_name: str) -> BrainStructure:
        """Return a summary of the brain: doc_types, counts, examples."""
        doctypes = self.list_doctypes(brain_name)
        idx = self._entity_index(brain_name)
        total = 0
        dt_stats: list[dict] = []

        if self.client.indices.exists(index=idx):
            # Get counts per doc_type via aggregation
            try:
                agg_res = self.client.search(
                    index=idx,
                    body={
                        "size": 0,
                        "aggs": {
                            "by_doctype": {
                                "terms": {"field": "doc_type", "size": 100}
                            }
                        },
                    },
                )
                buckets = agg_res["aggregations"]["by_doctype"]["buckets"]
                counts = {b["key"]: b["doc_count"] for b in buckets}
                total = sum(counts.values())
            except exceptions.NotFoundError:
                counts = {}
        else:
            counts = {}

        for dt in doctypes:
            dt_name = dt["name"]
            dt_count = counts.get(dt_name, 0)

            # Fetch 2 example entities for this doc_type
            examples = []
            if dt_count > 0:
                try:
                    ex_res = self.client.search(
                        index=idx,
                        body={
                            "query": {"term": {"doc_type": dt_name}},
                            "size": 2,
                        },
                    )
                    examples = [
                        e["_source"].get("data", {})
                        for e in ex_res["hits"]["hits"]
                    ]
                except exceptions.NotFoundError:
                    pass

            dt_stats.append(
                {
                    "name": dt_name,
                    "description": dt.get("description", ""),
                    "schema_fields": dt.get("schema_fields", []),
                    "entity_count": dt_count,
                    "examples": examples,
                }
            )

        return BrainStructure(
            brain_name=brain_name,
            doc_types=dt_stats,
            total_entities=total,
        )
