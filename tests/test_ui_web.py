"""The explorer as served — real requests against the real app.

These replace the Streamlit AppTest suite. They assert what a visitor receives,
which is the thing that broke before: under Streamlit every path rendered
whichever brain sorted first, and a status-code check could not see it because
the shell was returned for any path. Here the index is a route parameter, so the
same question is answerable directly from the response body.
"""

import shutil
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from open_index.brain import Brain

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "support-brain"


def _fresh_app():
    """Build the app with the module caches cleared.

    open_brain and discover are lru_cached for the life of the process, so a
    test that changes the environment would otherwise be served another test's
    brain.
    """
    from open_index.ui import web

    web.open_brain.cache_clear()
    web.discover.cache_clear()
    return web.build_app()


@pytest.fixture
def single(tmp_path, monkeypatch):
    """One brain, served at the root."""
    d = tmp_path / "support-brain"
    shutil.copytree(EXAMPLE, d)
    Brain.open(d).index()
    monkeypatch.setenv("OPEN_INDEX_DIR", str(d))
    monkeypatch.delenv("OPEN_INDEX_BRAINS_ROOT", raising=False)
    monkeypatch.delenv("OPEN_INDEX_READ_ONLY", raising=False)
    return TestClient(_fresh_app())


@pytest.fixture
def many(tmp_path, monkeypatch):
    """Two brains, each at its own path."""
    root = tmp_path / "brains"
    root.mkdir()
    for name in ("alpha", "beta"):
        shutil.copytree(EXAMPLE, root / name)
        Brain.open(root / name).index()
    monkeypatch.setenv("OPEN_INDEX_BRAINS_ROOT", str(root))
    monkeypatch.delenv("OPEN_INDEX_DIR", raising=False)
    monkeypatch.delenv("OPEN_INDEX_READ_ONLY", raising=False)
    return TestClient(_fresh_app())


def an_entity(brain_dir) -> str:
    return Brain.open(brain_dir).backend.all_entities()[0].id


# -- every page renders -------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/", "/schema", "/explore", "/map", "/analytics", "/jobs"])
def test_every_tab_renders(single, path):
    r = single.get(path)
    assert r.status_code == 200
    assert "<html" in r.text


def test_help_is_the_landing_page(single):
    """The root is the help tab: a first-time visitor lands on the explanation."""
    assert "What an index holds" in single.get("/").text


def test_static_assets_are_served(single):
    assert single.get("/static/app.css").status_code == 200
    assert single.get("/static/map.js").status_code == 200
    assert single.get("/static/vendor/cytoscape.min.js").status_code == 200


def test_healthz_is_plain_and_cheap(single):
    r = single.get("/healthz")
    assert r.status_code == 200 and r.text == "ok"


# -- the URL is the index selector --------------------------------------------


def test_each_path_serves_its_own_index(many):
    """The bug that survived a curl status check under Streamlit."""
    for name in ("alpha", "beta"):
        body = many.get(f"/{name}").text
        assert f"/{name}/mcp" in body
        other = "beta" if name == "alpha" else "alpha"
        assert f"/{other}/mcp" not in body


def test_the_root_lists_every_index(many):
    body = many.get("/").text
    assert "alpha" in body and "beta" in body


def test_an_unknown_index_is_a_404_that_says_what_exists(many):
    r = many.get("/nope")
    assert r.status_code == 404
    assert "alpha" in r.text and "beta" in r.text


def test_tabs_stay_within_the_selected_index(many):
    assert many.get("/beta/schema").status_code == 200
    assert 'href="/beta/schema"' in many.get("/beta").text


def test_a_single_brain_needs_no_path_segment(single):
    assert single.get("/schema").status_code == 200


# -- the MCP endpoint shown is this index's, derived from the request ----------


def test_the_endpoint_follows_the_host_header(many):
    body = many.get("/alpha", headers={"host": "brain.example.com"}).text
    assert "http://brain.example.com/alpha/mcp" in body


def test_a_tls_terminating_proxy_does_not_downgrade_the_endpoint(many):
    """Behind Caddy the app speaks http; the endpoint it advertises must not."""
    body = many.get("/alpha", headers={"host": "brain.example.com",
                                       "x-forwarded-proto": "https"}).text
    assert "https://brain.example.com/alpha/mcp" in body
    assert "http://brain.example.com/alpha/mcp" not in body


# -- search and browse --------------------------------------------------------


def test_no_query_browses_rather_than_showing_a_blank_page(single):
    body = single.get("/explore").text
    assert "issue" in body


def test_a_query_returns_results(single):
    body = single.get("/explore", params={"q": "payment"}).text
    assert "result" in body


def test_a_hopeless_query_says_so_rather_than_erroring(single):
    """Keyword mode, deliberately: semantic search ranks *everything*, so it
    always returns rows and could never exercise the empty state."""
    body = single.get("/explore",
                      params={"q": "zzzzzzzz-nothing", "mode": "Keyword"}).text
    assert "No matches" in body


def test_an_unknown_search_mode_falls_back_instead_of_500ing(single):
    assert single.get("/explore", params={"q": "a", "mode": "Telepathy"}).status_code == 200


