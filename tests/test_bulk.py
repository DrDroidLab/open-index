"""Bulk import: reading the formats people have, and writing many at once.

The governing rule is partial success — one malformed row must never cost you
the other 499 — so most of these assert on what still landed alongside an error.
"""

import json

import pytest

from open_index.brain import BulkResult
from open_index.bulk import load_entity_records
from open_index.models import Entity, Provenance


# -- format readers -----------------------------------------------------------


def test_json_array(tmp_path):
    path = tmp_path / "e.json"
    path.write_text(json.dumps([
        {"doc_type": "issue", "id": "issue:a", "name": "A"},
        {"doc_type": "issue", "id": "issue:b", "name": "B"},
    ]))
    records, errors = load_entity_records(path)
    assert errors == []
    assert [r["id"] for r in records] == ["issue:a", "issue:b"]


def test_json_single_object(tmp_path):
    path = tmp_path / "e.json"
    path.write_text(json.dumps({"doc_type": "issue", "id": "issue:a"}))
    records, errors = load_entity_records(path)
    assert len(records) == 1 and errors == []


def test_json_invalid(tmp_path):
    path = tmp_path / "e.json"
    path.write_text("{nope")
    records, errors = load_entity_records(path)
    assert records == [] and "invalid JSON" in errors[0]


def test_json_wrong_toplevel_type(tmp_path):
    path = tmp_path / "e.json"
    path.write_text("42")
    records, errors = load_entity_records(path)
    assert records == [] and "expected an object or array" in errors[0]


def test_jsonl_skips_only_the_bad_line(tmp_path):
    path = tmp_path / "e.jsonl"
    path.write_text(
        '{"doc_type":"issue","id":"issue:a"}\n'
        "\n"                                    # blank lines are ignored
        "{oops}\n"
        '{"doc_type":"issue","id":"issue:b"}\n'
    )
    records, errors = load_entity_records(path)
    assert [r["id"] for r in records] == ["issue:a", "issue:b"]
    assert len(errors) == 1 and "line 3" in errors[0]


def test_csv_needs_a_doc_type(tmp_path):
    path = tmp_path / "e.csv"
    path.write_text("id,name\na,A\n")
    with pytest.raises(ValueError, match="--doc-type is required for CSV"):
        load_entity_records(path)


def test_csv_qualifies_bare_slugs(tmp_path):
    path = tmp_path / "e.csv"
    path.write_text("id,name\ncheckout,Checkout\nproduct:other,Other\n")
    records, _ = load_entity_records(path, doc_type="product")
    assert [r["id"] for r in records] == ["product:checkout", "product:other"]


def test_csv_coerces_scalars(tmp_path):
    path = tmp_path / "e.csv"
    path.write_text("id,count,ratio,active,label\nx,7,1.5,true,hello\n")
    record = load_entity_records(path, doc_type="t")[0][0]
    assert record["count"] == 7
    assert record["ratio"] == 1.5
    assert record["active"] is True
    assert record["label"] == "hello"


def test_csv_empty_cells_become_absent_not_blank(tmp_path):
    """An empty cell must not defeat a required-field check by looking present."""
    path = tmp_path / "e.csv"
    path.write_text("id,name,owner\nx,X,\n")
    record = load_entity_records(path, doc_type="t")[0][0]
    assert "owner" not in record


def test_csv_parses_related_to_edges(tmp_path):
    path = tmp_path / "e.csv"
    path.write_text(
        "id,related_to\n"
        "x,\"service:api|depends on; datastore:pg|writes to\"\n"
    )
    record = load_entity_records(path, doc_type="t")[0][0]
    assert record["related_to"] == [
        {"target": "service:api", "relationship_edge_meaning": "depends on"},
        {"target": "datastore:pg", "relationship_edge_meaning": "writes to"},
    ]


def test_csv_edge_without_a_meaning(tmp_path):
    path = tmp_path / "e.csv"
    path.write_text("id,related_to\nx,service:api\n")
    record = load_entity_records(path, doc_type="t")[0][0]
    assert record["related_to"] == [
        {"target": "service:api", "relationship_edge_meaning": ""}
    ]


