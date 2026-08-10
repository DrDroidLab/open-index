"""Serving many brains from one process.

A process per brain re-loads the Python runtime and a ~250MB resident embedding
model each time, which caps a modest host at a handful. These cover the mounted
app: routing, per-brain isolation, per-brain auth, and the lifespan wiring that
the MCP transport needs.
"""

import json
import shutil
from pathlib import Path

import pytest

pytest.importorskip("mcp")
pytest.importorskip("starlette")

from starlette.testclient import TestClient  # noqa: E402

from open_index.mcp_server import (  # noqa: E402
    build_multi_app,
    discover_brains,
    token_for,
)

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "support-brain"

HANDSHAKE = {
    "jsonrpc": "2.0", "id": "1", "method": "initialize",
    "params": {"protocolVersion": "2024-11-05", "capabilities": {},
               "clientInfo": {"name": "test", "version": "0"}},
}
HEADERS = {"Content-Type": "application/json",
           "Accept": "application/json, text/event-stream"}


def client_for(app):
    """A test client whose Host header is one the server accepts.

    Starlette's default is `testserver`, which the SDK's DNS-rebinding
    protection rightly rejects with 421 — the same check that makes serving
    behind a proxy require an explicit allow-list.
    """
    return TestClient(app, base_url="http://localhost")


@pytest.fixture
def brains_root(tmp_path):
    """Two independent brains side by side, plus a decoy directory."""
    from open_index.brain import Brain

    root = tmp_path / "brains"
    root.mkdir()
    for name in ("alpha", "beta"):
        shutil.copytree(EXAMPLE, root / name)
        Brain.open(root / name).index()
    (root / "not-a-brain").mkdir()          # no brain.yaml
    (root / "notes.txt").write_text("x")     # not a directory
    return root


# -- discovery ----------------------------------------------------------------


def test_discovers_only_directories_holding_a_brain_yaml(brains_root):
    assert sorted(discover_brains(str(brains_root))) == ["alpha", "beta"]


def test_discovery_rejects_a_missing_root(tmp_path):
    with pytest.raises(SystemExit, match="no such directory"):
        discover_brains(str(tmp_path / "nope"))


def test_empty_root_discovers_nothing(tmp_path):
    assert discover_brains(str(tmp_path)) == {}


# -- per-brain tokens ---------------------------------------------------------


def test_token_comes_from_a_per_brain_variable(monkeypatch):
    monkeypatch.setenv("OPEN_INDEX_TOKEN_SALES_EU", "specific")
    assert token_for("sales-eu", "shared") == "specific"


def test_token_falls_back_to_the_shared_one(monkeypatch):
    monkeypatch.delenv("OPEN_INDEX_TOKEN_SALES", raising=False)
    assert token_for("sales", "shared") == "shared"


def test_no_token_at_all_is_none(monkeypatch):
    monkeypatch.delenv("OPEN_INDEX_TOKEN_SALES", raising=False)
    assert token_for("sales") is None


# -- routing ------------------------------------------------------------------


def test_each_brain_answers_on_its_own_path(brains_root):
    with client_for(build_multi_app(discover_brains(str(brains_root)))) as client:
        for name in ("alpha", "beta"):
            response = client.post(f"/{name}/mcp", json=HANDSHAKE, headers=HEADERS)
            assert response.status_code == 200, response.text
            assert "serverInfo" in response.text


def test_an_unknown_brain_path_is_a_404(brains_root):
    with client_for(build_multi_app(discover_brains(str(brains_root)))) as client:
        assert client.post("/ghost/mcp", json=HANDSHAKE, headers=HEADERS).status_code == 404


def test_the_directory_lists_every_brain(brains_root):
    with client_for(build_multi_app(discover_brains(str(brains_root)))) as client:
        listing = client.get("/").json()
    assert set(listing) == {"alpha", "beta"}
    assert listing["alpha"]["entities"] > 0
    assert "issue" in listing["alpha"]["doc_types"]