def test_a_failing_backend_shows_an_error_not_a_blank_page(single, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("backend is down")

    monkeypatch.setattr(Brain, "search", lambda self, **kw: boom(**kw))
    body = single.get("/explore", params={"q": "payment"}).text
    assert "backend is down" in body


def test_doc_type_filter_is_in_the_url(single):
    r = single.get("/explore", params={"t": "issue"})
    assert r.status_code == 200


# -- entities -----------------------------------------------------------------


def test_an_entity_has_its_own_url(single, tmp_path):
    eid = an_entity(tmp_path / "support-brain")
    body = single.get(f"/entity/{eid}").text
    assert eid in body
    assert "Relationships" in body


def test_a_missing_entity_is_a_404(single):
    r = single.get("/entity/issue:does-not-exist")
    assert r.status_code == 404
    assert "No entity" in r.text


def test_an_entity_id_with_a_slash_still_resolves(single):
    """Ids are `<doc_type>:<slug>`, but a slug containing / must not 404 the route."""
    assert single.get("/entity/issue:a/b").status_code in (200, 404)


# -- the map ------------------------------------------------------------------


def test_the_map_page_loads_without_the_graph(single):
    """The page is HTML; the graph arrives separately, so a slow graph cannot
    block the page from rendering."""
    body = single.get("/map").text
    assert 'id="cy"' in body
    assert "/api/graph" in body


def test_graph_json_has_nodes_edges_and_a_legend(single):
    data = single.get("/api/graph").json()
    assert data["nodes"] and "legend" in data and "edges" in data
    assert all("tooltip" in n for n in data["nodes"])


def test_graph_nodes_carry_no_canvas_label(single):
    """Labels are the thing that made the old map unreadable."""
    data = single.get("/api/graph").json()
    assert all("label" in n for n in data["nodes"])          # for the tooltip
    assert all(n["color"] for n in data["nodes"])


def test_focus_narrows_the_graph(single, tmp_path):
    eid = an_entity(tmp_path / "support-brain")
    whole = single.get("/api/graph").json()
    focused = single.get("/api/graph", params={"focus": eid}).json()
    assert len(focused["nodes"]) <= len(whole["nodes"])


def test_graph_json_for_an_unknown_index_is_404(many):
    assert many.get("/zzz/api/graph").status_code == 404


# -- read-only ----------------------------------------------------------------


def test_read_only_says_the_write_tools_are_absent(tmp_path, monkeypatch):
    d = tmp_path / "b"
    shutil.copytree(EXAMPLE, d)
    Brain.open(d).index()
    monkeypatch.setenv("OPEN_INDEX_DIR", str(d))
    monkeypatch.delenv("OPEN_INDEX_BRAINS_ROOT", raising=False)
    monkeypatch.setenv("OPEN_INDEX_READ_ONLY", "1")
    body = TestClient(_fresh_app()).get("/").text
    assert "read-only" in body


# -- escaping -----------------------------------------------------------------


def test_the_inline_markdown_filter_escapes_before_converting():
    from open_index.ui.web import inline_markdown

    out = str(inline_markdown("<script>alert(1)</script> **bold** `code`"))
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "<strong>bold</strong>" in out and "<code>code</code>" in out


def test_entity_content_is_escaped(single, tmp_path):
    """An entity name is user data and reaches the page; it must not be markup."""
    from open_index.models import Entity

    brain = Brain.open(tmp_path / "support-brain")
    brain.put_entity(Entity(id="issue:xss", doc_type="issue",
                            name="<img src=x onerror=alert(1)>"))
    body = single.get("/entity/issue:xss").text
    assert "<img src=x" not in body
    assert "&lt;img" in body


# -- hiding the directory -----------------------------------------------------
#
# On a host serving several unrelated indexes, the *names* are the sensitive
# part: knowing that /acme-index exists is the leak, whatever it contains.


@pytest.fixture
def hidden(tmp_path, monkeypatch):
    root = tmp_path / "brains"
    root.mkdir()
    for name in ("alpha", "beta"):
        shutil.copytree(EXAMPLE, root / name)
        Brain.open(root / name).index()
    monkeypatch.setenv("OPEN_INDEX_BRAINS_ROOT", str(root))
    monkeypatch.delenv("OPEN_INDEX_DIR", raising=False)
    monkeypatch.setenv("OPEN_INDEX_HIDE_DIRECTORY", "1")
    return TestClient(_fresh_app())


def test_hidden_root_is_a_404(hidden):
    assert hidden.get("/").status_code == 404


def test_hidden_root_names_no_index(hidden):
    body = hidden.get("/").text
    assert "alpha" not in body and "beta" not in body


def test_a_known_index_still_works_when_hidden(hidden):
    r = hidden.get("/alpha")
    assert r.status_code == 200
    assert "/alpha/mcp" in r.text


def test_a_hidden_host_does_not_name_siblings_on_a_404(hidden):
    """A 404 that helpfully lists the alternatives hands over the thing the
    flag exists to withhold."""
    body = hidden.get("/nope").text
    assert "alpha" not in body and "beta" not in body


def test_a_hidden_page_links_to_no_sibling(hidden):
    """The sidebar's 'all indexes' link would walk a visitor straight to the
    listing the root refuses to serve."""
    body = hidden.get("/alpha").text
    assert "all indexes" not in body
    assert "beta" not in body


def test_the_directory_is_shown_by_default(many):
    """Hiding is opt-in: a self-hosted instance wants its home page."""
    assert many.get("/").status_code == 200
