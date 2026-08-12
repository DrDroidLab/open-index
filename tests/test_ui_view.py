"""The explorer's view-model.

These pin the behaviour that made the old UI read as broken: a map that drew
nothing until you made a selection, and structure you had to hunt for.
"""

import re

import pytest

from open_index.brain import Brain
from open_index.models import Entity, Provenance, Relationship
from open_index.schema import DocType, DocTypeDisplay, FieldSpec
from open_index.ui import view


@pytest.fixture
def empty_brain(tmp_path):
    root = tmp_path / "fresh"
    root.mkdir()
    (root / "brain.yaml").write_text("name: fresh\n")
    return Brain.open(root)


# -- summary ------------------------------------------------------------------


def test_summary_reports_totals_and_types(brain):
    summary = view.summarize(brain)
    assert summary.name == "support-brain"
    assert summary.total_entities > 0
    assert {r.name for r in summary.doc_types} == set(brain.config.doc_types)


def test_summary_orders_by_count_then_name(brain):
    counts = [r.count for r in view.summarize(brain).doc_types]
    assert counts == sorted(counts, reverse=True)


def test_summary_carries_display_metadata(brain):
    row = next(r for r in view.summarize(brain).doc_types if r.name == "issue")
    assert row.color.startswith("#")
    assert row.storage in ("file", "index")
    assert row.description


def test_empty_brain_summary_is_flagged(empty_brain):
    summary = view.summarize(empty_brain)
    assert summary.is_empty
    assert not summary.has_schema
    assert summary.doc_types == []


def test_declared_but_unpopulated_types_still_listed(brain):
    """A doc_type with no entities must stay visible — that it is empty is
    exactly what the user needs to see."""
    brain.create_doc_type(DocType(doc_type="ghosttype",
                                  display=DocTypeDisplay(color="#123456")))
    row = next(r for r in view.summarize(brain).doc_types if r.name == "ghosttype")
    assert row.count == 0


# -- default anchors (the map-opens-blank fix) --------------------------------


def test_default_anchors_prefer_the_most_connected(brain):
    anchors = view.default_anchors(brain, limit=1)
    degree = view.edge_counts(brain)
    assert anchors, "expected an anchor on a populated brain"
    assert degree[anchors[0]] == max(degree.get(a, 0) for a in degree)


def test_default_anchors_are_capped(brain):
    assert len(view.default_anchors(brain, limit=2)) == 2


def test_default_anchors_respect_the_doc_type_scope(brain):
    anchors = view.default_anchors(brain, doc_types=["product"])
    assert anchors
    assert all(a.startswith("product:") for a in anchors)


def test_default_anchors_on_an_empty_brain(empty_brain):
    assert view.default_anchors(empty_brain) == []


def test_default_anchors_work_without_any_edges(brain):
    """An unconnected brain still gets anchors — just not interesting ones."""
    brain.create_doc_type(DocType(doc_type="lonely", fields=[FieldSpec(name="name")]))
    brain.put_entity(Entity(id="lonely:a", doc_type="lonely", name="A"))
    brain.put_entity(Entity(id="lonely:b", doc_type="lonely", name="B"))
    anchors = view.default_anchors(brain, doc_types=["lonely"])
    assert set(anchors) == {"lonely:a", "lonely:b"}


def test_edge_counts_include_both_directions(brain):
    """A target that never declares an edge still has degree from its referrers."""
    degree = view.edge_counts(brain)
    assert degree.get("issue:payment-declined", 0) > 0


# -- neighbours ---------------------------------------------------------------


def test_neighbours_include_incoming_and_outgoing(brain):
    rows = view.neighbours(brain, "issue:payment-declined")
    assert {r.direction for r in rows} & {"←", "→"}
    assert all(r.other_id for r in rows)


def test_neighbour_labels_are_human_readable(brain):
    rows = view.neighbours(brain, "product:checkout")
    assert rows
    assert any("has common issue" in r.label for r in rows)


def test_dangling_edges_are_shown_not_hidden(brain):
    """A broken reference must stay visible, or it can never be found."""
    brain.put_entity(Entity(
        id="issue:dangling", doc_type="issue", name="Dangling",
        related_to=[Relationship(target="product:does-not-exist",
                                 relationship_edge_meaning="affects")],
    ))
    row = next(r for r in view.neighbours(brain, "issue:dangling")
               if r.other_id == "product:does-not-exist")
    assert row.exists is False
    assert row.other_name == "does-not-exist"
    assert row.other_doc_type == "product"


