"""Live OpenSearch round-trip — runs only when OPENSEARCH_URL is set.

    OPENSEARCH_URL=http://localhost:9200 pytest tests/test_opensearch_integration.py

Spin up a throwaway cluster first, e.g.:
    docker run -d -p 9200:9200 -e discovery.type=single-node \
      -e DISABLE_SECURITY_PLUGIN=true opensearchproject/opensearch:2.13.0
"""

import os
import shutil

import pytest

from droid_brain.brain import Brain
from droid_brain.config import load_brain_config
from droid_brain.embeddings import FakeEmbedProvider
from droid_brain.models import Entity
from droid_brain.storage import get_backend

OS_URL = os.environ.get("OPENSEARCH_URL")
pytestmark = pytest.mark.skipif(not OS_URL, reason="set OPENSEARCH_URL to run")


@pytest.fixture
def os_brain(tmp_path):
    example = os.path.join(os.path.dirname(__file__), "..", "examples", "infra-brain")
    dst = tmp_path / "infra"
    shutil.copytree(example, dst)

    cfg = load_brain_config(dst)
    cfg.search.backend = "opensearch"
    cfg.search.hosts = [OS_URL]
    cfg.search.index = f"droid_brain_test_{os.getpid()}"

    backend = get_backend(cfg)
    brain = Brain(cfg, backend=backend)
    backend.clear()
    brain.index()
    yield brain
    backend.clear()


def test_search_and_boost(os_brain):
    res = os_brain.search("checkout")
    assert res.total >= 1
    # name matches (boost 6) must outrank description-only matches. The service
    # ("Checkout Service") and the dashboard ("Checkout Latency") tie on _score —
    # both names match at equal boost — so assert on the name-match GROUP, not
    # a single winner (deterministic tie-break is by name.kw).
    scores = {r["id"]: r["score"] for r in res.results}
    name_hits = {"service:checkout", "dashboard:checkout-latency"}
    top = {r["id"] for r in res.results[:2]}
    assert top == name_hits
    desc_only = [s for i, s in scores.items() if i not in name_hits]
    assert all(s < min(scores[i] for i in name_hits) for s in desc_only)


def test_unreachable_cluster_gives_clean_error(os_brain):
    # Point a backend at a dead port: ensure_schema must exit with an
    # actionable message, not a raw ConnectionError traceback.
    import pytest as _pytest

    from droid_brain.storage.opensearch_backend import OpenSearchBackend

    cfg = os_brain.config
    cfg.search.hosts = ["http://localhost:9999"]
    with _pytest.raises(SystemExit, match="cannot reach OpenSearch"):
        OpenSearchBackend(cfg).ensure_schema({})


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


@pytest.fixture
def os_brain_semantic(tmp_path):
    """OpenSearch-backed copy of support-brain with a fake embedding provider."""
    example = os.path.join(os.path.dirname(__file__), "..", "examples", "support-brain")
    dst = tmp_path / "support"
    shutil.copytree(example, dst)
    cfg = load_brain_config(dst)
    cfg.search.backend = "opensearch"
    cfg.search.hosts = [OS_URL]
    cfg.search.index = f"droid_brain_sem_test_{os.getpid()}"
    backend = get_backend(cfg)
    backend._embedding_provider = FakeEmbedProvider(dim=32)
    brain = Brain(cfg, backend=backend)
    backend.clear()
    brain.index()
    yield brain
    backend.clear()


def test_semantic_knn_recalls(os_brain_semantic):
    """A keyword-disjoint query finds the conceptually related entity via k-NN."""
    brain = os_brain_semantic
    brain.put_entity(
        Entity.from_dict(
            {
                "doc_type": "issue",
                "id": "issue:gold-card-decline",
                "name": "Payment Rejection Spike",
                "description": "Shoppers see their payment authorization rejected at the bank step during purchase confirmation.",
                "severity": "high",
                "status": "open",
            }
        )
    )

    res = brain.search("credit card refused issuer", doc_types=["issue"], limit=10)
    ids = [r["id"] for r in res.results]
    assert "issue:gold-card-decline" in ids


def test_semantic_knn_migration_on_existing_index(tmp_path):
    """An index created without the embedding field can be migrated live."""
    example = os.path.join(os.path.dirname(__file__), "..", "examples", "infra-brain")
    dst = tmp_path / "infra"
    shutil.copytree(example, dst)
    cfg = load_brain_config(dst)
    cfg.search.backend = "opensearch"
    cfg.search.hosts = [OS_URL]
    cfg.search.index = f"droid_brain_migrate_test_{os.getpid()}"

    backend = get_backend(cfg)
    backend._embedding_provider = None
    backend._embedding_provider_initialized = True
    # Create an old-style index without embedding / knn.
    backend._client.indices.create(index=backend.index, body=backend._base_mapping())
    brain = Brain(cfg, backend=backend)
    brain.index()

    # Now attach a provider and ensure_schema should migrate the mapping.
    backend._embedding_provider = FakeEmbedProvider(dim=32)
    backend.ensure_schema(cfg.doc_types)
    mapping = backend._client.indices.get_mapping(index=backend.index)
    assert "embedding" in mapping[backend.index]["mappings"]["properties"]
    # Existing docs remain searchable after the migration.
    assert brain.get_entity("service:checkout") is not None

    backend.clear()


def test_degrades_without_provider_os(os_brain_semantic, caplog):
    """Without an embedding provider, OpenSearch falls back to keyword-only search."""
    backend = os_brain_semantic.backend
    backend._embedding_provider = None
    backend._embedding_provider_initialized = True
    backend._warned_no_provider = False
    with caplog.at_level("WARNING", logger="droid_brain.storage.opensearch"):
        res = os_brain_semantic.search("card authentication broken", doc_types=["issue"])
    assert res.results is not None
    assert res.total >= 0
    assert any("no embedding provider" in m.lower() for m in caplog.messages)


def test_knn_doc_type_filter_and_counts(os_brain_semantic):
    """k-NN filter respects doc_types and doc_type_counts match the keyword arm."""
    brain = os_brain_semantic
    brain.put_entity(
        Entity.from_dict(
            {
                "doc_type": "issue",
                "id": "issue:gold-card-decline",
                "name": "Payment Rejection Spike",
                "description": "Shoppers see their payment authorization rejected at the bank step during purchase confirmation.",
                "severity": "high",
                "status": "open",
            }
        )
    )

    res = brain.search("credit card refused issuer", doc_types=["issue"], limit=10)
    assert all(r["doc_type"] == "issue" for r in res.results)
    assert res.doc_type_counts.get("issue")
    # Counts-only keyword arm should agree on the same filtered total shape.
    counts = brain.search("credit card refused issuer", doc_types=["issue"], counts_only=True)
    assert counts.total == sum(counts.doc_type_counts.values())


def test_dimension_mismatch_raises(os_brain_semantic):
    """A configured dimension that differs from the existing index mapping is caught early."""
    backend = os_brain_semantic.backend
    # The fixture index was created with dim=32.
    backend._embedding_provider = FakeEmbedProvider(dim=64)
    backend._warned_no_provider = False
    with pytest.raises(SystemExit, match="Recreate the index"):
        backend.ensure_schema(os_brain_semantic.config.doc_types)
