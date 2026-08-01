"""Data models for Droid Brain."""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class SchemaField(BaseModel):
    """A single field definition in a doc_type schema."""

    name: str
    field_type: str = "string"  # string, number, boolean, object
    description: str = ""
    required: bool = False
    processing_type: str = "keyword"  # keyword, text, number, boolean
    search_type: str = "syntactic"  # syntactic, semantic


class DocType(BaseModel):
    """Definition of a document type — the 'concept' in the brain."""

    name: str
    description: str = ""
    schema_fields: list[SchemaField] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class Entity(BaseModel):
    """An instance of a doc_type — the actual data stored in the brain."""

    entity_id: str
    doc_type: str
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class BrainStructure(BaseModel):
    """Summary of a brain's structure — doc_types, counts, examples."""

    brain_name: str
    doc_types: list[dict[str, Any]] = Field(default_factory=list)
    total_entities: int = 0


class Brain(BaseModel):
    """Metadata for a brain."""

    name: str
    description: str = ""
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
