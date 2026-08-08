"""The explorer actually runs.

Executes the Streamlit script with AppTest, which surfaces exceptions the way a
browser would. These guard the specific complaints that prompted the rebuild:
structure you had to hunt for, and a map that opened blank.
"""

import shutil
from pathlib import Path

import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402

APP = Path(__file__).resolve().parent.parent / "open_index" / "ui" / "app.py"
EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "support-brain"


def _run(brain_dir, monkeypatch):
    monkeypatch.setenv("OPEN_INDEX_DIR", str(brain_dir))
    return AppTest.from_file(str(APP), default_timeout=60).run()


@pytest.fixture
def populated(tmp_path, monkeypatch):
    from open_index.brain import Brain

    dst = tmp_path / "support"
    shutil.copytree(EXAMPLE, dst)
    Brain.open(dst).index()
    return _run(dst, monkeypatch)


@pytest.fixture
def empty(tmp_path, monkeypatch):
    root = tmp_path / "fresh"
    root.mkdir()
    (root / "brain.yaml").write_text("name: fresh\ndescription: New.\n")
    return _run(root, monkeypatch)


# -- it runs ------------------------------------------------------------------


def test_app_runs_without_exceptions(populated):
    assert not populated.exception, [e.value for e in populated.exception]


def test_empty_brain_runs_without_exceptions(empty):
    """The first thing a new user sees must not be a stack trace."""
    assert not empty.exception, [e.value for e in empty.exception]


def test_three_tabs(populated):
    assert [t.label for t in populated.tabs] == ["Explore", "Map", "Jobs"]


# -- structure is visible without hunting for it ------------------------------


def test_sidebar_names_the_brain(populated):
    assert any("support-brain" in m.value for m in populated.sidebar.markdown)


def test_sidebar_lists_every_doc_type(populated):
    text = " ".join(m.value for m in populated.sidebar.markdown)
    for name in ("issue", "product", "comment", "user_segment"):
        assert name in text, f"{name} missing from the sidebar"


def test_sidebar_shows_counts_and_storage_policy(populated):
    text = " ".join(m.value for m in populated.sidebar.markdown)
    assert "entities" in " ".join(c.value for c in populated.sidebar.caption)
    assert "file" in text or "index" in text


def test_empty_brain_sidebar_explains_what_to_do(empty):
    text = " ".join(c.value for c in empty.sidebar.caption)
    assert "add-doc-type" in text


# -- the map no longer opens blank --------------------------------------------


def test_map_preselects_anchors(populated):
    """The original complaint: nothing rendered until you made a selection."""
    anchors = next(m for m in populated.multiselect if m.label == "Anchors")
    assert anchors.value, "map opened with no anchors selected"


def test_map_anchor_options_are_entity_labels(populated):
    anchors = next(m for m in populated.multiselect if m.label == "Anchors")
    assert all("(" in option and ")" in option for option in anchors.options)


def test_map_doc_type_filter_defaults_to_everything(populated):
    types = next(m for m in populated.multiselect if m.label == "Doc types to include")
    assert set(types.value) == set(types.options)


# -- explore ------------------------------------------------------------------


def test_search_box_is_present(populated):
    assert populated.text_input, "expected a search box"


def test_browse_rows_are_rendered_without_a_query(populated):
    """Idle state lists entities rather than showing an empty page."""
    assert len(populated.button) > 1


def test_searching_narrows_to_matches(populated):
    populated.text_input[0].set_value("payment").run()
    assert not populated.exception
    labels = " ".join(b.label for b in populated.button)
    assert "payment" in labels.lower()


def test_opening_an_entity_shows_its_relationships(populated):
    populated.text_input[0].set_value("checkout").run()
    target = next(b for b in populated.button if "product:checkout" in b.label)
    target.click().run()

    assert not populated.exception
    text = " ".join(m.value for m in populated.markdown)
    assert "Relationships" in text
    assert "Checkout" in text


def test_back_returns_to_the_list(populated):
    populated.text_input[0].set_value("checkout").run()
    next(b for b in populated.button if "product:checkout" in b.label).click().run()
    next(b for b in populated.button if "back" in b.label).click().run()
    assert not populated.exception


def test_empty_brain_explains_the_next_step(empty):
    text = " ".join(c.value for c in empty.caption) + " ".join(
        i.value for i in empty.info)
    assert "no entities" in text.lower()
