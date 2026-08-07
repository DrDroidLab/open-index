"""Declared relationship slots + surfacing (relationships aren't blind)."""

from open_index.brain import Brain
from open_index.models import Entity


def test_declared_relationships_loaded(brain):
    dt = brain.config.doc_type("product")
    assert dt.relationship("has common issue") is not None
    assert dt.relationship("has common issue").target_doc_type == "issue"


def test_observed_relationships_counts(brain):
    obs = brain.observed_relationships("product")
    # checkout has 2 "has common issue" edges, search has 1 -> 3 total
    assert obs.get("has common issue") == 3


def test_structure_surfaces_relationships(brain):
    by_type = {d["doc_type"]: d for d in brain.structure()["doc_types"]}
    rels = by_type["comment"]["relationships"]
    declared = {r["name"] for r in rels["declared"]}
    assert {"is evidence for", "came from"} <= declared
    assert "is evidence for" in rels["observed"]


def test_navigation_guidelines_lists_relationships(brain):
    md = brain.navigation_guidelines()
    assert "Relationships (declared)" in md
    assert "has common issue" in md


def test_wrong_target_type_flagged(brain):
    # product declares "has common issue" -> issue; point it at a user_segment
    bad = Entity.from_dict({
        "doc_type": "product", "id": "product:bad", "name": "Bad",
        "related_to": [{"target": "user_segment:enterprise",
                        "relationship_edge_meaning": "has common issue"}],
    })
    errors = brain.validate_entity(bad)
    assert any("expects a 'issue' target" in e for e in errors)


def test_forward_reference_allowed(brain):
    # target doesn't exist yet -> no error (forward references are fine)
    ok = Entity.from_dict({
        "doc_type": "product", "id": "product:ok2", "name": "Ok",
        "related_to": [{"target": "issue:not-created-yet",
                        "relationship_edge_meaning": "has common issue"}],
    })
    assert brain.validate_entity(ok) == []


def test_undeclared_meaning_still_allowed(brain):
    # a meaning not in the declared list is permitted (declaring names expected ones)
    e = Entity.from_dict({
        "doc_type": "product", "id": "product:ok3", "name": "Ok",
        "related_to": [{"target": "issue:payment-declined",
                        "relationship_edge_meaning": "some novel relation"}],
    })
    assert brain.validate_entity(e) == []
