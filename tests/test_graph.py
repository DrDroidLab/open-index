from droid_brain.graph import build_graph


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
