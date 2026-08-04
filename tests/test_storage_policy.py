"""The file|index storage policy: where a doc_type's entities live."""

from droid_brain.brain import Brain
from droid_brain.models import Entity
from droid_brain.schema import DocType, FieldSpec


def _add_index_doc_type(brain):
    dt = DocType(
        doc_type="memory",
        description="agent learnings",
        storage="index",  # DB is source of truth
        fields=[FieldSpec(name="name", boost=4), FieldSpec(name="body", type="text", search="semantic")],
    )
    brain.create_doc_type(dt)


def test_index_backed_entity_not_written_to_file(brain):
    _add_index_doc_type(brain)
    e = Entity.from_dict({"doc_type": "memory", "id": "memory:m1", "name": "m1", "body": "learned X"})
    path = brain.put_entity(e)
    assert path is None  # nothing written to disk
    assert not (brain.config.root / "entities" / "memory").exists()
    assert brain.get_entity("memory:m1") is not None  # but queryable


def test_file_backed_entity_is_written(brain):
    # 'issue' is storage:file in the example
    e = Entity.from_dict({"doc_type": "issue", "id": "issue:w", "name": "w", "severity": "low", "status": "open"})
    path = brain.put_entity(e)
    assert path is not None and path.exists()


def test_reindex_preserves_index_backed_but_reloads_file_backed(brain):
    _add_index_doc_type(brain)
    brain.put_entity(Entity.from_dict({"doc_type": "memory", "id": "memory:keep", "name": "keep", "body": "b"}))

    before = brain.counts()
    brain.index()  # reconciles file-backed types only
    after = brain.counts()

    # index-backed 'memory' survived the reindex (it has no file to reload from)
    assert brain.get_entity("memory:keep") is not None
    assert after.get("memory") == 1
    # file-backed types reloaded to the same counts
    assert after["product"] == before["product"] == 2


def test_reindex_ignores_stray_index_backed_files(brain, tmp_path):
    """A JSON file for an index-backed type is not auto-loaded by index()."""
    _add_index_doc_type(brain)
    stray = brain.config.root / "entities" / "memory" / "stray.json"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text('{"doc_type": "memory", "id": "memory:stray", "name": "stray"}')
    brain.index()
    assert brain.get_entity("memory:stray") is None  # DB owns index-backed types


def test_storage_persisted_in_yaml(brain):
    _add_index_doc_type(brain)
    reopened = Brain.open(brain.config.root)
    assert reopened.config.doc_type("memory").storage == "index"
