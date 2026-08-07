import pytest

from open_index.models import Entity


def test_entity_from_dict_folds_fields_and_edges():
    e = Entity.from_dict({
        "doc_type": "product",
        "id": "product:checkout",
        "name": "Checkout",
        "description": "pay",
        "owner": "team",
        "related_to": [{"target": "issue:x", "relationship_edge_meaning": "has"}],
    })
    assert e.fields == {"description": "pay", "owner": "team"}
    assert e.related_to[0].target == "issue:x"
    assert e.related_to[0].relationship_edge_meaning == "has"


def test_related_to_string_shorthand():
    e = Entity.from_dict({
        "doc_type": "product", "id": "product:a", "related_to": ["issue:b"],
    })
    assert e.related_to[0].target == "issue:b"
    assert e.related_to[0].relationship_edge_meaning == ""


def test_name_defaults_to_slug():
    e = Entity.from_dict({"doc_type": "product", "id": "product:checkout"})
    assert e.name == "checkout"


def test_id_must_match_doc_type():
    with pytest.raises(ValueError):
        Entity.from_dict({"doc_type": "product", "id": "issue:x"})


def test_bad_id_format_rejected():
    with pytest.raises(ValueError):
        Entity.from_dict({"doc_type": "product", "id": "no-colon"})