def test_health_is_unauthenticated_and_leaks_nothing(brains_root):
    app = build_multi_app(discover_brains(str(brains_root)), token="secret")
    with client_for(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.text.strip() == "ok"


def test_public_base_url_is_advertised_per_brain(brains_root):
    app = build_multi_app(discover_brains(str(brains_root)),
                          public_base_url="https://x.example.com")
    with client_for(app) as client:
        listing = client.get("/").json()
    assert listing["alpha"]["mcp"] == "https://x.example.com/alpha/mcp"


# -- isolation ----------------------------------------------------------------


def test_a_brains_token_does_not_open_another(brains_root, monkeypatch):
    monkeypatch.setenv("OPEN_INDEX_TOKEN_ALPHA", "alpha-token")
    monkeypatch.setenv("OPEN_INDEX_TOKEN_BETA", "beta-token")
    app = build_multi_app(discover_brains(str(brains_root)))

    with client_for(app) as client:
        ok = client.post("/alpha/mcp", json=HANDSHAKE,
                         headers={**HEADERS, "Authorization": "Bearer alpha-token"})
        crossed = client.post("/beta/mcp", json=HANDSHAKE,
                              headers={**HEADERS, "Authorization": "Bearer alpha-token"})
    assert ok.status_code == 200
    assert crossed.status_code == 401


def test_an_untokened_brain_stays_open_when_others_are_gated(brains_root, monkeypatch):
    """A missing per-brain token must not silently inherit a neighbour's."""
    monkeypatch.setenv("OPEN_INDEX_TOKEN_ALPHA", "alpha-token")
    monkeypatch.delenv("OPEN_INDEX_TOKEN_BETA", raising=False)
    app = build_multi_app(discover_brains(str(brains_root)))

    with client_for(app) as client:
        assert client.post("/alpha/mcp", json=HANDSHAKE, headers=HEADERS).status_code == 401
        assert client.post("/beta/mcp", json=HANDSHAKE, headers=HEADERS).status_code == 200


def test_writing_to_one_brain_does_not_touch_the_other(brains_root):
    """Separate storage, not a shared index with a tenant column."""
    from open_index.brain import Brain
    from open_index.models import Entity

    Brain.open(brains_root / "alpha").put_entity(
        Entity(id="issue:only-alpha", doc_type="issue", name="Only Alpha"))

    assert Brain.open(brains_root / "alpha").get_entity("issue:only-alpha") is not None
    assert Brain.open(brains_root / "beta").get_entity("issue:only-alpha") is None


def test_read_only_applies_to_every_mounted_brain(brains_root):
    app = build_multi_app(discover_brains(str(brains_root)), read_only=True)
    with client_for(app) as client:
        assert client.get("/").json()["alpha"]["read_only"] is True


# -- the shared embedding model, which is the whole point ---------------------


def test_all_brains_share_one_embedding_provider(brains_root):
    """One process per brain re-loads the model each time; that duplication is
    what caps a host at a handful of brains."""
    import open_index.embeddings as emb
    from open_index.config import load_brain_config

    emb._provider_cache.clear()
    for name in ("alpha", "beta"):
        emb.get_embedding_provider(load_brain_config(brains_root / name))

    assert len(emb._provider_cache) == 1, (
        "each brain built its own provider — the model would be loaded per brain")


def test_lifespan_starts_every_mounted_session_manager(brains_root):
    """Starlette does not run a mounted app's lifespan. Without wiring it up,
    requests fail with 'Task group is not initialized' — so this asserts a real
    request works for the *last* mount, not just the first."""
    with client_for(build_multi_app(discover_brains(str(brains_root)))) as client:
        assert client.post("/beta/mcp", json=HANDSHAKE, headers=HEADERS).status_code == 200


def test_a_foreign_host_header_is_still_rejected(brains_root):
    """The proxy allow-list applies per mount, not only to a single-brain serve."""
    app = build_multi_app(discover_brains(str(brains_root)))
    with TestClient(app, base_url="http://evil.example.com") as client:
        assert client.post("/alpha/mcp", json=HANDSHAKE, headers=HEADERS).status_code == 421


def test_the_public_host_is_accepted(brains_root):
    app = build_multi_app(discover_brains(str(brains_root)),
                          public_base_url="https://brain.acme.com")
    with TestClient(app, base_url="https://brain.acme.com") as client:
        assert client.post("/alpha/mcp", json=HANDSHAKE, headers=HEADERS).status_code == 200
