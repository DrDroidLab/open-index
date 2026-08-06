"""Semantic/vector search tests — run offline with a deterministic fake embedder.

OpenSearch-backed tests live in tests/test_opensearch_integration.py behind the
OPENSEARCH_URL env var. This module covers the SQLite backend and the shared
provider/hybrid logic.
"""

import shutil
from pathlib import Path

import pytest

from droid_brain.brain import Brain
from droid_brain.config import load_brain_config
from droid_brain.embeddings import FakeEmbedProvider
from droid_brain.models import Entity
from droid_brain.schema import DocType, FieldSpec
from droid_brain.storage import get_backend

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

    with caplog.at_level("WARNING", logger="droid_brain.storage.sqlite"):
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
    import droid_brain.embeddings as _emb

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
    from droid_brain.embeddings import cosine_similarity

    assert cosine_similarity(v1, gold) > cosine_similarity(v1, unrelated)


def test_backfill_only_semantic_doc_types(tmp_path, monkeypatch):
    """The backfill guard counts only semantic doc_types, not all entities."""
    import droid_brain.embeddings as _emb

    from droid_brain.storage.base import semantic_doc_types

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
    import droid_brain.embeddings as _emb
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
    with caplog.at_level("WARNING", logger="droid_brain.storage.sqlite"):
        brain.search("payment", doc_types=["issue"], limit=10)

    assert any("droid-brain index --reembed" in m.lower() for m in caplog.messages)
