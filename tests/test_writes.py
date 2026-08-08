import json

from open_index.models import Entity
from open_index.schema import DocType, DocTypeDisplay, FieldSpec


def test_put_entity_persists_file_and_indexes(brain):
    e = Entity.from_dict({
        "doc_type": "issue",
        "id": "issue:new-one",
        "name": "A new issue",
        "severity": "low",
        "status": "open",
        "related_to": [{"target": "product:checkout", "relationship_edge_meaning": "affects"}],
    })
    path = brain.put_entity(e)
    # file written under entities/<doc_type>/<slug>.json
    assert path.exists()
    assert path.name == "new-one.json"
    on_disk = json.loads(path.read_text())
    assert on_disk["id"] == "issue:new-one"
    # queryable immediately
    assert brain.get_entity("issue:new-one") is not None
    assert ("issue:new-one", "product:checkout", "affects") in \
        brain.backend.relationships_from("issue:new-one")


def test_put_entity_rejects_unknown_doc_type(brain):
    e = Entity(id="ghost:x", doc_type="ghost", name="x")
    try:
        brain.put_entity(e)
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "unknown doc_type" in str(exc)


def test_create_doc_type_writes_yaml_and_registers(brain):
    dt = DocType(
        doc_type="incident",
        description="An incident",
        display=DocTypeDisplay(color="#111111"),
        fields=[FieldSpec(name="name", boost=6), FieldSpec(name="summary", type="text", search="semantic")],
    )
    path = brain.create_doc_type(dt)
    assert path.exists()
    assert "incident" in brain.config.doc_types
    # newly created type accepts entities
    e = Entity.from_dict({"doc_type": "incident", "id": "incident:i1", "name": "I1", "summary": "boom"})
    brain.put_entity(e)
    assert brain.get_entity("incident:i1") is not None

    # reloading the brain from disk sees the persisted doc_type
    from open_index.brain import Brain
    reopened = Brain.open(brain.config.root)
    assert "incident" in reopened.config.doc_types


def test_create_doc_type_duplicate_rejected(brain):
    dt = DocType(doc_type="product", description="dup")
    try:
        brain.create_doc_type(dt)
        assert False
    except ValueError as exc:
        assert "already exists" in str(exc)


def test_init_scaffolds_agent_skill(tmp_path):
    from open_index.scaffold import init_brain

    init_brain(tmp_path / "b", "b")
    skill = tmp_path / "b" / ".claude" / "skills" / "edit-brain" / "SKILL.md"
    assert skill.exists()
    text = skill.read_text()
    assert text.startswith("---")           # frontmatter
    assert "name: edit-brain" in text
    assert "put_entity" in text and "create_doc_type" in text
    claude_md = (tmp_path / "b" / "CLAUDE.md").read_text()
    assert "navigation_guidelines" not in claude_md  # runtime navigation comes from MCP prompt
    # and the optional Claude Code MCP wiring is present
    assert (tmp_path / "b" / ".mcp.json").exists()


def test_navigation_guidelines_markdown(brain):
    md = brain.navigation_guidelines()
    assert "Domain Context Instructions" in md
    assert "## Doc types" in md
    assert "### issue" in md
    assert "put_entity" in md          # write guidance present
    assert "search_brain" in md        # read guidance present
    assert "boost 6" in md             # field boost surfaced
