"""Semantic/vector search tests — run offline with a deterministic fake embedder.

OpenSearch-backed tests live in tests/test_opensearch_integration.py behind the
OPENSEARCH_URL env var. This module covers the SQLite backend and the shared
provider/hybrid logic.
"""

import shutil
from pathlib import Path

import pytest

from open_index.brain import Brain
from open_index.config import load_brain_config
from open_index.embeddings import FakeEmbedProvider
from open_index.models import Entity
from open_index.schema import DocType, FieldSpec
from open_index.storage import get_backend

SUPPORT = Path(__file__).resolve().parent.parent / "examples" / "support-brain"


def _open_support_with_embeddings(tmp_path):
    """Return a Brain over a copy of support-brain with a fake embedding provider."""
    dst = tmp_path / "support"
    shutil.copytree(SUPPORT, dst)
    cfg = load_brain_config(dst)
    backend = get_backend(cfg)
    backend._embedding_provider = FakeEmbedProvider(dim=32)
    brain = Brain(cfg, backend=backend)
    brain.index()
    return brain


def test_semantic_recall_keyword_disjoint_sqlite(tmp_path):
    """A query with no keyword overlap with the description still finds the entity."""
    brain = _open_support_with_embeddings(tmp_path)
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


def test_hybrid_keeps_name_match_on_top_sqlite(tmp_path):
    """A pure keyword name match still outranks semantic-only candidates."""
    brain = _open_support_with_embeddings(tmp_path)
    res = brain.search("payment", doc_types=["issue"], limit=10)
    assert res.results[0]["id"] == "issue:payment-declined"


def test_degrades_gracefully_without_provider(tmp_path, caplog):
    """Without an embedding provider, semantic-declared fields fall back to keyword-only."""
    dst = tmp_path / "support"
    shutil.copytree(SUPPORT, dst)
    brain = Brain.open(dst)
    brain.index()
    # Force the provider to be absent regardless of whether fastembed is installed.
    brain.backend._embedding_provider = None
    brain.backend._embedding_provider_initialized = True
    brain.backend._warned_no_provider = False

    with caplog.at_level("WARNING", logger="open_index.storage.sqlite"):
        res = brain.search("card authentication broken", doc_types=["issue"], limit=10)

    assert res.results is not None
    assert res.total >= 0
    assert any("no embedding provider" in m.lower() for m in caplog.messages)


def test_storage_policy_preserved_after_index_and_reembed(tmp_path):
    """Reindex + reembed preserve index-backed entities."""
    brain = _open_support_with_embeddings(tmp_path)
    dt = DocType(
        doc_type="memory",
        description="agent learnings",
        storage="index",
        fields=[
            FieldSpec(name="name", boost=4),
            FieldSpec(name="body", type="text", search="semantic"),
        ],
    )
    brain.create_doc_type(dt)
    brain.put_entity(
        Entity.from_dict(
            {"doc_type": "memory", "id": "memory:keep", "name": "keep", "body": "b"}
        )
    )

    brain.index()
    brain.reembed()

    assert brain.get_entity("memory:keep") is not None
    assert brain.counts().get("memory") == 1


def test_sqlite_backfill_on_open(tmp_path, monkeypatch):
    """Opening a brain with a provider backfills embeddings when the table is empty."""
    import open_index.embeddings as _emb

    dst = tmp_path / "support"
    shutil.copytree(SUPPORT, dst)
    # Open with no provider so the embeddings table starts empty.
    monkeypatch.setattr(_emb, "get_embedding_provider", lambda _config: None)
    brain = Brain.open(dst)
    brain.index()

    before = brain.backend._conn.execute(
        "SELECT COUNT(*) FROM entity_embeddings"
    ).fetchone()[0]
    assert before == 0

    brain.backend._embedding_provider = FakeEmbedProvider(dim=32)
    brain.backend.ensure_schema(brain.config.doc_types)

    after = brain.backend._conn.execute(
        "SELECT COUNT(*) FROM entity_embeddings"
    ).fetchone()[0]
    entities = brain.backend._conn.execute(
        "SELECT COUNT(*) FROM entities"
    ).fetchone()[0]
    assert after == entities


def test_fake_embedder_is_deterministic():
    """The fake provider returns the same vectors for the same texts."""
    p = FakeEmbedProvider(dim=32)
    v1 = p.encode(["credit card refused issuer"])[0]
    v2 = p.encode(["credit card refused issuer"])[0]
    assert v1 == v2
    # Synonymous phrases should be closer than unrelated ones.
    gold = p.encode(["Shoppers see their payment authorization rejected at the bank"])[0]
    unrelated = p.encode(["the quick brown fox jumps over the lazy dog"])[0]
    from open_index.embeddings import cosine_similarity

    assert cosine_similarity(v1, gold) > cosine_similarity(v1, unrelated)


def test_backfill_only_semantic_doc_types(tmp_path, monkeypatch):
    """The backfill guard counts only semantic doc_types, not all entities."""
    import open_index.embeddings as _emb

    from open_index.storage.base import semantic_doc_types

    dst = tmp_path / "support"
    shutil.copytree(SUPPORT, dst)
    monkeypatch.setattr(_emb, "get_embedding_provider", lambda _config: None)
    brain = Brain.open(dst)
    brain.index()

    # Add a non-semantic doc_type and one entity.
    dt = DocType(
        doc_type="note",
        description="plain notes",
        storage="index",
        fields=[FieldSpec(name="name", boost=4)],
    )
    brain.create_doc_type(dt)
    brain.put_entity(Entity.from_dict({"doc_type": "note", "id": "note:n1", "name": "n1"}))

    semantic_types = semantic_doc_types(brain.config.doc_types)
    placeholders = ",".join("?" * len(semantic_types))
    semantic_entities = brain.backend._conn.execute(
        f"SELECT COUNT(*) FROM entities WHERE doc_type IN ({placeholders})",
        list(semantic_types),
    ).fetchone()[0]
    total_entities = brain.backend._conn.execute(
        "SELECT COUNT(*) FROM entities"
    ).fetchone()[0]
    assert total_entities > semantic_entities

    # Attach a provider and backfill; only semantic entities should get embeddings.
    brain.backend._embedding_provider = FakeEmbedProvider(dim=32)
    brain.backend.ensure_schema(brain.config.doc_types)
    emb_count = brain.backend._conn.execute(
        "SELECT COUNT(*) FROM entity_embeddings"
    ).fetchone()[0]
    assert emb_count == semantic_entities


