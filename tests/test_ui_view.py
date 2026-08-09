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


@pytest.mark.parametrize("mode,expected",
                         [("Hybrid", None), ("Keyword", 0.0), ("Semantic", 1.0)])
def test_search_mode_weights(mode, expected):
    assert view.semantic_weight_for(mode) == expected


def test_unknown_search_mode_falls_back_to_configured(brain):
    assert view.semantic_weight_for("nonsense") is None


def test_color_for_unknown_doc_type_is_the_default(brain):
    assert view.color_for(brain, "nope") == view.DEFAULT_COLOR


# -- map theming and canvas sizing --------------------------------------------
#
# Two bugs these pin, both reported from a dark-mode screenshot: labels rendered
# near-black on a near-black canvas, and the graph sat in a corner instead of
# centred because the canvas width was an invalid CSS value.


def test_graph_width_is_an_int_not_a_css_string():
    """streamlit-agraph does f"{width}px", so "100%" becomes "100%px" and the
    canvas never sizes — the cause of the off-centre map."""
    assert isinstance(view.GRAPH_WIDTH, int)
    assert isinstance(view.GRAPH_HEIGHT, int)
    # Reproduce the library's formatting and check it yields valid CSS.
    for value in (view.GRAPH_WIDTH, view.GRAPH_HEIGHT):
        rendered = f"{value}px"
        assert re.fullmatch(r"\d+px", rendered), f"invalid CSS length: {rendered}"


def test_dark_theme_labels_are_light():
    dark = view.graph_theme("dark")
    light = view.graph_theme("light")
    assert dark["node_label"] != light["node_label"]
    # A light label on a dark canvas: high channel values.
    assert int(dark["node_label"].lstrip("#")[:2], 16) > 0x80
    assert int(light["node_label"].lstrip("#")[:2], 16) < 0x80


def test_label_halo_is_disabled_in_both_themes():
    """vis's default white stroke turns every label into outlined text."""
    for theme in ("dark", "light"):
        assert view.graph_theme(theme)["stroke_width"] == 0


def test_unknown_or_missing_theme_falls_back_to_light():
    """Streamlit reports None when the viewer follows their browser setting."""
    assert view.graph_theme(None) == view.graph_theme("light")
    assert view.graph_theme("") == view.graph_theme("light")
    assert view.graph_theme("solarized") == view.graph_theme("light")


def test_theme_lookup_is_case_insensitive():
    assert view.graph_theme("Dark") == view.graph_theme("dark")


def test_every_theme_defines_the_full_palette():
    keys = {"node_label", "edge_label", "edge", "stroke_width"}
    for theme in ("dark", "light"):
        assert keys <= set(view.graph_theme(theme))


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
    assert [n for n, _ in view.TAB_GUIDE][:3] == ["?", "Schema", "Explore"]


# -- map readability -----------------------------------------------------------
#
# Long entity names drawn next to their dot were the main thing making the map
# unreadable; the full text moves to the hover tooltip.


def test_short_labels_are_left_alone():
    assert view.truncate_label("Checkout") == "Checkout"


def test_long_labels_are_truncated_with_an_ellipsis():
    long = "N412NL 2026-05-18 — Same WING A.ICE VLV OPEN L message five weeks after"
    out = view.truncate_label(long)
    assert len(out) <= view.MAX_NODE_LABEL + 1
    assert out.endswith("…")


def test_truncation_collapses_whitespace():
    assert view.truncate_label("a   b") == "a b"


def test_truncation_does_not_end_on_punctuation():
    assert not view.truncate_label("Wing anti-ice valve, left side").rstrip("…").endswith(",")


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


def test_graph_width_fits_beside_the_legend():
    assert view.GRAPH_WIDTH <= 1000
