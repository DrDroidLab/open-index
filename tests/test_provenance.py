"""Provenance and validity — who asserted a claim, and when it is true.

The failure mode these guard against: a bulk import writes many low-quality
attributions, the store holds them all as fact, and nothing distinguishes the few
good ones from the many bad ones. Provenance makes that separable — and, more
importantly, makes an UNATTRIBUTED claim fail a trust filter rather than pass it.
"""
import json

from open_index.brain import Brain
from open_index.models import Entity, Provenance, Relationship


def _e(brain, id_, **kw):
    e = Entity.from_dict({"doc_type": "issue", "id": id_, "name": id_.split(":")[1],
                          "severity": "low", "status": "open", **kw})
    brain.put_entity(e)
    return e


# --------------------------------------------------------------------------- #
# Trust
# --------------------------------------------------------------------------- #
def test_an_unattributed_claim_does_not_clear_a_confidence_floor():
    """`None` must never read as certain. This is the whole point: a guess with no
    score is exactly the thing that should be filtered out, and treating missing
    confidence as 1.0 would serve it ahead of a scored, honest claim."""
    bare = Entity.from_dict({"doc_type": "issue", "id": "issue:a", "name": "a"})
    assert bare.trusted(0.0) is True          # no floor -> everything passes
    assert bare.trusted(0.5) is False
    assert bare.trusted(0.01) is False


def test_confidence_is_compared_against_the_floor():
    hi = Entity.from_dict({"doc_type": "issue", "id": "issue:h", "name": "h",
                           "provenance": {"asserted_by": "agent:x", "confidence": 0.9}})
    lo = Entity.from_dict({"doc_type": "issue", "id": "issue:l", "name": "l",
                           "provenance": {"asserted_by": "agent:x", "confidence": 0.2}})
    assert hi.trusted(0.5) and not lo.trusted(0.5)
    assert lo.trusted(0.2)                    # boundary is inclusive


def test_confidence_outside_0_1_is_rejected_at_construction():
    for bad in (1.5, -0.1):
        try:
            Provenance(confidence=bad)
            raise AssertionError(f"accepted {bad}")
        except ValueError:
            pass


# --------------------------------------------------------------------------- #
# Validity — distinct from provenance time
# --------------------------------------------------------------------------- #
def test_no_validity_window_means_no_temporal_claim():
    """Absence of a bound is not a bound. An entity that says nothing about time
    holds at every time, which is different from asserting it is always true."""
    e = Entity.from_dict({"doc_type": "issue", "id": "issue:x", "name": "x"})
    assert e.holds_at("2020-01-01T00:00:00Z")
    assert e.holds_at("2030-01-01T00:00:00Z")
    assert e.holds_at(None)


def test_a_closed_window_excludes_instants_outside_it():
    e = Entity.from_dict({"doc_type": "issue", "id": "issue:w", "name": "w",
                          "valid_from": "2026-07-01T00:00:00Z",
                          "valid_to": "2026-07-25T12:50:54Z"})
    assert not e.holds_at("2026-06-30T23:59:59Z")
    assert e.holds_at("2026-07-10T00:00:00Z")
    assert not e.holds_at("2026-07-25T12:50:55Z")


def test_an_open_ended_window_still_holds_now():
    """`valid_to=None` means "still true", not "never true"."""
    e = Entity.from_dict({"doc_type": "issue", "id": "issue:o", "name": "o",
                          "valid_from": "2026-07-01T00:00:00Z"})
    assert e.holds_at("2099-01-01T00:00:00Z")


def test_validity_comparison_does_no_date_arithmetic():
    """Lexical ISO-8601 only. Timestamps reach this store from many sources, and
    re-implementing normalisation here would add a class of error the caller
    cannot see or audit."""
    import inspect
    src = inspect.getsource(Entity.holds_at)
    for banned in ("datetime", "strptime", "fromisoformat", "timestamp(", "timedelta"):
        assert banned not in src, banned