def test_neighbours_of_an_isolated_entity(brain):
    brain.put_entity(Entity(id="issue:alone", doc_type="issue", name="Alone"))
    assert view.neighbours(brain, "issue:alone") == []


# -- entity detail ------------------------------------------------------------


def test_field_rows_skip_empty_values(brain):
    entity = Entity(id="issue:x", doc_type="issue", name="X",
                    fields={"severity": "high", "status": None, "note": ""})
    assert view.field_rows(entity) == [{"field": "severity", "value": "high"}]


def test_provenance_row_formats_confidence(brain):
    entity = Entity(id="issue:p", doc_type="issue", name="P",
                    provenance=Provenance(asserted_by="agent:x", confidence=0.5))
    row = view.provenance_row(entity)
    assert row["asserted_by"] == "agent:x"
    assert row["confidence"] == "0.50"
    assert row["evidence"] == "—"


def test_provenance_row_is_none_when_unattributed(brain):
    assert view.provenance_row(Entity(id="issue:n", doc_type="issue")) is None


def test_empty_provenance_block_counts_as_unattributed(brain):
    """An all-None Provenance is not attribution and must not render as one."""
    entity = Entity(id="issue:e", doc_type="issue", provenance=Provenance())
    assert view.provenance_row(entity) is None


# -- search modes -------------------------------------------------------------


@pytest.mark.parametrize("label,expected",
                         [("Hybrid", "hybrid"), ("Keyword", "keyword"),
                          ("Semantic", "semantic")])
def test_search_labels_map_to_backend_modes(label, expected):
    """These used to map to a semantic_weight, which let 'Keyword' return
    documents that matched no keyword. The label now selects the mode."""
    assert view.backend_mode_for(label) == expected


def test_unknown_search_mode_falls_back_to_hybrid(brain):
    assert view.backend_mode_for("nonsense") == "hybrid"


def test_match_badge_describes_why_a_result_came_back():
    badge = view.match_badge({"type": "both", "keyword_score": 0.9,
                              "semantic_score": 0.4})
    assert "keyword" in badge["label"] and "meaning" in badge["label"]
    assert badge["keyword_score"] == 0.9


def test_match_badge_is_absent_when_the_backend_sent_none():
    assert view.match_badge(None) is None


def test_color_for_unknown_doc_type_is_the_default(brain):
    assert view.color_for(brain, "nope") == view.DEFAULT_COLOR


# -- map theming and canvas sizing --------------------------------------------
#
# Two bugs these pin, both reported from a dark-mode screenshot: labels rendered
# near-black on a near-black canvas, and the graph sat in a corner instead of
# centred because the canvas width was an invalid CSS value.


# -- Schema tab ----------------------------------------------------------------


def test_schema_field_rows_describe_search_behaviour(brain):
    dt = brain.config.doc_type("issue")
    rows = {r["field"]: r for r in view.schema_field_rows(dt)}
    assert rows["name"]["searched by"] == "keyword match"
    assert rows["description"]["searched by"] == "meaning (vector)"
    assert rows["name"]["weight"] == "6×"


def test_unsearched_fields_show_no_weight(brain):
    from open_index.schema import DocType, FieldSpec

    dt = DocType(doc_type="t", fields=[FieldSpec(name="blob", search="none", boost=9)])
    row = view.schema_field_rows(dt)[0]
    assert row["searched by"] == "not searched"
    assert row["weight"] == "—", "a weight on an unsearched field is misleading"


def test_required_fields_are_marked(brain):
    from open_index.schema import DocType, FieldSpec

    dt = DocType(doc_type="t", fields=[FieldSpec(name="owner", required=True)])
    assert view.schema_field_rows(dt)[0]["required"] == "yes"


def test_relationship_rows_merge_declared_and_observed(brain):
    rows = {r["relationship"]: r for r in view.schema_relationship_rows(brain, "product")}
    assert "has common issue" in rows
    assert rows["has common issue"]["declared"] == "yes"
    assert rows["has common issue"]["points at"] == "issue"
    assert rows["has common issue"]["in use"] > 0


def test_undeclared_relationships_in_use_are_still_listed(brain):
    """An edge nobody declared but everything uses is worth seeing."""
    from open_index.models import Entity, Relationship

    brain.put_entity(Entity(
        id="product:improvised", doc_type="product", name="Improvised",
        related_to=[Relationship(target="issue:no-results",
                                 relationship_edge_meaning="totally made up")]))
    rows = {r["relationship"]: r for r in view.schema_relationship_rows(brain, "product")}
    assert rows["totally made up"]["declared"] == "no"
    assert rows["totally made up"]["in use"] == 1


