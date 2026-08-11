"""Search modes, match provenance, and strict filtering.

The three properties worth defending here:

  mode decides membership, not just weight — a keyword search must not return a
  document that matched no keyword, however low it scores.

  `match` says why a document came back, and "semantic" means it was among the
  nearest by vector. Cosine is positive for nearly every embedded document, so a
  naive `sem > 0` would label the whole index a semantic match.

  a filter is a hard predicate on every path. The dangerous bug is not an error
  but a silent one: an unfiltered semantic arm, or a typo'd field that quietly
  matches everything.
"""

import shutil

import pytest
import yaml

from open_index.brain import Brain
from open_index.models import Entity
from open_index.storage.base import resolve_filters

EXAMPLE = "examples/support-brain"


@pytest.fixture
def brain(tmp_path):
    """The example brain plus a filterable `tenant_id` on `issue`."""
    d = tmp_path / "b"
    shutil.copytree(EXAMPLE, d)
    spec_path = d / "doc_types" / "issue.yaml"
    spec = yaml.safe_load(spec_path.read_text())
    spec["schema"]["fields"].append({
        "name": "tenant_id", "type": "string", "processing": "keyword",
        "search": "none", "boost": 1.0, "filterable": True,
    })
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False))

    b = Brain.open(d)
    b.index()
    for i, tenant in [(1, "acme"), (2, "globex")]:
        b.put_entity(Entity(
            id=f"issue:t{i}", doc_type="issue",
            name=f"payment gateway timeout {tenant}",
            fields={"tenant_id": tenant,
                    "description": "card payments fail at checkout"},
        ))
    return b


def ids(results):
    return [r["id"] for r in results.results]


# -- mode decides membership ---------------------------------------------------


def test_keyword_mode_returns_nothing_that_missed_the_keywords(brain):
    """The bug a weight of 0.0 could not fix: semantic candidates entered the
    set anyway and merely scored 0."""
    res = brain.search(query="payment", mode="keyword")
    assert res.results
    for row in res.results:
        assert row["match"]["type"] == "keyword"


def test_keyword_mode_is_narrower_than_hybrid(brain):
    hybrid = brain.search(query="card declined at till", mode="hybrid")
    keyword = brain.search(query="card declined at till", mode="keyword")
    assert keyword.total < hybrid.total


def test_semantic_mode_finds_documents_that_share_no_words(brain):
    res = brain.search(query="customer cannot pay with their card", mode="semantic")
    assert res.results
    assert all(r["match"]["type"] == "semantic" for r in res.results)


def test_semantic_mode_runs_even_when_the_configured_weight_is_zero(brain):
    """Asking for semantic and getting keyword back would answer a different
    question than the one put."""
    res = brain.search(query="card trouble", mode="semantic", semantic_weight=0.0)
    assert res.results
    assert all(r["match"]["type"] == "semantic" for r in res.results)


def test_an_unknown_mode_is_refused(brain):
    with pytest.raises(ValueError, match="unknown search mode"):
        brain.search(query="payment", mode="telepathy")


# -- match provenance ----------------------------------------------------------


def test_every_result_says_why_it_came_back(brain):
    res = brain.search(query="payment")
    assert res.results
    for row in res.results:
        m = row["match"]
        assert m["type"] in ("keyword", "semantic", "both", "filter", "none")
        assert 0.0 <= m["keyword_score"] <= 1.0
        assert 0.0 <= m["semantic_score"] <= 1.0


def test_hybrid_can_report_both_arms(brain):
    res = brain.search(query="payment", mode="hybrid")
    assert any(r["match"]["type"] == "both" for r in res.results)


def test_semantic_match_is_bounded_by_the_nearest_neighbourhood(brain):
    """cos rescaled to [0,1] is > 0 almost everywhere, so membership by score
    would make every embedded document a semantic match. Membership is instead
    "among the K nearest", which only shows on a corpus bigger than K — on a
    small index every document genuinely is in the neighbourhood.
    """
    for i in range(80):
        brain.put_entity(Entity(
            id=f"issue:bulk-{i}", doc_type="issue", name=f"unrelated topic {i}",
            fields={"description": f"a wholly different subject number {i}"}))

    total_entities = sum(brain.counts().values())
    assert total_entities > 50, "the K bound is only observable past K"

    res = brain.search(query="payment", mode="semantic", limit=5)
    assert res.total <= 50
    assert res.total < total_entities


