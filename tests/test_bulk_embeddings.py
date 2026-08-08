"""Batched embedding on the bulk write path.

`upsert_many` exists mostly to collapse N embedding calls into one — that is the
larger of its two savings — so this asserts both that it happens and that the
vectors it stores are the same ones the per-entity path would have written.
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


class CountingProvider(FakeEmbedProvider):
    """A fake embedder that records how it was called."""

    def __init__(self, dim: int = 32):
        super().__init__(dim=dim)
        self.calls: list[int] = []          # batch size of each encode()

    def encode(self, texts):
        self.calls.append(len(texts))
        return super().encode(texts)


@pytest.fixture
def embedding_brain(tmp_path):
    dst = tmp_path / "support"
    shutil.copytree(SUPPORT, dst)
    config = load_brain_config(dst)
    backend = get_backend(config)
    backend._embedding_provider = CountingProvider(dim=32)
    brain = Brain(config, backend=backend)
    brain.index()
    backend._embedding_provider.calls.clear()
    return brain


def _issues(n, prefix):
    # `description` is the semantic field on the example issue doc_type.
    return [
        Entity(id=f"issue:{prefix}{i}", doc_type="issue", name=f"{prefix}{i}",
               fields={"description": f"a description of thing number {i}"})
        for i in range(n)
    ]


def test_batch_uses_one_encode_call(embedding_brain):
    provider = embedding_brain.backend._embedding_provider
    embedding_brain.put_entities(_issues(10, "batch"))
    assert provider.calls == [10], f"expected one call of 10, got {provider.calls}"


def test_per_entity_path_still_encodes_one_at_a_time(embedding_brain):
    """Contrast case — this is the cost upsert_many removes."""
    provider = embedding_brain.backend._embedding_provider
    for entity in _issues(5, "single"):
        embedding_brain.put_entity(entity)
    assert provider.calls == [1, 1, 1, 1, 1]


def test_batched_vectors_match_the_single_write_path(embedding_brain):
    """Same entity, either path, same stored vector."""
    backend = embedding_brain.backend
    entity = Entity(id="issue:cmp", doc_type="issue", name="Cmp",
                    fields={"description": "a distinctive description"})

    backend.upsert_entity(entity, embedding_brain.config.doc_type("issue"))
    single = backend._load_embeddings(["issue:cmp"], 32)["issue:cmp"]

    backend.upsert_many([(entity, embedding_brain.config.doc_type("issue"))])
    batched = backend._load_embeddings(["issue:cmp"], 32)["issue:cmp"]

    assert batched == pytest.approx(single)


def test_batched_entities_are_semantically_searchable(embedding_brain):
    embedding_brain.put_entities([Entity(
        id="issue:findme", doc_type="issue", name="Findme",
        fields={"description": "shoppers cannot complete their purchase"},
    )])
    results = embedding_brain.search("buyers unable to check out",
                                     doc_types=["issue"], semantic_weight=1.0)
    assert "issue:findme" in [r["id"] for r in results.results]


def test_doc_types_without_semantic_fields_never_encode(embedding_brain):
    """A non-semantic batch must not even construct the provider — that would
    load a model to do nothing."""
    provider = embedding_brain.backend._embedding_provider
    embedding_brain.create_doc_type(DocType(
        doc_type="plain", fields=[FieldSpec(name="name", search="syntactic")]))
    embedding_brain.put_entities([
        Entity(id=f"plain:{i}", doc_type="plain", name=f"P{i}") for i in range(5)
    ])
    assert provider.calls == []


def test_mixed_batch_encodes_only_the_semantic_rows(embedding_brain):
    provider = embedding_brain.backend._embedding_provider
    embedding_brain.create_doc_type(DocType(
        doc_type="plain", fields=[FieldSpec(name="name", search="syntactic")]))
    embedding_brain.put_entities(
        _issues(3, "mix") + [Entity(id="plain:x", doc_type="plain", name="X")]
    )
    assert provider.calls == [3]


def test_batch_without_a_provider_is_a_noop(brain):
    """No embeddings installed: the write still lands, just without vectors."""
    brain.backend._embedding_provider = None
    brain.backend._embedding_provider_initialized = True
    result = brain.put_entities(_issues(3, "noprov"))
    assert result.written == 3
    assert brain.get_entity("issue:noprov0") is not None


def test_empty_batch_does_not_encode(embedding_brain):
    provider = embedding_brain.backend._embedding_provider
    embedding_brain.put_entities([])
    assert provider.calls == []
