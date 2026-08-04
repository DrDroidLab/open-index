"""Entity and Relationship — the instances stored in the brain.

An entity is one instance of a doc_type. Every entity can declare relationships
to other entities. The edge model is intentionally generic:

    related_to: [ { target, relationship_edge_meaning } ]

`target` is another entity's id; `relationship_edge_meaning` is free text
describing the edge ("has common issue", "is downstream of", "is explained by").
This replaces the reference's hardcoded upstream/downstream service fields with
something that works for support, sales, product, infra — any domain.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

_ID_RE = re.compile(r"^[a-zA-Z0-9._-]+:[a-zA-Z0-9._-]+$")


class Relationship(BaseModel):
    """A directed, labeled edge from one entity to another."""

    target: str
    relationship_edge_meaning: str = ""

    @field_validator("target")
    @classmethod
    def _valid_target(cls, v: str) -> str:
        if not v:
            raise ValueError("relationship target must be non-empty")
        return v


class Entity(BaseModel):
    """One instance of a doc_type.

    Reserved keys (`id`, `doc_type`, `name`, `related_to`) are first-class;
    everything else declared in the doc_type schema lives in `fields`.
    """

    id: str
    doc_type: str
    name: str = ""
    related_to: list[Relationship] = Field(default_factory=list)
    # Arbitrary schema-defined fields (description, owner, status, ...).
    fields: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}

    @field_validator("id")
    @classmethod
    def _valid_id(cls, v: str) -> str:
        if not _ID_RE.match(v):
            raise ValueError(
                f"entity id {v!r} must look like '<doc_type>:<slug>' "
                "using [a-zA-Z0-9._-]"
            )
        return v

    @model_validator(mode="after")
    def _id_matches_doc_type(self) -> "Entity":
        prefix = self.id.split(":", 1)[0]
        if prefix != self.doc_type:
            raise ValueError(
                f"entity id prefix '{prefix}' does not match doc_type "
                f"'{self.doc_type}' (expected id like '{self.doc_type}:...')"
            )
        if not self.name:
            self.name = self.id.split(":", 1)[1]
        return self

    @classmethod
    def from_dict(cls, data: dict) -> "Entity":
        """Build an Entity from a loose JSON/dict, folding unknown keys into
        `fields` and coercing `related_to` shorthand into Relationships."""
        data = dict(data)
        reserved = {"id", "doc_type", "name", "related_to", "fields"}
        explicit_fields = dict(data.pop("fields", {}) or {})
        related = data.pop("related_to", []) or []

        norm_related: list[dict] = []
        for r in related:
            if isinstance(r, str):
                norm_related.append({"target": r, "relationship_edge_meaning": ""})
            else:
                norm_related.append(dict(r))

        core = {k: data.pop(k) for k in list(data) if k in reserved}
        # Anything left over is a schema field.
        explicit_fields.update(data)

        return cls.model_validate(
            {
                "id": core.get("id"),
                "doc_type": core.get("doc_type"),
                "name": core.get("name", ""),
                "related_to": norm_related,
                "fields": explicit_fields,
            }
        )

    def searchable_text(self) -> str:
        """Concatenated text of all field values, for the FTS content column."""
        parts: list[str] = []
        for v in self.fields.values():
            if v is None:
                continue
            parts.append(str(v))
        return " ".join(parts)

    def label_for(self, label_field: str) -> str:
        if label_field == "name":
            return self.name
        val = self.fields.get(label_field)
        return str(val) if val not in (None, "") else self.name

    def to_json(self) -> dict:
        """Flat, file-friendly JSON (fields hoisted to the top level)."""
        out: dict[str, Any] = {"id": self.id, "doc_type": self.doc_type, "name": self.name}
        out.update(self.fields)
        if self.related_to:
            out["related_to"] = [r.model_dump() for r in self.related_to]
        return out
