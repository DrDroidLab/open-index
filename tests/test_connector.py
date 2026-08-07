from open_index.connectors.base import Connector, EntitySpec
from open_index.connectors.runner import discover_connectors, run_connector


def test_extractor_discovery():
    class C(Connector):
        name = "c"

        def extract_a(self):
            yield EntitySpec(doc_type="issue", id="issue:a")

        def extract_b(self):
            yield EntitySpec(doc_type="issue", id="issue:b")

        def helper(self):
            return []

    assert C().extractor_methods() == ["extract_a", "extract_b"]


def test_run_connector_creates_entities(brain):
    class C(Connector):
        name = "c"

        def extract_x(self):
            yield EntitySpec(
                doc_type="issue",
                id="issue:from-connector",
                name="Connector issue",
                fields={"severity": "low", "status": "open"},
                related_to=[("product:checkout", "affects product")],
            )

    result = run_connector(brain, C)
    assert result.created == 1
    e = brain.get_entity("issue:from-connector")
    assert e is not None
    edges = brain.backend.relationships_from("issue:from-connector")
    assert ("issue:from-connector", "product:checkout", "affects product") in edges


def test_example_connector_discovered_and_runs(brain):
    found = discover_connectors(brain)
    assert "example-issues" in found
    result = run_connector(brain, found["example-issues"])
    # demo extractor produces two issues offline
    assert result.created == 2
    assert brain.get_entity("issue:cart-abandonment") is not None
