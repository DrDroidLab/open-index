"""Write-path affordances an authoring agent depends on.

Covers the gap that sent a user to the GitHub README: relationships were not
declarable over MCP at all, and failures came back without saying how to fix them.
"""

import asyncio
import json

import pytest

pytest.importorskip("mcp")

import yaml  # noqa: E402


def _text(res) -> str:
    content = res.content if hasattr(res, "content") else (res[0] if isinstance(res, tuple) else res)
    item = content[0] if isinstance(content, list) else content
    return getattr(item, "text", str(item))


class _Server:
    def __init__(self, brain, read_only=False):
        from open_index.mcp_server import build_server

        self.server = build_server(brain, read_only=read_only)
        self.loop = asyncio.new_event_loop()

    def call(self, name, args=None):
        return _text(self.loop.run_until_complete(self.server.call_tool(name, args or {})))


@pytest.fixture
def srv(brain):
    return _Server(brain)


# -- declaring relationships over MCP -----------------------------------------


def test_create_doc_type_accepts_relationships(srv, brain):
    out = json.loads(srv.call("create_doc_type", {
        "doc_type": "runbook",
        "description": "A procedure.",
        "storage": "file",
        "fields": [{"name": "name", "type": "string", "search": "syntactic", "boost": 6}],
        "relationships": [{"name": "resolves", "target_doc_type": "issue"}],
    }))
    assert out["ok"] is True

    spec = brain.config.doc_type("runbook").relationship("resolves")
    assert spec is not None and spec.target_doc_type == "issue"


def test_declared_relationships_persist_to_yaml(srv, brain):
    """The YAML file is the git source of truth — it must round-trip."""
    srv.call("create_doc_type", {
        "doc_type": "runbook",
        "fields": [{"name": "name"}],
        "relationships": [{"name": "resolves", "target_doc_type": "issue"}],
    })
    written = yaml.safe_load((brain.config.root / "doc_types" / "runbook.yaml").read_text())
    assert written["relationships"] == [{"name": "resolves", "target_doc_type": "issue"}]


def test_declared_relationships_are_then_validated(srv, brain):
    """Declaring a target type makes wrong-target edges an error."""
    srv.call("create_doc_type", {
        "doc_type": "runbook",
        "storage": "file",
        "fields": [{"name": "name"}],
        "relationships": [{"name": "resolves", "target_doc_type": "issue"}],
    })
    out = json.loads(srv.call("put_entity", {
        "doc_type": "runbook", "id": "runbook:r1", "name": "R1",
        # product:checkout exists and is a product, not an issue.
        "related_to": [{"target": "product:checkout",
                        "relationship_edge_meaning": "resolves"}],
    }))
    assert "error" in out
    assert "issue" in out["error"]


def test_create_doc_type_suggests_next_step(srv):
    out = json.loads(srv.call("create_doc_type", {"doc_type": "memory",
                                                  "fields": [{"name": "name"}]}))
    assert "put_entity" in out["next"]


# -- errors that teach --------------------------------------------------------


def test_unknown_doc_type_lists_the_known_ones(srv):
    out = json.loads(srv.call("put_entity", {"doc_type": "ghost", "id": "ghost:x"}))
    assert "issue" in out["known_doc_types"]
    assert "create_doc_type" in out["hint"]


def test_mismatched_id_prefix_shows_the_convention(srv):
    out = json.loads(srv.call("put_entity", {"doc_type": "issue", "id": "oops"}))
    assert "issue:<slug>" in out["hint"]


def test_bad_id_prefix_is_reported_not_raised(srv):
    """A prefix naming a *different* real doc_type is still a mismatch."""
    out = json.loads(srv.call("put_entity", {"doc_type": "issue", "id": "product:x"}))
    assert "error" in out


def test_related_to_missing_target_is_explained(srv):
    out = json.loads(srv.call("put_entity", {
        "doc_type": "issue", "id": "issue:x",
        "related_to": [{"relationship_edge_meaning": "affects"}],
    }))
    assert "target" in out["error"]
    assert "product:checkout" in out["hint"]


def test_invalid_field_spec_points_at_the_vocabulary(srv):
    out = json.loads(srv.call("create_doc_type", {
        "doc_type": "bad", "storage": "nonsense", "fields": [{"name": "name"}],
    }))
    assert "error" in out
    assert "navigation_guidelines" in out["hint"]


