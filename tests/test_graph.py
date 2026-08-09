from open_index.graph import build_graph


def test_depth_one_reaches_direct_neighbors(brain):
    g = build_graph(brain, "product:checkout", depth=1)
    ids = {n.id for n in g.nodes}
    # outgoing edges to its two issues
    assert "issue:payment-declined" in ids
    assert "issue:slow-checkout" in ids
    # anchor flagged
    anchor = next(n for n in g.nodes if n.id == "product:checkout")
    assert anchor.is_anchor


def test_incoming_edges_expand_too(brain):
    # enterprise segment -> payment-declined (incoming to the issue)
    g = build_graph(brain, "issue:payment-declined", depth=1)
    ids = {n.id for n in g.nodes}
    assert "user_segment:enterprise" in ids  # via incoming edge
    assert "product:checkout" in ids  # via incoming edge
    assert "issue:threed-secure-timeout" in ids  # via outgoing edge


def test_depth_two_traverses_further(brain):
    g1 = build_graph(brain, "product:checkout", depth=1)
    g2 = build_graph(brain, "product:checkout", depth=2)
    assert len(g2.nodes) > len(g1.nodes)
    ids = {n.id for n in g2.nodes}
    # checkout -> payment-declined -> threed-secure-timeout
    assert "issue:threed-secure-timeout" in ids


def test_edges_carry_meaning(brain):
    g = build_graph(brain, "product:checkout", depth=1)
    meanings = {e.meaning for e in g.edges}
    assert "has common issue" in meanings


def test_nodes_are_type_colored(brain):
    g = build_graph(brain, "product:checkout", depth=1)
    product_node = next(n for n in g.nodes if n.id == "product:checkout")
    assert product_node.color == "#7c3aed"


def test_missing_anchor_returns_empty(brain):
    g = build_graph(brain, "product:nope", depth=1)
    assert g.nodes == []


def test_multiple_anchors(brain):
    g = build_graph(brain, ["product:checkout", "product:search"], depth=1)
    anchors = {n.id for n in g.nodes if n.is_anchor}
    assert anchors == {"product:checkout", "product:search"}
    ids = {n.id for n in g.nodes}
    assert "issue:no-results" in ids  # neighbor of product:search


def test_anchor_property(brain):
    g = build_graph(brain, ["product:checkout"], depth=1)
    assert g.anchor == "product:checkout"


# -- whole-index overview ------------------------------------------------------


def test_overview_includes_every_entity_in_scope(brain):
    from open_index.graph import build_overview_graph

    graph = build_overview_graph(brain, ["product"])
    assert {n.id for n in graph.nodes} == {
        e.id for e in brain.backend.all_entities(["product"])}


def test_overview_only_draws_edges_with_both_ends_in_scope(brain):
    """A half-edge to a filtered-out type would render as a line to nowhere."""
    from open_index.graph import build_overview_graph

    graph = build_overview_graph(brain, ["product"])
    ids = {n.id for n in graph.nodes}
    assert all(e.source in ids and e.target in ids for e in graph.edges)


def test_overview_keeps_edges_between_included_types(brain):
    from open_index.graph import build_overview_graph

    graph = build_overview_graph(brain, ["product", "issue"])
    assert graph.edges, "product→issue edges should survive when both are in scope"


def test_overview_colours_nodes_by_doc_type(brain):
    from open_index.graph import build_overview_graph

    graph = build_overview_graph(brain)
    by_type = {n.doc_type: n.color for n in graph.nodes}
    assert len(set(by_type.values())) > 1, "doc_types should be visually distinct"


def test_overview_cap_keeps_the_most_connected(brain):
    """When the cap bites, it must keep the hubs — a random subset of a graph
    is far less informative than its best-connected part."""
    from open_index.graph import build_overview_graph
    from open_index.ui.view import edge_counts

    kept = {n.id for n in build_overview_graph(brain, limit=3).nodes}
    assert len(kept) == 3

    degree = edge_counts(brain)
    dropped = {e.id for e in brain.backend.all_entities()} - kept
    assert min(degree.get(i, 0) for i in kept) >= max(degree.get(i, 0) for i in dropped)


def test_overview_of_an_empty_scope(brain):
    from open_index.graph import build_overview_graph

    graph = build_overview_graph(brain, ["nonexistent-type"])
    assert graph.nodes == [] and graph.edges == []


def test_overview_deduplicates_edges(brain):
    from open_index.graph import build_overview_graph

    graph = build_overview_graph(brain)
    keys = [(e.source, e.target, e.meaning) for e in graph.edges]
    assert len(keys) == len(set(keys))
