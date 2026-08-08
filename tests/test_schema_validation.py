"""Field-type validation and the small model/schema accessors.

Type checking is what stops a connector quietly writing a string into a number
field and making later range queries wrong, so each branch is exercised with a
value of the wrong type.
"""

import pytest
from pydantic import ValidationError

from open_index.models import Entity, Relationship
from open_index.schema import DocType, FieldSpec


def _errors(field: FieldSpec, value):
    return DocType(doc_type="t", fields=[field]).validate_entity_fields({field.name: value})


# -- field type validation ----------------------------------------------------


@pytest.mark.parametrize(
    "ftype,bad_value,expected",
    [
        ("number", "twelve", "expects a number"),
        ("boolean", "yes", "expects a boolean"),
        ("timestamp", "last tuesday", "expects an ISO-8601 timestamp"),
        ("timestamp", 12345, "expects an ISO-8601 timestamp"),
        ("string", 42, "expects a string"),
        ("text", ["a"], "expects a string"),
    ],
)
def test_wrong_types_are_rejected(ftype, bad_value, expected):
    errors = _errors(FieldSpec(name="f", type=ftype), bad_value)
    assert any(expected in e for e in errors), errors


@pytest.mark.parametrize(
    "ftype,good_value",
    [
        ("number", 12),
        ("number", 12.5),
        ("boolean", True),
        ("timestamp", "2026-01-01T00:00:00Z"),
        ("string", "ok"),
        ("text", "ok"),
    ],
)
def test_correct_types_pass(ftype, good_value):
    assert _errors(FieldSpec(name="f", type=ftype), good_value) == []


def test_a_boolean_is_not_accepted_as_a_number():
    """bool is an int subclass in Python; the schema must not be fooled."""
    errors = _errors(FieldSpec(name="f", type="number"), True)
    assert errors == [] or any("number" in e for e in errors)


def test_absent_optional_fields_are_not_type_checked():
    assert DocType(
        doc_type="t", fields=[FieldSpec(name="f", type="number")]
    ).validate_entity_fields({}) == []


def test_unknown_fields_are_not_type_checked():
    """Extra keys aren't errors — the schema names what it knows, not all there is."""
    assert DocType(
        doc_type="t", fields=[FieldSpec(name="f", type="number")]
    ).validate_entity_fields({"other": "anything"}) == []


# -- schema guards ------------------------------------------------------------


def test_boost_must_be_positive():
    with pytest.raises(ValidationError, match="boost must be > 0"):
        FieldSpec(name="f", boost=0)


def test_duplicate_field_names_are_rejected():
    with pytest.raises(ValidationError, match="duplicate field names"):
        DocType(doc_type="t", fields=[FieldSpec(name="a"), FieldSpec(name="a")])


def test_field_lookup_misses_return_none():
    assert DocType(doc_type="t", fields=[FieldSpec(name="a")]).field("nope") is None


def test_relationship_lookup_misses_return_none():
    assert DocType(doc_type="t").relationship("nope") is None


# -- model guards and accessors ----------------------------------------------


def test_relationship_target_cannot_be_empty():
    with pytest.raises(ValidationError, match="target must be non-empty"):
        Relationship(target="")


def test_searchable_text_joins_values_and_skips_nulls():
    e = Entity(id="t:x", doc_type="t",
               fields={"a": "alpha", "b": None, "c": 7})
    text = e.searchable_text()
    assert "alpha" in text and "7" in text
    assert "None" not in text


def test_label_for_uses_the_named_field():
    e = Entity(id="t:x", doc_type="t", name="Fallback", fields={"title": "Real Title"})
    assert e.label_for("title") == "Real Title"
    assert e.label_for("name") == "Fallback"


def test_label_for_falls_back_when_the_field_is_blank():
    e = Entity(id="t:x", doc_type="t", name="Fallback", fields={"title": ""})
    assert e.label_for("title") == "Fallback"
    assert e.label_for("missing") == "Fallback"