def test_dimension_mismatch_warns_and_skips(tmp_path, monkeypatch, caplog):
    """SQLite warns once when stored embedding dimensions don't match the provider."""
    import open_index.embeddings as _emb
    import struct

    dst = tmp_path / "support"
    shutil.copytree(SUPPORT, dst)
    monkeypatch.setattr(_emb, "get_embedding_provider", lambda _config: None)
    brain = Brain.open(dst)
    brain.index()

    # Seed an embedding with the wrong dimension.
    brain.backend._conn.execute(
        "INSERT INTO entity_embeddings (entity_id, model, vector, updated_at) VALUES (?, ?, ?, datetime('now'))",
        ("issue:payment-declined", "old", struct.pack("16f", *([0.0] * 16))),
    )
    brain.backend._conn.commit()

    brain.backend._embedding_provider = FakeEmbedProvider(dim=32)
    with caplog.at_level("WARNING", logger="open_index.storage.sqlite"):
        brain.search("payment", doc_types=["issue"], limit=10)

    assert any("open-index index --reembed" in m.lower() for m in caplog.messages)


def test_keyword_mode_never_constructs_provider(tmp_path):
    """semantic_weight=0.0 (Keyword mode) must not even lazily build the provider
    — the zero-config promise: pure-keyword users never pay for the model.
    (index() DOES embed when a provider exists — that's by design — so reset
    the lazy-init flags after indexing and prove search stays keyword-only.)"""
    dst = tmp_path / "support"
    shutil.copytree(SUPPORT, dst)
    cfg = load_brain_config(dst)
    backend = get_backend(cfg)
    brain = Brain(cfg, backend=backend)
    brain.index()
    backend._embedding_provider = None
    backend._embedding_provider_initialized = False
    res = brain.search("payment", doc_types=["issue"], semantic_weight=0.0)
    assert res.total >= 1
    assert backend._embedding_provider_initialized is False  # still untouched
    # ...while a hybrid search on the same semantic brain does construct it
    brain.search("payment", doc_types=["issue"])
    assert backend._embedding_provider_initialized is True


def test_semantic_only_mode_ordering(tmp_path):
    """semantic_weight=1.0 ranks purely by embedding similarity."""
    brain = _open_support_with_embeddings(tmp_path)
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
    res = brain.search("credit card refused issuer", doc_types=["issue"], limit=10,
                       semantic_weight=1.0)
    assert res.results[0]["id"] == "issue:gold-card-decline"


def test_weight_override_does_not_mutate_config(tmp_path):
    """Per-call override is scoped to the call; the brain default is unchanged."""
    brain = _open_support_with_embeddings(tmp_path)
    brain.search("payment", doc_types=["issue"], semantic_weight=1.0)
    assert brain.config.search.semantic_weight == 0.3


# -- embedding cache location -------------------------------------------------
#
# fastembed defaults to a temp directory, which a container discards on every
# recreate — a ~90MB re-download per restart. Deployments point it somewhere
# persistent via OPEN_INDEX_EMBEDDING_CACHE.


def test_cache_dir_is_passed_through_to_fastembed(monkeypatch):
    captured = {}

    class FakeTextEmbedding:
        def __init__(self, model_name, **kwargs):
            captured.update(model_name=model_name, **kwargs)

    import sys
    import types

    fake = types.ModuleType("fastembed")
    fake.TextEmbedding = FakeTextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", fake)

    from open_index.embeddings import FastEmbedProvider

    FastEmbedProvider("some/model", cache_dir="/var/cache/oi")
    assert captured["cache_dir"] == "/var/cache/oi"


def test_no_cache_dir_leaves_fastembed_default(monkeypatch):
    """Passing cache_dir=None explicitly would override fastembed's default."""
    captured = {}

    class FakeTextEmbedding:
        def __init__(self, model_name, **kwargs):
            captured.update(model_name=model_name, **kwargs)

    import sys
    import types

    fake = types.ModuleType("fastembed")
    fake.TextEmbedding = FakeTextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", fake)

    from open_index.embeddings import FastEmbedProvider

    FastEmbedProvider("some/model")
    assert "cache_dir" not in captured


def test_provider_cache_is_keyed_on_the_cache_dir(monkeypatch, tmp_path):
    """Two different cache dirs must not share one cached provider."""
    import open_index.embeddings as emb
    from open_index.config import load_brain_config

    emb._provider_cache.clear()
    config = load_brain_config(_copy_support(tmp_path))

    monkeypatch.setenv("OPEN_INDEX_EMBEDDING_CACHE", "/cache/a")
    emb.get_embedding_provider(config)
    monkeypatch.setenv("OPEN_INDEX_EMBEDDING_CACHE", "/cache/b")
    emb.get_embedding_provider(config)

    assert len(emb._provider_cache) == 2, "cache_dir is not part of the cache key"


def _copy_support(tmp_path):
    dst = tmp_path / "support"
    shutil.copytree(SUPPORT, dst)
    return dst
