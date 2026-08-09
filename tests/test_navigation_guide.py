"""The navigation guide has to be enough on its own.

For a remote brain it is the only documentation an agent ever sees — CLAUDE.md
and the edit-brain skill are files on the brain host, invisible over MCP. These
tests pin the things an agent cannot author without: the id convention, the call
shapes, and the full schema vocabulary.
"""

import pytest

from open_index.brain import Brain
from open_index.config import BrainConfig


@pytest.fixture
def empty_brain(tmp_path):
    """A brain with no doc_types — a freshly-initialised one."""
    root = tmp_path / "fresh"
    root.mkdir()
    (root / "brain.yaml").write_text("name: fresh\ndescription: New.\n")
    return Brain.open(root)


# -- self-sufficiency ---------------------------------------------------------


def test_guide_teaches_the_id_convention(brain):
    """The single most common write failure."""
    assert "<doc_type>:<slug>" in brain.navigation_guidelines()


def test_guide_shows_concrete_call_shapes(brain):
    md = brain.navigation_guidelines()
    assert "put_entity(" in md
    assert "create_doc_type(" in md
    assert "relationship_edge_meaning" in md


def test_guide_documents_the_full_field_vocabulary(brain):
    """An agent can't write a field spec without the allowed values."""
    md = brain.navigation_guidelines()
    for token in ("syntactic", "semantic", "none", "keyword", "timestamp",
                  "boolean", "boost", "required"):
        assert token in md, f"field vocabulary missing {token!r}"


def test_guide_explains_the_storage_policy(brain):
    """storage: file|index is the least obvious concept in the product."""
    md = brain.navigation_guidelines()
    assert "storage" in md
    assert "no files written" in md.lower()
    assert "source of truth" in md.lower()


def test_guide_lists_full_field_detail_per_doc_type(brain):
    """Not just searchable names — types and required-ness too."""
    md = brain.navigation_guidelines()
    assert "**Fields:**" in md
    assert "`severity`" in md          # a non-name field of the example issue type
    assert "storage: file" in md


def test_guide_reports_relationship_vocabulary(brain):
    md = brain.navigation_guidelines()
    assert "Relationships (declared)" in md
    assert "Relationships (in use)" in md


def test_guide_pluralizes_counts(brain):
    md = brain.navigation_guidelines()
    assert "1 entities" not in md


# -- empty brain --------------------------------------------------------------


def test_empty_brain_gets_a_bootstrap_section(empty_brain):
    """The case where the inventory teaches nothing and the agent is most stuck."""
    md = empty_brain.navigation_guidelines()
    assert "empty" in md.lower()
    assert "create_doc_type" in md


def test_empty_brain_still_carries_the_full_vocabulary(empty_brain):
    md = empty_brain.navigation_guidelines()
    assert "<doc_type>:<slug>" in md
    assert "syntactic" in md and "semantic" in md
    assert "put_entity(" in md


def test_populated_brain_has_no_bootstrap_section(brain):
    assert "This brain is empty" not in brain.navigation_guidelines()


# -- read-only ----------------------------------------------------------------


def test_read_only_guide_omits_write_tools(brain):
    """Describing tools that aren't registered only misleads."""
    md = brain.navigation_guidelines(include_writes=False)
    assert "put_entity" not in md
    assert "create_doc_type" not in md
    # ...but reading is still fully documented.
    assert "search_brain" in md
    assert "## Doc types" in md


def test_read_write_guide_includes_write_tools(brain):
    assert "put_entity" in brain.navigation_guidelines(include_writes=True)


def test_required_fields_are_marked(brain):
    """An agent must be able to see which fields it cannot omit."""
    from open_index.schema import DocType, FieldSpec

    brain.create_doc_type(DocType(
        doc_type="task",
        fields=[FieldSpec(name="name"),
                FieldSpec(name="owner", required=True, description="Who owns it.")],
    ))
    md = brain.navigation_guidelines()
    assert "**required**" in md
    assert "Who owns it." in md


def test_examples_per_type_is_respected(brain):
    md = brain.navigation_guidelines(examples_per_type=1)
    examples_line = next(ln for ln in md.splitlines() if "**Examples:**" in ln)
    assert examples_line.count("`") == 2  # exactly one backticked id


# -- Brain guard rails --------------------------------------------------------


def test_add_entity_rejects_an_invalid_entity(brain):
    from open_index.models import Entity

    with pytest.raises(ValueError, match="invalid entity"):
        brain.add_entity(Entity(id="ghost:x", doc_type="ghost"))


def test_create_doc_type_needs_a_directory():
    """A brain built in memory has nowhere to persist a schema."""
    from open_index.schema import DocType

    config = BrainConfig(name="mem")
    b = Brain(config)
    with pytest.raises(RuntimeError, match="no directory"):
        b.create_doc_type(DocType(doc_type="x"))


def test_index_on_a_rootless_brain_is_a_no_op():
    assert Brain(BrainConfig(name="mem")).index() == 0
