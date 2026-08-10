"""Unit tests for the OpenSearch backend's pure builders (no live cluster).

These verify the mapping, document round-trip, boosted field list, and query
body — the logic that would talk to a real cluster. A live round-trip test lives
behind the OPENSEARCH_URL env var (skipped by default).
"""

from open_index.models import Entity
from open_index.schema import DocType, DocTypeDisplay, FieldSpec
from open_index.storage.opensearch_backend import OpenSearchBackend


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


# -- provenance / validity round-trip -----------------------------------------
#
# SQLite stores the whole entity as a JSON blob, so it round-trips anything
# added to the model. This backend maps fields explicitly, so a field the model
# gains and the mapping doesn't is silently dropped on write — which is exactly
# what happened to provenance. These pin the round-trip.


def _roundtrip(entity):
    return OpenSearchBackend.doc_to_entity(OpenSearchBackend.entity_to_doc(entity))


def test_entity_provenance_survives_the_round_trip():
    from open_index.models import Provenance

    entity = Entity(id="issue:a", doc_type="issue", name="A",
                    provenance=Provenance(asserted_by="agent:x", confidence=0.8,
                                          evidence="seen in logs"))
    back = _roundtrip(entity)
    assert back.provenance is not None, "provenance dropped on write"
    assert back.provenance.asserted_by == "agent:x"
    assert back.provenance.confidence == 0.8
    assert back.provenance.evidence == "seen in logs"


def test_validity_window_survives_the_round_trip():
    entity = Entity(id="issue:b", doc_type="issue", name="B",
                    valid_from="2020-01-01", valid_to="2020-06-01")
    back = _roundtrip(entity)
    assert back.valid_from == "2020-01-01"
    assert back.valid_to == "2020-06-01"


def test_per_edge_provenance_survives_the_round_trip():
    from open_index.models import Provenance, Relationship

    entity = Entity(id="issue:c", doc_type="issue", name="C", related_to=[
        Relationship(target="product:checkout", relationship_edge_meaning="affects",
                     provenance=Provenance(asserted_by="agent:y", confidence=0.3)),
    ])
    edge = _roundtrip(entity).related_to[0]
    assert edge.relationship_edge_meaning == "affects"
    assert edge.provenance is not None, "edge provenance dropped on write"
    assert edge.provenance.confidence == 0.3


def test_unattributed_entities_stay_clean():
    """No provenance must not become an empty block — absence is informative."""
    doc = OpenSearchBackend.entity_to_doc(Entity(id="issue:d", doc_type="issue"))
    assert "provenance" not in doc
    assert "valid_from" not in doc
    assert _roundtrip(Entity(id="issue:d", doc_type="issue")).provenance is None


def test_mapping_declares_provenance_and_validity():
    props = OpenSearchBackend.mapping()["mappings"]["properties"]
    assert "provenance" in props
    assert "valid_from" in props and "valid_to" in props
    assert "provenance" in props["related_to"]["properties"]


def test_reserved_keys_cover_the_metadata_fields():
    """Otherwise a schema field named `provenance` would collide with it."""
    from open_index.storage.opensearch_backend import _RESERVED_KEYS

    assert {"provenance", "valid_from", "valid_to"} <= _RESERVED_KEYS


# -- fuzzy search must not be handed non-text fields ---------------------------
#
# OpenSearch rejects the entire query with "Can only use fuzzy queries on keyword
# and text fields" if a numeric or date field is listed in multi_match. One
# `number` field in the schema therefore broke *all* search — found with a real
# dataset, not caught by the earlier synthetic ones.


def test_numeric_fields_are_kept_out_of_the_fuzzy_query():
    dt = DocType(doc_type="defect", fields=[
        FieldSpec(name="name", type="string", boost=6),
        FieldSpec(name="symptom", type="text"),
        FieldSpec(name="manhours", type="number"),
    ])
    fields = _backend_with_types([dt])._search_fields(None)
    joined = " ".join(fields)
    assert "fields.symptom" in joined
    assert "manhours" not in joined, "a numeric field would break the whole query"


def test_boolean_and_timestamp_fields_are_excluded_too():
    dt = DocType(doc_type="t", fields=[
        FieldSpec(name="name", type="string"),
        FieldSpec(name="active", type="boolean"),
        FieldSpec(name="seen_at", type="timestamp"),
    ])
    joined = " ".join(_backend_with_types([dt])._search_fields(None))
    assert "active" not in joined and "seen_at" not in joined


def test_text_fields_keep_their_boosts():
    dt = DocType(doc_type="t", fields=[
        FieldSpec(name="title", type="string", boost=6),
        FieldSpec(name="body", type="text", boost=2),
        FieldSpec(name="count", type="number", boost=9),
    ])
    fields = _backend_with_types([dt])._search_fields(None)
    assert "fields.title^6" in fields
    assert "fields.body^2" in fields
    assert not any("count" in f for f in fields)


def test_date_detection_is_disabled_in_the_mapping():
    """A `string` field holding e.g. 2026-04-12 would otherwise be mapped as a
    date, and then rejected by fuzzy search."""
    assert OpenSearchBackend.mapping()["mappings"]["date_detection"] is False
    backend = _backend_with_types([DocType(doc_type="t")])
    assert backend._base_mapping()["mappings"]["date_detection"] is False