# --------------------------------------------------------------------------- #
# Round-trip — provenance must survive the file that is the source of truth
# --------------------------------------------------------------------------- #
def test_provenance_and_validity_round_trip_through_json():
    e = Entity.from_dict({
        "doc_type": "issue", "id": "issue:r", "name": "r", "severity": "low",
        "provenance": {"asserted_by": "import:batch-7",
                       "asserted_at": "2026-08-07T00:00:00Z",
                       "confidence": 0.9, "evidence": "source-record-4821"},
        "valid_from": "2026-01-15T09:30:00Z",
    })
    again = Entity.from_dict(json.loads(json.dumps(e.to_json())))
    assert again.provenance.asserted_by == "import:batch-7"
    assert again.provenance.confidence == 0.9
    assert again.valid_from == "2026-01-15T09:30:00Z"
    assert again.valid_to is None


def test_empty_provenance_is_not_written_to_the_file():
    """A diff should show provenance appearing, not `"provenance": null` on every
    entity in the repo."""
    e = Entity.from_dict({"doc_type": "issue", "id": "issue:n", "name": "n"})
    assert "provenance" not in e.to_json()
    assert "valid_from" not in e.to_json()


def test_edges_carry_their_own_provenance():
    """A well-attributed entity can still carry a guessed edge; the two need
    separate trust."""
    e = Entity.from_dict({
        "doc_type": "issue", "id": "issue:edge", "name": "edge",
        "related_to": [{"target": "issue:other", "relationship_edge_meaning": "caused",
                        "provenance": {"asserted_by": "agent:x", "confidence": 0.4}}]})
    assert e.related_to[0].provenance.confidence == 0.4
    again = Entity.from_dict(json.loads(json.dumps(e.to_json())))
    assert again.related_to[0].provenance.asserted_by == "agent:x"


def test_an_edge_without_provenance_stays_clean_in_json():
    e = Entity.from_dict({"doc_type": "issue", "id": "issue:c", "name": "c",
                          "related_to": [{"target": "issue:z"}]})
    assert "provenance" not in e.to_json()["related_to"][0]


# --------------------------------------------------------------------------- #
# Brain-level filtering and the audit report
# --------------------------------------------------------------------------- #
def test_search_filters_out_untrusted_and_out_of_window_claims(brain):
    _e(brain, "issue:trusted", provenance={"asserted_by": "h", "confidence": 0.9})
    _e(brain, "issue:guessed", provenance={"asserted_by": "agent", "confidence": 0.1})
    _e(brain, "issue:bare")
    ids = lambda res: {r["id"] for r in res.results}

    assert {"issue:trusted", "issue:guessed", "issue:bare"} <= ids(
        brain.search(doc_types=["issue"], limit=50))
    # A floor drops both the low-scored AND the unattributed.
    kept = ids(brain.search(doc_types=["issue"], limit=50, min_confidence=0.5))
    assert "issue:trusted" in kept
    assert "issue:guessed" not in kept and "issue:bare" not in kept


def test_search_as_of_respects_the_validity_window(brain):
    _e(brain, "issue:old", valid_to="2026-01-01T00:00:00Z")
    _e(brain, "issue:current", valid_from="2026-01-01T00:00:00Z")
    ids = {r["id"] for r in
           brain.search(doc_types=["issue"], limit=50, as_of="2026-06-01T00:00:00Z").results}
    assert "issue:current" in ids and "issue:old" not in ids


def test_counts_only_search_is_unaffected_by_filters(brain):
    """Counts are the map's spoke data; silently filtering them would make the
    map disagree with the store for reasons the user cannot see."""
    _e(brain, "issue:x")
    before = brain.search(doc_types=["issue"], counts_only=True).doc_type_counts
    after = brain.search(doc_types=["issue"], counts_only=True, min_confidence=0.9).doc_type_counts
    assert before == after


