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

from open_index.ui import view  # noqa: E402

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


    # -- analytics ------------------------------------------------------------


def test_analytics_tab_renders_on_a_cold_brain(populated):
    """No usage recorded yet must be an explanation, not a crash or a blank."""
    text = " ".join(i.value for i in populated.info) + " ".join(
        c.value for c in populated.caption)
    assert "Analytics" in [t.label for t in populated.tabs]
    assert not populated.exception
    assert "~/.local/state/open-index/" in text or "recorded" in text


def test_searching_is_recorded_as_ui_usage(populated):
    """A UI search must show up in analytics, or the usage picture has a hole."""
    populated.text_input[0].set_value("payment").run()
    assert not populated.exception

    from open_index.brain import Brain

    import os
    summary = Brain.open(os.environ["OPEN_INDEX_DIR"]).analytics_summary()
    assert summary["total_fetches"] >= 1
    assert "ui" in summary["by_source"]


def test_opening_an_entity_is_recorded_as_ui_usage(populated):
    populated.text_input[0].set_value("checkout").run()
    next(b for b in populated.button if "product:checkout" in b.label).click().run()

    import os

    from open_index.brain import Brain

    summary = Brain.open(os.environ["OPEN_INDEX_DIR"]).analytics_summary()
    assert "get_entity" in summary["by_operation"]


def test_empty_brain_explains_the_next_step(empty):
    text = " ".join(c.value for c in empty.caption) + " ".join(
        i.value for i in empty.info)
    assert "no entities" in text.lower()


# -- dark mode ----------------------------------------------------------------


def test_row_css_is_theme_agnostic():
    """A hardcoded white row background rendered white-on-white under the dark
    theme, which kept its light text — the entity list became invisible."""
    style = view.ROW_CSS.lower()
    assert "background:#fff" not in style
    assert "background:transparent" in style
    assert "color:inherit" in style


def test_row_css_inherits_colour_through_the_label_element():
    """Streamlit wraps button labels in <p>, which otherwise keeps its own
    colour and ignores the inherit on the button."""
    assert "> button p{color:inherit" in view.ROW_CSS


# -- How to use ----------------------------------------------------------------


def test_how_to_use_is_the_rightmost_tab(populated):
    assert [t.label for t in populated.tabs][-1] == "How to use"


def test_tab_guide_matches_the_tabs_actually_rendered(populated):
    """A stale list of what-each-tab-does is worse than none."""
    assert [t.label for t in populated.tabs] == [n for n, _ in view.TAB_GUIDE]


def test_documented_tools_match_the_registered_mcp_tools(brain):
    """The tab must not advertise a tool the server does not expose, nor miss one."""
    pytest.importorskip("mcp")
    import asyncio as _asyncio

    from open_index.mcp_server import build_server

    server = build_server(brain)
    registered = {t.name for t in
                  _asyncio.new_event_loop().run_until_complete(server.list_tools())}
    documented = {name.split("(")[0] for name, _ in view.READ_TOOLS + view.WRITE_TOOLS}
    assert documented == registered, (
        f"docs vs server mismatch: only-in-docs={documented - registered}, "
        f"only-on-server={registered - documented}")


def test_read_only_tools_are_the_documented_read_set(brain):
    pytest.importorskip("mcp")
    import asyncio as _asyncio

    from open_index.mcp_server import build_server

    server = build_server(brain, read_only=True)
    registered = {t.name for t in
                  _asyncio.new_event_loop().run_until_complete(server.list_tools())}
    assert {n.split("(")[0] for n, _ in view.READ_TOOLS} == registered


def test_connection_block_is_valid_json_with_the_url():
    import json

    block = json.loads(view.mcp_client_config("https://x.example.com/demo/mcp", "demo"))
    assert block["mcpServers"]["demo"]["url"] == "https://x.example.com/demo/mcp"
    assert block["mcpServers"]["demo"]["type"] == "http"


def test_connection_block_appends_the_mcp_path():
    import json

    block = json.loads(view.mcp_client_config("https://x.example.com/demo"))
    assert block["mcpServers"]["open-index"]["url"].endswith("/mcp")
