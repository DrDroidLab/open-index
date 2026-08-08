"""Trust and time filters on search.

Complements the provenance tests, which cover the model. These cover the query
path: filtering by confidence and by validity window, and the guard that an
unjudgeable row is kept rather than silently dropped.
"""

from open_index.models import Entity


def _put(brain, entity_id, **kwargs):
    brain.put_entity(Entity(id=entity_id, doc_type="issue", name=entity_id, **kwargs))


def test_unfiltered_search_returns_everything(brain):
    _put(brain, "issue:unscored")
    assert brain.search(query=None, doc_types=["issue"]).total >= 1


def test_counts_only_skips_filtering(brain):
    """The cheap aggregate path must not pay for row-level judging."""
    results = brain.search(query=None, doc_types=["issue"], counts_only=True,
                           min_confidence=0.9)
    assert results.doc_type_counts.get("issue", 0) >= 1
    assert results.results == []


def test_min_confidence_drops_low_confidence_entities(brain):
    from open_index.models import Provenance

    _put(brain, "issue:sure", provenance=Provenance(asserted_by="a", confidence=0.95))
    _put(brain, "issue:guess", provenance=Provenance(asserted_by="a", confidence=0.1))

    ids = {r["id"] for r in brain.search(doc_types=["issue"], min_confidence=0.5).results}
    assert "issue:sure" in ids
    assert "issue:guess" not in ids


def test_unscored_entities_are_untrusted_not_certain(brain):
    """An absent confidence must never be read as 1.0."""
    _put(brain, "issue:unscored")
    ids = {r["id"] for r in brain.search(doc_types=["issue"], min_confidence=0.5).results}
    assert "issue:unscored" not in ids


def test_as_of_respects_the_validity_window(brain):
    _put(brain, "issue:past", valid_from="2020-01-01", valid_to="2020-06-01")
    _put(brain, "issue:current", valid_from="2020-01-01")

    ids = {r["id"] for r in brain.search(doc_types=["issue"], as_of="2026-01-01").results}
    assert "issue:current" in ids
    assert "issue:past" not in ids


def test_as_of_inside_a_closed_window_keeps_the_entity(brain):
    _put(brain, "issue:past", valid_from="2020-01-01", valid_to="2020-06-01")
    ids = {r["id"] for r in brain.search(doc_types=["issue"], as_of="2020-03-01").results}
    assert "issue:past" in ids


def test_confidence_and_time_filters_compose(brain):
    from open_index.models import Provenance

    _put(brain, "issue:good", provenance=Provenance(confidence=0.9),
         valid_from="2020-01-01")
    _put(brain, "issue:stale", provenance=Provenance(confidence=0.9),
         valid_from="2020-01-01", valid_to="2020-06-01")

    ids = {r["id"] for r in brain.search(
        doc_types=["issue"], min_confidence=0.5, as_of="2026-01-01").results}
    assert ids == {"issue:good"}


def test_rows_without_a_judgeable_payload_are_kept(brain, monkeypatch):
    """Better a possibly-untrusted row than a silently vanished one."""
    results = brain.search(doc_types=["issue"])
    for row in results.results:
        row.pop("entity", None)
    monkeypatch.setattr(brain.backend, "search", lambda *a, **k: results)

    filtered = brain.search(doc_types=["issue"], min_confidence=0.99)
    assert len(filtered.results) == len(results.results)


# -- coverage / health reporting ---------------------------------------------


def test_provenance_report_breaks_down_by_doc_type(brain):
    from open_index.models import Provenance

    _put(brain, "issue:attributed",
         provenance=Provenance(asserted_by="agent:x", confidence=0.8),
         valid_from="2020-01-01")
    report = brain.provenance_report()

    issue = report["by_doc_type"]["issue"]
    assert issue["attributed"] >= 1
    assert issue["scored"] >= 1
    assert issue["dated"] >= 1
    assert issue["entities"] == issue["attributed"] + issue["unattributed"]


def test_provenance_report_counts_unattributed_entities(brain):
    """The point of the report: seeing what cannot be audited."""
    _put(brain, "issue:anonymous")
    assert brain.provenance_report()["by_doc_type"]["issue"]["unattributed"] >= 1


def test_provenance_report_skips_empty_doc_types(brain):
    """A type with no entities contributes no row rather than a zeroed one."""
    from open_index.schema import DocType

    brain.create_doc_type(DocType(doc_type="empty_type"))
    assert "empty_type" not in brain.provenance_report()["by_doc_type"]