def test_provenance_report_counts_what_can_be_audited(brain):
    _e(brain, "issue:a1", provenance={"asserted_by": "h", "confidence": 0.9})
    _e(brain, "issue:a2", provenance={"asserted_by": "h"})
    _e(brain, "issue:a3")
    rep = brain.provenance_report()
    issue = rep["by_doc_type"]["issue"]
    assert issue["attributed"] == 2
    assert issue["scored"] == 1
    assert issue["unattributed"] == issue["entities"] - 2
    assert 0 <= rep["attributed_pct"] <= 100


# --------------------------------------------------------------------------- #
# Declared-type validation and fault-tolerant indexing
# --------------------------------------------------------------------------- #
def test_a_declared_number_field_rejects_a_string(brain):
    """A schema that advertises `type: number` and accepts "about 15k" is a schema
    in name only; the bad value surfaces later with no trace to the write."""
    from open_index.schema import DocType, FieldSpec
    dt = DocType(doc_type="metric", fields=[FieldSpec(name="size", type="number")])
    assert dt.validate_entity_fields({"size": 15800}) == []
    assert dt.validate_entity_fields({"size": 1.5}) == []
    errs = dt.validate_entity_fields({"size": "about 15k"})
    assert len(errs) == 1 and "expects a number" in errs[0]


def test_a_boolean_in_a_number_field_is_an_error_not_a_one():
    """`bool` subclasses `int` in Python, so a naive isinstance check lets True
    through as 1."""
    from open_index.schema import DocType, FieldSpec
    dt = DocType(doc_type="m", fields=[FieldSpec(name="n", type="number")])
    assert dt.validate_entity_fields({"n": True})


def test_timestamp_fields_are_shape_checked_not_parsed():
    from open_index.schema import DocType, FieldSpec
    dt = DocType(doc_type="e", fields=[FieldSpec(name="at", type="timestamp")])
    for ok in ("2026-07-25", "2026-07-25T12:50:54Z", "2026-07-25 12:50:54",
               "2026-07-25T12:50:54.123+05:30"):
        assert dt.validate_entity_fields({"at": ok}) == [], ok
    for bad in ("yesterday", "25/07/2026", 1784985000):
        assert dt.validate_entity_fields({"at": bad}), bad


def test_undeclared_fields_are_tolerated():
    """Schemas grow by accretion; a strict-unknown rule makes every edit a migration."""
    from open_index.schema import DocType, FieldSpec
    dt = DocType(doc_type="e", fields=[FieldSpec(name="a")])
    assert dt.validate_entity_fields({"a": "x", "not_declared": 42}) == []


def test_empty_values_skip_type_checks_but_still_trip_required():
    from open_index.schema import DocType, FieldSpec
    dt = DocType(doc_type="e", fields=[FieldSpec(name="n", type="number", required=True)])
    errs = dt.validate_entity_fields({"n": None})
    assert len(errs) == 1 and "missing required" in errs[0]


def test_one_bad_entity_file_does_not_abort_the_whole_index(brain, tmp_path):
    """`index()` is the recover-from-disk path. A typo in one file previously took
    the entire brain offline, with no indication of which file was at fault."""
    import json as _json
    good_before = brain.index()
    bad = brain.config.root / "entities" / "issue" / "broken.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text(_json.dumps({"doc_type": "nonexistent_type", "id": "nonexistent_type:x",
                                "name": "x"}))
    count = brain.index()
    assert count == good_before          # every good entity still loaded
    assert len(brain.index_errors) == 1
    assert "broken.json" in brain.index_errors[0]


def test_unreadable_json_is_reported_not_raised(brain):
    bad = brain.config.root / "entities" / "issue" / "garbage.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{not json at all")
    brain.index()
    assert any("garbage.json" in e and "unreadable" in e for e in brain.index_errors)


def test_index_errors_reset_between_runs(brain):
    bad = brain.config.root / "entities" / "issue" / "bad2.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{oops")
    brain.index()
    assert brain.index_errors
    bad.unlink()
    brain.index()
    assert brain.index_errors == []