def test_schema_validation_error_reports_known_fields(srv, brain):
    """Required-field failures should say what the schema actually has."""
    srv.call("create_doc_type", {
        "doc_type": "task",
        "fields": [{"name": "name"}, {"name": "owner", "required": True}],
    })
    out = json.loads(srv.call("put_entity", {"doc_type": "task", "id": "task:t1",
                                             "name": "T1"}))
    assert "error" in out
    assert "owner" in out["known_fields"]


# -- provenance over the MCP boundary -----------------------------------------
#
# The provenance tests elsewhere exercise Entity.from_dict directly. These go
# through the tool, which is where the input schema is enforced — a nested
# provenance object is rejected before the body runs if `related_to` is
# annotated dict[str, str] rather than dict[str, Any].


def test_entity_provenance_and_validity_round_trip(srv, brain):
    out = json.loads(srv.call("put_entity", {
        "doc_type": "issue", "id": "issue:attributed", "name": "Attributed",
        "provenance": {"asserted_by": "agent:x", "asserted_at": "2026-01-01T00:00:00Z",
                       "confidence": 0.9, "evidence": "seen in logs"},
        "valid_from": "2026-01-01T00:00:00Z",
        "valid_to": "2026-02-01T00:00:00Z",
    }))
    assert out["ok"] is True

    stored = brain.get_entity("issue:attributed")
    assert stored.provenance.asserted_by == "agent:x"
    assert stored.provenance.confidence == 0.9
    assert stored.valid_from == "2026-01-01T00:00:00Z"
    assert stored.valid_to == "2026-02-01T00:00:00Z"


def test_per_edge_provenance_survives_the_tool_schema(srv, brain):
    """Regression: a nested dict inside related_to must not be schema-rejected."""
    out = json.loads(srv.call("put_entity", {
        "doc_type": "issue", "id": "issue:edged", "name": "Edged",
        "related_to": [{
            "target": "product:checkout",
            "relationship_edge_meaning": "affects",
            "provenance": {"asserted_by": "agent:x", "confidence": 0.4},
        }],
    }))
    assert out["ok"] is True, out

    edge = brain.get_entity("issue:edged").related_to[0]
    assert edge.provenance is not None, "per-edge provenance was dropped"
    assert edge.provenance.confidence == 0.4


def test_entity_and_edge_provenance_are_independent(srv, brain):
    """A well-attributed entity can carry a guessed edge."""
    srv.call("put_entity", {
        "doc_type": "issue", "id": "issue:mixed", "name": "Mixed",
        "provenance": {"asserted_by": "human:dipesh", "confidence": 1.0},
        "related_to": [{"target": "product:checkout",
                        "relationship_edge_meaning": "affects",
                        "provenance": {"asserted_by": "agent:x", "confidence": 0.2}}],
    })
    stored = brain.get_entity("issue:mixed")
    assert stored.provenance.confidence == 1.0
    assert stored.related_to[0].provenance.confidence == 0.2


def test_malformed_entity_provenance_is_reported_as_provenance(srv):
    """Not as a schema-field error — the two have different fixes."""
    out = json.loads(srv.call("put_entity", {
        "doc_type": "issue", "id": "issue:badprov",
        "provenance": {"confidence": 5.0},
    }))
    assert "invalid provenance" in out["error"]
    assert "confidence" in out["hint"]
    assert "known_fields" not in out


def test_malformed_edge_provenance_is_reported(srv):
    out = json.loads(srv.call("put_entity", {
        "doc_type": "issue", "id": "issue:badedge",
        "related_to": [{"target": "product:checkout",
                        "provenance": {"confidence": -1}}],
    }))
    assert "invalid provenance" in out["error"]


def test_omitting_provenance_stays_clean(srv, brain):
    """Unattributed writes must not gain an empty provenance block."""
    srv.call("put_entity", {"doc_type": "issue", "id": "issue:plain", "name": "Plain"})
    stored = brain.get_entity("issue:plain")
    assert stored.provenance is None
    assert stored.valid_from is None


def test_provenance_is_documented_in_the_tool_docstring(brain):
    """The docstring is the only place an agent learns these arguments exist."""
    from open_index.mcp_server import build_server

    server = build_server(brain)
    tools = asyncio.new_event_loop().run_until_complete(server.list_tools())
    doc = next(t for t in tools if t.name == "put_entity").description
    assert "asserted_by" in doc
    assert "confidence" in doc
    assert "valid_from" in doc


# -- read-only ----------------------------------------------------------------


def test_read_only_guide_matches_registered_tools(brain):
    """The guide must not advertise write tools a read-only server doesn't expose."""
    srv = _Server(brain, read_only=True)
    assert "put_entity" not in srv.call("navigation_guidelines")
