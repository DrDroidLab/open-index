"""Droid Brain — a domain-agnostic context graph for your company.

Public surface:
    Brain            — the top-level handle; open a brain directory and query it.
    Entity, Relationship, DocType, FieldSpec — the data model.
"""

from droid_brain.models import Entity, Relationship
from droid_brain.schema import DocType, FieldSpec
from droid_brain.brain import Brain

__version__ = "0.1.0"

__all__ = ["Brain", "Entity", "Relationship", "DocType", "FieldSpec", "__version__"]
