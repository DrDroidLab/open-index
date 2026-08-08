"""Entity and Relationship — the instances stored in the brain.

An entity is one instance of a doc_type. Every entity can declare relationships
to other entities. The edge model is intentionally generic:

    related_to: [ { target, relationship_edge_meaning } ]

`target` is another entity's id; `relationship_edge_meaning` is free text
describing the edge ("has common issue", "is downstream of", "is explained by").
This replaces the reference's hardcoded upstream/downstream service fields with
something that works for support, sales, product, infra — any domain.

Entities and edges also carry two orthogonal pieces of metadata, both optional:

    provenance  — who asserted this, when, how confidently, on what evidence
    validity    — the window over which the claim is true OF THE WORLD

They are separate on purpose. `asserted_at` answers "when did we come to believe
this"; `valid_from`/`valid_to` answer "when was it true". A brain that only tracks
the first cannot say whether a fact applied during an incident window, and one that
only tracks the second cannot tell a stale belief from a current one.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

_ID_RE = re.compile(r"^[a-zA-Z0-9._-]+:[a-zA-Z0-9._-]+$")


class Provenance(BaseModel):
    """Where a claim came from, and how much to trust it.

    Every field is optional so nothing existing breaks, but the absence of
    provenance is itself informative: an unattributed claim is one nothing can
    audit. A wrong attribute does not fail loudly — it is served to readers in
    exactly the same voice as a correct one, so consumers that act on a claim
    should filter on `confidence` and record what they filtered on.
    """

    # Who or what asserted it. Free-form, but a `kind:id` convention keeps it
    # sortable and greppable: "connector:<name>", "agent:<name>", "human:<user>",
    # "import:<batch-id>".
    asserted_by: Optional[str] = None
    # When the assertion was made (ISO-8601). Distinct from validity — see module docstring.
    asserted_at: Optional[str] = None
    # 0..1. Consumers should treat `None` as "unknown", never as 1.0.
    confidence: Optional[float] = None
    # What justified it, verbatim where possible, so the claim can be re-checked.
    evidence: Optional[str] = None

    @field_validator("confidence")
    @classmethod
    def _bounded(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and not (0.0 <= v <= 1.0):
            raise ValueError("confidence must be between 0 and 1")
        return v

    def is_empty(self) -> bool:
        return not any((self.asserted_by, self.asserted_at,
                        self.confidence is not None, self.evidence))


class Relationship(BaseModel):
    """A directed, labeled edge from one entity to another."""

    target: str
    relationship_edge_meaning: str = ""
    provenance: Optional[Provenance] = None

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
    # Who asserted this entity, when, how confidently.
    provenance: Optional[Provenance] = None
    # The window over which the claim holds of the world. Both open-ended by
    # default: `valid_from=None` means "as far back as we know", `valid_to=None`
    # means "still true". An entity with neither makes no temporal claim at all,
    # which is different from claiming it is always true.
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None

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
        reserved = {"id", "doc_type", "name", "related_to", "fields",
                    "provenance", "valid_from", "valid_to"}
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
                "provenance": core.get("provenance"),
                "valid_from": core.get("valid_from"),
                "valid_to": core.get("valid_to"),
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
            # Drop empty provenance rather than writing `"provenance": null` onto
            # every edge — a file diff should show provenance appearing, not noise.
            out["related_to"] = [
                {k: v for k, v in r.model_dump().items()
                 if not (k == "provenance" and (v is None or not any(v.values())))}
                for r in self.related_to
            ]
        if self.provenance is not None and not self.provenance.is_empty():
            out["provenance"] = {k: v for k, v in self.provenance.model_dump().items()
                                 if v is not None}
        if self.valid_from is not None:
            out["valid_from"] = self.valid_from
        if self.valid_to is not None:
            out["valid_to"] = self.valid_to
        return out

    # -- temporal / trust helpers ---------------------------------------------

    def holds_at(self, when: Optional[str]) -> bool:
        """Was this claim true of the world at `when`?

        Lexical ISO-8601 comparison — no parsing, no offset arithmetic. Timestamps
        arrive from many sources in this domain and re-implementing normalisation
        here would add a class of error the caller cannot see.

        An entity with no validity window makes no temporal claim, so it holds at
        every time: absence of a bound is not a bound.
        """
        if when is None:
            return True
        if self.valid_from is not None and str(when) < str(self.valid_from):
            return False
        if self.valid_to is not None and str(when) > str(self.valid_to):
            return False
        return True

    def trusted(self, min_confidence: float) -> bool:
        """Does this claim clear a confidence floor?

        Unattributed or unscored claims do NOT clear a positive floor. Treating
        `None` as certain is how an unaudited guess ends up indistinguishable from
        a measurement.
        """
        if min_confidence <= 0:
            return True
        if self.provenance is None or self.provenance.confidence is None:
            return False
        return self.provenance.confidence >= min_confidence
