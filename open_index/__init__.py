"""Open Index — a domain-agnostic context graph for your company.

Public surface:
    Brain            — the top-level handle; open a brain directory and query it.
    Entity, Relationship, DocType, FieldSpec — the data model.
"""

from open_index.models import Entity, Relationship
from open_index.schema import DocType, FieldSpec
from open_index.brain import Brain

__version__ = "0.1.0"

__all__ = ["Brain", "Entity", "Relationship", "DocType", "FieldSpec", "__version__"]
