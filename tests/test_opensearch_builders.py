"""Unit tests for the OpenSearch backend's pure builders (no live cluster).

These verify the mapping, document round-trip, boosted field list, and query
body — the logic that would talk to a real cluster. A live round-trip test lives
behind the OPENSEARCH_URL env var (skipped by default).
"""

from droid_brain.models import Entity
from droid_brain.schema import DocType, DocTypeDisplay, FieldSpec
from droid_brain.storage.opensearch_backend import OpenSearchBackend


def _backend_with_types(doc_types):
    """Build an instance without connecting (bypass __init__)."""
    b = OpenSearchBackend.__new__(OpenSearchBackend)
    b.index = "test"
    b._doc_types = {dt.doc_type: dt for dt in doc_types}
    return b


def test_mapping_has_nested_related_to():
    m = OpenSearchBackend.mapping()
    assert m["mappings"]["properties"]["related_to"]["type"] == "nested"
    assert m["mappings"]["properties"]["doc_type"]["type"] == "keyword"


def test_entity_doc_round_trip():
    e = Entity.from_dict({
        "doc_type": "service", "id": "service:api", "name": "API",
        "owner": "team", "tier": "critical",
        "related_to": [{"target": "datastore:pg", "relationship_edge_meaning": "writes to"}],
    })
    doc = OpenSearchBackend.entity_to_doc(e)
    assert doc["id"] == "service:api"
    assert doc["fields"]["owner"] == "team"
    assert doc["related_to"] == [{"target": "datastore:pg", "meaning": "writes to"}]

    back = OpenSearchBackend.doc_to_entity(doc)
    assert back.id == e.id
    assert back.fields["tier"] == "critical"
    assert back.related_to[0].target == "datastore:pg"
    assert back.related_to[0].relationship_edge_meaning == "writes to"


def test_search_fields_reflect_boosts():
    dt = DocType(
        doc_type="service", display=DocTypeDisplay(label_field="name"),
        fields=[
            FieldSpec(name="name", boost=6),
            FieldSpec(name="tier", boost=2),
            FieldSpec(name="secret", search="none", boost=9),
        ],
    )
    b = _backend_with_types([dt])
    fields = b._search_fields(["service"])
    assert "name^6" in fields
    assert "fields.tier^2" in fields
    # search:none field is excluded from the query
    assert not any("secret" in f for f in fields)


def test_build_search_body_query_and_filter():
    dt = DocType(doc_type="service", fields=[FieldSpec(name="name", boost=6)])
    b = _backend_with_types([dt])
    body = b.build_search_body("payment", ["service"], limit=10, counts_only=False)
    assert body["size"] == 10
    assert body["query"]["bool"]["filter"] == [{"terms": {"doc_type": ["service"]}}]
    mm = body["query"]["bool"]["must"]["multi_match"]
    assert mm["fuzziness"] == "AUTO"           # fuzzy typo tolerance
    assert "name^6" in mm["fields"]
    assert "by_doc_type" in body["aggs"]


def test_build_search_body_counts_only_zero_size():
    dt = DocType(doc_type="service", fields=[FieldSpec(name="name")])
    b = _backend_with_types([dt])
    body = b.build_search_body("x", None, limit=20, counts_only=True)
    assert body["size"] == 0
    assert "by_doc_type" in body["aggs"]


def test_build_search_body_no_query_sorts_by_name():
    dt = DocType(doc_type="service", fields=[FieldSpec(name="name")])
    b = _backend_with_types([dt])
    body = b.build_search_body(None, None, limit=20, counts_only=False)
    assert body["query"]["bool"]["must"] == {"match_all": {}}
    assert body["sort"] == [{"name.kw": "asc"}]