def test_declared_but_unused_relationships_show_zero(brain):
    from open_index.schema import DocType, RelationshipSpec

    brain.create_doc_type(DocType(
        doc_type="unused_rel",
        relationships=[RelationshipSpec(name="never used", target_doc_type="issue")]))
    row = view.schema_relationship_rows(brain, "unused_rel")[0]
    assert row["declared"] == "yes" and row["in use"] == 0


def test_help_tab_is_first_in_the_guide():
    assert view.TAB_GUIDE[0][0] == view.HELP_TAB
    assert [n for n, _ in view.TAB_GUIDE][:3] == ["How to use?", "Schema", "Explore"]


# -- map readability -----------------------------------------------------------
#
# Long entity names drawn next to their dot were the main thing making the map
# unreadable; the full text moves to the hover tooltip.


def test_node_tooltip_carries_the_full_name_and_type():
    tip = view.node_tooltip("issue:x", "A very long name that got truncated",
                            "issue", {"severity": "high"})
    assert "A very long name that got truncated" in tip
    assert "issue:x" in tip
    assert "severity: high" in tip


def test_node_tooltip_skips_empty_fields_and_caps_length():
    tip = view.node_tooltip("t:x", "N", "t",
                            {f"f{i}": "v" for i in range(20)} | {"blank": ""})
    assert "blank" not in tip
    assert len(tip.splitlines()) <= 7


def test_edge_tooltip_names_the_relationship():
    tip = view.edge_tooltip("a:1", "b:2", "depends on")
    assert "depends on" in tip and "a:1" in tip and "b:2" in tip


def test_edge_tooltip_handles_an_unlabelled_edge():
    assert "related" in view.edge_tooltip("a:1", "b:2", "")


def test_legend_describes_what_is_on_screen(brain):
    from open_index.graph import build_overview_graph

    graph = build_overview_graph(brain, ["product", "issue"])
    rows = view.legend_rows(brain, graph)
    assert {r["doc_type"] for r in rows} <= {"product", "issue"}
    assert all(r["color"].startswith("#") for r in rows)
    assert sum(r["count"] for r in rows) == len(graph.nodes)


def test_legend_is_ordered_by_count(brain):
    from open_index.graph import build_overview_graph

    rows = view.legend_rows(brain, build_overview_graph(brain))
    assert [r["count"] for r in rows] == sorted([r["count"] for r in rows], reverse=True)


# -- the model explanation on the help tab ------------------------------------


def test_model_guide_covers_the_three_ideas():
    terms = " ".join(t for t, _ in view.MODEL_GUIDE).lower()
    assert "doc_type" in terms
    assert "entity" in terms and "doc" in terms   # both names for an instance
    assert "relationship" in terms


def test_model_guide_says_relationships_are_optional():
    text = dict(view.MODEL_GUIDE)["relationship"].lower()
    assert "optional" in text
    assert "without any" in text, "should say entities are valid with no edges"


def test_model_guide_explains_the_id_convention():
    """The single most common write failure, so it belongs in the explanation."""
    text = " ".join(w for _, w in view.MODEL_GUIDE)
    assert "<doc_type>:<slug>" in text


def test_model_guide_distinguishes_schema_from_data():
    text = dict(view.MODEL_GUIDE)["doc_type"].lower()
    assert "schema" in text




# -- the help tab's tool list must match the server's actual tools -------------


def _documented_tool_names():
    """Tool names as the help tab lists them, e.g. 'search_brain(...)'."""
    return {name.split("(")[0]
            for name, _ in list(view.READ_TOOLS) + list(view.WRITE_TOOLS)}


def test_the_help_tab_lists_exactly_the_tools_the_server_registers(brain):
    """A stale tool list on the page is worse than none: it tells a reader an
    agent can do something it cannot, or hides something it can. This assertion
    is the only thing keeping the two in step.
    """
    pytest.importorskip("mcp")
    import asyncio

    from open_index.mcp_server import build_server

    server = build_server(brain)
    registered = {t.name for t in asyncio.run(server.list_tools())}
    # navigation_guidelines is described in prose on the page, not as a row.
    assert _documented_tool_names() | {"navigation_guidelines"} == registered


def test_the_read_only_tools_are_exactly_the_documented_read_ones(brain):
    pytest.importorskip("mcp")
    import asyncio

    from open_index.mcp_server import build_server

    server = build_server(brain, read_only=True)
    registered = {t.name for t in asyncio.run(server.list_tools())}
    documented = {name.split("(")[0] for name, _ in view.READ_TOOLS}
    assert documented | {"navigation_guidelines"} == registered