def test_csv_custom_id_column(tmp_path):
    path = tmp_path / "e.csv"
    path.write_text("slug,name\ncheckout,Checkout\n")
    record = load_entity_records(path, doc_type="product", id_field="slug")[0][0]
    assert record["id"] == "product:checkout"
    assert "slug" not in record


def test_csv_empty_file(tmp_path):
    path = tmp_path / "e.csv"
    path.write_text("")
    records, errors = load_entity_records(path, doc_type="t")
    assert records == [] and "no header row" in errors[0]


def test_row_without_an_id_is_an_error(tmp_path):
    path = tmp_path / "e.csv"
    path.write_text("id,name\n,Nameless\n")
    records, errors = load_entity_records(path, doc_type="t")
    assert records == []
    assert "no 'id' value" in errors[0]


def test_row_doc_type_overrides_the_default(tmp_path):
    path = tmp_path / "e.jsonl"
    path.write_text('{"doc_type":"product","id":"product:a"}\n')
    record = load_entity_records(path, doc_type="issue")[0][0]
    assert record["doc_type"] == "product"


def test_row_without_any_doc_type_is_an_error(tmp_path):
    path = tmp_path / "e.jsonl"
    path.write_text('{"id":"issue:a"}\n')
    records, errors = load_entity_records(path)
    assert records == [] and "no doc_type" in errors[0]


def test_unsupported_extension(tmp_path):
    path = tmp_path / "e.xlsx"
    path.write_text("")
    with pytest.raises(ValueError, match="unsupported file type"):
        load_entity_records(path)


def test_non_object_row_is_reported(tmp_path):
    path = tmp_path / "e.json"
    path.write_text(json.dumps([{"doc_type": "issue", "id": "issue:a"}, 42]))
    records, errors = load_entity_records(path)
    assert len(records) == 1
    assert "expected an object" in errors[0]


# -- Brain.put_entities -------------------------------------------------------


def test_bulk_result_summary():
    result = BulkResult(written=2, errors=["x: bad"])
    assert result.failed == 1
    assert "2 written" in result.summary() and "1 failed" in result.summary()


def test_bulk_result_summary_mentions_persisted_files(tmp_path):
    result = BulkResult(written=1, paths=[tmp_path / "a.json"])
    assert "1 file(s) persisted" in result.summary()


def test_bulk_result_clean_summary():
    assert BulkResult(written=3).summary() == "3 written"


def test_csv_coercion_passes_non_strings_through():
    from open_index.bulk import _coerce_csv

    assert _coerce_csv(7) == 7
    assert _coerce_csv(None) is None


@pytest.mark.parametrize("text,expected",
                         [("false", False), ("no", False), ("FALSE", False),
                          ("true", True), ("YES", True)])
def test_csv_boolean_words(text, expected):
    from open_index.bulk import _coerce_csv

    assert _coerce_csv(text) is expected


def test_csv_edge_cell_tolerates_stray_separators(tmp_path):
    path = tmp_path / "e.csv"
    path.write_text("id,related_to\nx,\"; service:api|uses ;; \"\n")
    record = load_entity_records(path, doc_type="t")[0][0]
    assert record["related_to"] == [
        {"target": "service:api", "relationship_edge_meaning": "uses"}
    ]


def test_put_entities_writes_all_valid(brain):
    entities = [Entity(id=f"issue:bulk{i}", doc_type="issue", name=f"B{i}")
                for i in range(5)]
    result = brain.put_entities(entities)
    assert result.written == 5
    assert result.errors == []
    assert brain.get_entity("issue:bulk3") is not None


def test_put_entities_keeps_going_past_a_bad_row(brain):
    result = brain.put_entities([
        Entity(id="issue:ok1", doc_type="issue", name="OK1"),
        Entity(id="ghost:x", doc_type="ghost", name="Ghost"),
        Entity(id="issue:ok2", doc_type="issue", name="OK2"),
    ])
    assert result.written == 2
    assert result.failed == 1
    assert "unknown doc_type" in result.errors[0]
    assert brain.get_entity("issue:ok2") is not None