def test_a_listing_is_not_reported_as_a_keyword_match(brain):
    res = brain.search()
    assert res.results
    assert all(r["match"]["type"] == "none" for r in res.results)


def test_a_pure_filter_reports_itself_as_filtered(brain):
    res = brain.search(filters={"tenant_id": "acme"})
    assert ids(res) == ["issue:t1"]
    assert res.results[0]["match"]["type"] == "filter"


# -- strict filtering ----------------------------------------------------------


@pytest.mark.parametrize("mode", ["hybrid", "keyword", "semantic"])
def test_a_filter_holds_in_every_mode(brain, mode):
    """Filtering only the keyword arm would let a semantic search return exactly
    the documents the filter exists to exclude."""
    res = brain.search(query="payment", mode=mode, filters={"tenant_id": "acme"})
    assert ids(res) == ["issue:t1"]


def test_a_filter_excludes_the_other_tenant(brain):
    res = brain.search(query="payment", filters={"tenant_id": "globex"})
    assert ids(res) == ["issue:t2"]


def test_a_filter_narrows_the_totals_and_counts_too(brain):
    """Counts that disagree with the rows are a display that lies."""
    res = brain.search(query="payment", filters={"tenant_id": "acme"})
    assert res.total == 1
    assert sum(res.doc_type_counts.values()) == 1


def test_entities_without_the_field_never_match(brain):
    """An entity of a doc_type that does not model the field must not slip
    through a tenant filter."""
    res = brain.search(filters={"tenant_id": "acme"})
    assert all(r["doc_type"] == "issue" for r in res.results)


def test_filtering_on_an_undeclared_field_raises(brain):
    with pytest.raises(ValueError, match="cannot filter on"):
        brain.search(query="payment", filters={"nope": "x"})


def test_filtering_on_a_non_filterable_field_raises(brain):
    """`description` exists but is not declared filterable — a promise not made
    is not a promise kept."""
    with pytest.raises(ValueError, match="cannot filter on"):
        brain.search(query="payment", filters={"description": "x"})


def test_the_refusal_names_what_can_be_filtered(brain):
    with pytest.raises(ValueError, match="tenant_id"):
        brain.search(filters={"nope": "x"})


def test_no_filter_is_not_a_filter(brain):
    assert resolve_filters({}, None) == []
    assert resolve_filters({}, {}) == []


def test_a_filter_outside_the_doc_type_scope_raises(brain):
    """tenant_id is declared on `issue`; filtering a search scoped to `product`
    is a mistake worth surfacing rather than a silent empty result."""
    with pytest.raises(ValueError, match="cannot filter on"):
        brain.search(doc_types=["product"], filters={"tenant_id": "acme"})


def test_filters_combine_with_doc_types(brain):
    res = brain.search(doc_types=["issue"], filters={"tenant_id": "acme"})
    assert ids(res) == ["issue:t1"]


# -- the flag has to survive a round trip --------------------------------------


def test_filterable_survives_being_written_back_to_yaml():
    """It was dropped on serialization, which is a quiet failure: the field
    comes back non-filterable and every filter on it is then refused as
    undeclared, long after the doc_type was written."""
    from open_index.config import doc_type_to_yaml_dict
    from open_index.schema import DocType

    dt = DocType.from_dict({
        "doc_type": "issue", "description": "d",
        "schema": {"fields": [
            {"name": "tenant_id", "type": "string", "search": "none",
             "filterable": True},
            {"name": "body", "type": "text", "search": "semantic"},
        ]},
    })
    back = DocType.from_dict(doc_type_to_yaml_dict(dt))
    assert {f.name: f.filterable for f in back.fields} == {
        "tenant_id": True, "body": False}


def test_a_doc_type_created_through_the_agent_can_declare_filterable(tmp_path):
    """create_doc_type writes the schema to disk; the flag must reach it."""
    import shutil

    from open_index.config import load_brain_config, write_doc_type
    from open_index.schema import DocType

    d = tmp_path / "b"
    shutil.copytree(EXAMPLE, d)
    dt = DocType.from_dict({
        "doc_type": "record", "description": "d",
        "schema": {"fields": [{"name": "account_id", "type": "string",
                               "search": "none", "filterable": True}]},
    })
    write_doc_type(d, dt)
    reloaded = load_brain_config(d).doc_type("record")
    assert reloaded.fields[0].filterable is True
