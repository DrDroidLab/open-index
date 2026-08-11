"""Exercise the MCP server's read AND write tools by actually calling them."""

import asyncio
import json

import pytest

pytest.importorskip("mcp")  # skip if the optional dep isn't installed


def _text(res) -> str:
    """Pull the text payload out of an MCP call_tool result across shapes."""
    content = res.content if hasattr(res, "content") else (res[0] if isinstance(res, tuple) else res)
    item = content[0] if isinstance(content, list) else content
    return getattr(item, "text", str(item))


class _Server:
    def __init__(self, brain):
        from open_index.mcp_server import build_server

        self.server = build_server(brain)
        self.loop = asyncio.new_event_loop()

    def call(self, name, args=None):
        return _text(self.loop.run_until_complete(self.server.call_tool(name, args or {})))

    def tools(self):
        return {t.name for t in self.loop.run_until_complete(self.server.list_tools())}


@pytest.fixture
def srv(brain):
    return _Server(brain)


def test_default_server_exposes_read_and_write_tools(srv):
    assert {"navigation_guidelines", "search_brain", "get_entity",
            "get_entities", "lookup_by_external_id",
            "put_entity", "put_entities", "create_doc_type",
            "delete_entity"} == srv.tools()


def test_default_prompt_positions_brain_as_read_write_domain_context(srv):
    instructions = getattr(srv.server, "instructions", "") or ""
    assert "context layer for this domain-specialized agent" in instructions
    assert "Read and write access is the default" in instructions


def test_read_only_mode_hides_write_tools(brain):
    import asyncio

    from open_index.mcp_server import build_server

    server = build_server(brain, read_only=True)
    names = {t.name for t in asyncio.new_event_loop().run_until_complete(server.list_tools())}
    assert {"navigation_guidelines", "search_brain", "get_entity"} <= names
    assert "put_entity" not in names and "create_doc_type" not in names
    instructions = getattr(server, "instructions", "") or ""
    assert "put_entity" not in instructions


# ---- read tools ---------------------------------------------------------- #

def test_navigation_guidelines_tool(srv):
    md = srv.call("navigation_guidelines")
    assert "Domain Context Instructions" in md
    assert "## Doc types" in md
    assert "put_entity" in md  # write guidance surfaced too


def test_search_brain_tool(srv):
    out = json.loads(srv.call("search_brain", {"query": "payment"}))
    assert out["total"] >= 1
    assert "doc_type_counts" in out
    assert any(r["id"] == "issue:payment-declined" for r in out["results"])


def test_search_brain_doc_type_filter(srv):
    out = json.loads(srv.call("search_brain", {"query": "checkout", "doc_types": ["product"]}))
    assert all(r["doc_type"] == "product" for r in out["results"])


def test_get_entity_tool(srv):
    out = json.loads(srv.call("get_entity", {"entity_id": "product:checkout"}))
    assert out["name"] == "Checkout"
    assert "relationships" in out
    outgoing = {(r["target"], r["meaning"]) for r in out["relationships"]["outgoing"]}
    assert ("issue:payment-declined", "has common issue") in outgoing


def test_get_entity_missing(srv):
    out = json.loads(srv.call("get_entity", {"entity_id": "product:nope"}))
    assert "error" in out


# ---- write tools --------------------------------------------------------- #

def test_create_doc_type_tool(srv, brain):
    out = json.loads(srv.call("create_doc_type", {
        "doc_type": "ticket",
        "description": "A support ticket",
        "fields": [{"name": "name", "boost": 6}, {"name": "status", "search": "syntactic"}],
        "storage": "index",
    }))
    assert out["ok"] is True
    assert "ticket" in brain.config.doc_types
    assert brain.config.doc_type("ticket").storage == "index"


def test_put_entity_tool(srv, brain):
    # issue is file-backed in the example -> a file is written
    out = json.loads(srv.call("put_entity", {
        "doc_type": "issue",
        "id": "issue:from-mcp",
        "name": "Filed via MCP",
        "fields": {"severity": "low", "status": "open"},
        "related_to": [{"target": "product:checkout", "relationship_edge_meaning": "affects"}],
    }))
    assert out["ok"] is True
    assert out["path"].endswith("issue/from-mcp.json")
    # queryable immediately + edge stored
    got = json.loads(srv.call("get_entity", {"entity_id": "issue:from-mcp"}))
    assert got["name"] == "Filed via MCP"
    assert brain.get_entity("issue:from-mcp") is not None


def test_put_entity_unknown_doc_type(srv):
    out = json.loads(srv.call("put_entity", {"doc_type": "ghost", "id": "ghost:x", "name": "x"}))
    assert "error" in out and "unknown doc_type" in out["error"]


def test_create_then_put_roundtrip(srv, brain):
    srv.call("create_doc_type", {"doc_type": "memory", "description": "learnings",
                                 "fields": [{"name": "name", "boost": 4}], "storage": "index"})
    out = json.loads(srv.call("put_entity", {
        "doc_type": "memory", "id": "memory:m1", "name": "A learning"}))
    assert out["ok"] is True
    # index-backed -> no file written
    assert out["path"] is None
    assert brain.get_entity("memory:m1") is not None