def test_put_entities_persists_file_backed_types(brain):
    result = brain.put_entities([Entity(id="issue:filed", doc_type="issue", name="F")])
    assert len(result.paths) == 1
    assert result.paths[0].exists()
    assert json.loads(result.paths[0].read_text())["id"] == "issue:filed"


def test_put_entities_does_not_persist_index_backed_types(brain):
    from open_index.schema import DocType, FieldSpec

    brain.create_doc_type(DocType(doc_type="memory", storage="index",
                                  fields=[FieldSpec(name="name")]))
    result = brain.put_entities([Entity(id="memory:m", doc_type="memory", name="M")])
    assert result.written == 1
    assert result.paths == []
    assert brain.get_entity("memory:m") is not None


def test_shared_provenance_is_applied(brain):
    brain.put_entities(
        [Entity(id="issue:s1", doc_type="issue", name="S1"),
         Entity(id="issue:s2", doc_type="issue", name="S2")],
        provenance=Provenance(asserted_by="import:batch", confidence=0.6),
    )
    stored = brain.get_entity("issue:s1")
    assert stored.provenance.asserted_by == "import:batch"
    assert stored.provenance.confidence == 0.6


def test_entity_provenance_beats_the_shared_one(brain):
    brain.put_entities(
        [Entity(id="issue:own", doc_type="issue", name="Own",
                provenance=Provenance(asserted_by="human:dipesh"))],
        provenance=Provenance(asserted_by="import:batch"),
    )
    assert brain.get_entity("issue:own").provenance.asserted_by == "human:dipesh"


def test_put_entities_empty_list(brain):
    result = brain.put_entities([])
    assert result.written == 0 and result.errors == []


def test_put_entities_all_invalid_writes_nothing(brain):
    result = brain.put_entities([Entity(id="ghost:a", doc_type="ghost")])
    assert result.written == 0 and result.failed == 1


def test_put_entities_can_skip_validation(brain):
    """validate=False is the connector/trusted path; it must not silently drop."""
    result = brain.put_entities(
        [Entity(id="ghost:a", doc_type="ghost", name="G")], validate=False)
    assert result.written == 1


# -- backend batching ---------------------------------------------------------


def test_upsert_many_matches_single_writes(brain):
    """The batched path must produce the same rows, edges and searchability."""
    from open_index.models import Relationship

    entity = Entity(id="issue:batched", doc_type="issue", name="Batched",
                    fields={"severity": "high"},
                    related_to=[Relationship(target="product:checkout",
                                             relationship_edge_meaning="affects")])
    brain.backend.upsert_many([(entity, brain.config.doc_type("issue"))])

    stored = brain.get_entity("issue:batched")
    assert stored.fields["severity"] == "high"
    assert stored.related_to[0].target == "product:checkout"
    assert brain.backend.relationships_from("issue:batched")
    assert any(r["id"] == "issue:batched"
               for r in brain.search(query="Batched").results)


def test_upsert_many_is_an_upsert(brain):
    dt = brain.config.doc_type("issue")
    brain.backend.upsert_many([(Entity(id="issue:u", doc_type="issue", name="First"), dt)])
    brain.backend.upsert_many([(Entity(id="issue:u", doc_type="issue", name="Second"), dt)])
    assert brain.get_entity("issue:u").name == "Second"
    assert brain.counts()["issue"] == len(brain.backend.all_entities(["issue"]))


def test_upsert_many_replaces_edges_rather_than_appending(brain):
    from open_index.models import Relationship

    dt = brain.config.doc_type("issue")
    brain.backend.upsert_many([(Entity(
        id="issue:e", doc_type="issue", name="E",
        related_to=[Relationship(target="product:checkout", relationship_edge_meaning="a")]), dt)])
    brain.backend.upsert_many([(Entity(
        id="issue:e", doc_type="issue", name="E",
        related_to=[Relationship(target="product:checkout", relationship_edge_meaning="b")]), dt)])
    meanings = {m for _s, _t, m in brain.backend.relationships_from("issue:e")}
    assert meanings == {"b"}


def test_upsert_many_empty_is_a_noop(brain):
    before = brain.counts()
    brain.backend.upsert_many([])
    assert brain.counts() == before
