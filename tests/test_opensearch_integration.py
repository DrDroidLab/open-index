"""Live OpenSearch round-trip — runs only when OPENSEARCH_URL is set.

    OPENSEARCH_URL=http://localhost:9200 pytest tests/test_opensearch_integration.py

Spin up a throwaway cluster first, e.g.:
    docker run -d -p 9200:9200 -e discovery.type=single-node \
      -e DISABLE_SECURITY_PLUGIN=true opensearchproject/opensearch:2.13.0
"""

import os

import pytest

OS_URL = os.environ.get("OPENSEARCH_URL")
pytestmark = pytest.mark.skipif(not OS_URL, reason="set OPENSEARCH_URL to run")


@pytest.fixture
def os_brain(tmp_path):
    import shutil

    from droid_brain.brain import Brain
    from droid_brain.config import load_brain_config

    example = os.path.join(os.path.dirname(__file__), "..", "examples", "infra-brain")
    dst = tmp_path / "infra"
    shutil.copytree(example, dst)

    cfg = load_brain_config(dst)
    cfg.search.backend = "opensearch"
    cfg.search.hosts = [OS_URL]
    cfg.search.index = f"droid_brain_test_{os.getpid()}"

    from droid_brain.storage import get_backend

    backend = get_backend(cfg)
    brain = Brain(cfg, backend=backend)
    backend.clear()
    brain.index()
    yield brain
    backend.clear()


def test_search_and_boost(os_brain):
    res = os_brain.search("checkout")
    assert res.total >= 1
    # service name (boost 6) should rank the service first
    assert res.results[0]["doc_type"] == "service"


def test_counts_only(os_brain):
    res = os_brain.search("postgres", counts_only=True)
    assert res.results == []
    assert sum(res.doc_type_counts.values()) == res.total


def test_get_and_relationships(os_brain):
    e = os_brain.get_entity("service:checkout")
    assert e is not None and e.name == "Checkout Service"
    incoming = os_brain.backend.relationships_to("datastore:postgres-main")
    assert any(src == "service:checkout" for src, _t, _m in incoming)


def test_fuzzy_match(os_brain):
    # typo tolerance is the point of the OpenSearch backend
    res = os_brain.search("chekout")   # missing 'c'
    assert any(r["doc_type"] == "service" for r in res.results)


def test_storage_policy_preserved_on_reindex(os_brain):
    from droid_brain.connectors.runner import ingest

    ingest(os_brain, "infra-alerts")           # index-backed alerts
    assert os_brain.get_entity("alert:checkout-5xx") is not None
    os_brain.index()                            # reconciles file-backed only
    assert os_brain.get_entity("alert:checkout-5xx") is not None  # survived
