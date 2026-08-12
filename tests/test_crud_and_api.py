"""Delete, external ids, and the JSON HTTP API.

The properties worth defending:

  a delete reaches the file. For a `storage: file` doc_type the JSON on disk is
  the source of truth, so deleting only the index row is a pause, not a delete —
  the entity returns on the next reindex.

  a delete takes its edges with it, in both directions. An edge left pointing at
  a removed entity still renders on the map and in neighbour lists, and reads as
  corruption rather than as a deletion.

  writes are gated only when a token is configured, and reads never are.
"""

import os
import shutil

import pytest
from starlette.testclient import TestClient

from open_index.brain import Brain
from open_index.models import Entity, Relationship

EXAMPLE = "examples/support-brain"


@pytest.fixture
def brain(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    d = tmp_path / "b"
    shutil.copytree(EXAMPLE, d)
    b = Brain.open(d)
    b.index()
    return b


# -- delete --------------------------------------------------------------------


def test_delete_removes_the_entity(brain):
    target = brain.backend.all_entities()[0].id
    assert brain.delete_entity(target) is True
    assert brain.get_entity(target) is None


def test_delete_removes_the_file_so_a_reindex_does_not_resurrect_it(brain):
    """The failure this guards: delete the row, reindex, and it is back."""
    entity = next(e for e in brain.backend.all_entities()
                  if e.doc_type in brain._file_backed_types())
    path = brain.entity_path(entity)
    assert path.exists()

    brain.delete_entity(entity.id)
    assert not path.exists()

    brain.index()
    assert brain.get_entity(entity.id) is None


def test_delete_removes_edges_pointing_at_it(brain):
    """An edge surviving its target renders as a dangling node on the map."""
    target = brain.backend.all_entities()[0]
    brain.put_entity(Entity(
        id="issue:linker", doc_type="issue", name="linker",
        related_to=[Relationship(target=target.id,
                                 relationship_edge_meaning="refers to")]))
    assert brain.backend.relationships_to(target.id)

    brain.delete_entity(target.id)
    assert brain.backend.relationships_to(target.id) == []
    assert brain.backend.relationships_from("issue:linker") == []


def test_deleting_something_absent_is_false_not_an_error(brain):
    assert brain.delete_entity("issue:never-existed") is False


def test_delete_is_idempotent(brain):
    target = brain.backend.all_entities()[0].id
    assert brain.delete_entity(target) is True
    assert brain.delete_entity(target) is False


def test_a_delete_that_cannot_remove_the_file_deletes_nothing(brain, monkeypatch):
    """Better to fail wholly than to unindex an entity whose file remains: that
    file silently restores it on the next reindex."""
    entity = next(e for e in brain.backend.all_entities()
                  if e.doc_type in brain._file_backed_types())

    def refuse(self, missing_ok=False):
        raise OSError("read-only file system")

    monkeypatch.setattr("pathlib.Path.unlink", refuse)
    with pytest.raises(RuntimeError, match="still indexed"):
        brain.delete_entity(entity.id)
    assert brain.get_entity(entity.id) is not None


def test_delete_is_recorded_in_the_audit_trail(brain):
    target = brain.backend.all_entities()[0].id
    brain.delete_entity(target, source="cli")
    assert brain.analytics_events(limit=1)[0]["operation"] == "delete_entity"


# -- external ids --------------------------------------------------------------


def test_an_entity_can_carry_the_id_its_source_system_knows_it_by(brain):
    brain.put_entity(Entity(id="issue:crm", doc_type="issue", name="n",
                            external_id="CRM-4471"))
    found = brain.get_by_external_id("CRM-4471")
    assert found is not None and found.id == "issue:crm"


def test_an_unknown_external_id_is_none(brain):
    assert brain.get_by_external_id("nope") is None
    assert brain.get_by_external_id("") is None


def test_the_external_id_survives_a_file_round_trip(brain):
    """It is written to disk, so a reindex must not drop it."""
    brain.put_entity(Entity(id="issue:crm", doc_type="issue", name="n",
                            external_id="CRM-1"))
    brain.index()
    reopened = Brain.open(brain.config.root)
    assert reopened.get_by_external_id("CRM-1") is not None


def test_the_canonical_id_keeps_its_shape(brain):
    """external_id is free-form precisely so `id` does not have to be: an agent
    reads the doc_type off the id."""
    with pytest.raises(ValueError, match="must look like"):
        Entity(id="not-a-valid-id", doc_type="issue")


def test_external_id_is_not_treated_as_a_schema_field(brain):
    brain.put_entity(Entity(id="issue:crm", doc_type="issue", name="n",
                            external_id="CRM-2"))
    assert "external_id" not in brain.get_entity("issue:crm").fields


def test_a_batch_lookup_skips_what_is_missing(brain):
    ids = [e.id for e in brain.backend.all_entities()[:2]] + ["issue:ghost"]
    found = brain.get_entities(ids)
    assert len(found) == 2
    assert all(e is not None for e in found)


# -- the HTTP API --------------------------------------------------------------


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    d = tmp_path / "support-brain"
    shutil.copytree(EXAMPLE, d)
    b = Brain.open(d)
    b.index()
    b.put_entity(Entity(id="issue:crm", doc_type="issue", name="from crm",
                        external_id="CRM-9", fields={"description": "x"}))
    monkeypatch.setenv("OPEN_INDEX_DIR", str(d))
    monkeypatch.delenv("OPEN_INDEX_BRAINS_ROOT", raising=False)
    monkeypatch.delenv("OPEN_INDEX_TOKEN", raising=False)

    from open_index.ui import web

    web.open_brain.cache_clear()
    web.discover.cache_clear()
    return TestClient(web.build_app())


def test_search_over_http(api):
    body = api.get("/api/v1/search", params={"q": "payment"}).json()
    assert body["total"] and body["results"][0]["match"]


def test_search_exposes_the_modes(api):
    body = api.get("/api/v1/search", params={"q": "payment", "mode": "keyword"}).json()
    assert all(r["match"]["type"] == "keyword" for r in body["results"])


def test_an_unknown_mode_is_a_400_not_a_500(api):
    """The caller can fix it, and the message says how."""
    r = api.get("/api/v1/search", params={"q": "x", "mode": "telepathy"})
    assert r.status_code == 400
    assert "unknown search mode" in r.json()["error"]


def test_filtering_on_an_undeclared_field_is_a_400(api):
    r = api.get("/api/v1/search", params={"filter.nope": "1"})
    assert r.status_code == 400
    assert "cannot filter on" in r.json()["error"]


def test_get_one_entity(api):
    body = api.get("/api/v1/entities/issue:crm").json()
    assert body["id"] == "issue:crm"
    assert "relationships" in body


def test_a_missing_entity_is_a_404(api):
    assert api.get("/api/v1/entities/issue:ghost").status_code == 404


def test_lookup_by_external_id_over_http(api):
    assert api.get("/api/v1/entities/by-external-id/CRM-9").json()["id"] == "issue:crm"


def test_batch_reports_what_was_missing(api):
    body = api.get("/api/v1/entities",
                   params={"id": ["issue:crm", "issue:ghost"]}).json()
    assert body["found"] == 1
    assert body["missing"] == ["issue:ghost"]


def test_the_schema_endpoint_describes_the_index(api):
    body = api.get("/api/v1/schema").json()
    assert body["doc_types"] and "count" in body["doc_types"][0]


def test_put_creates_an_entity(api):
    r = api.put("/api/v1/entities/issue:via-api",
                json={"doc_type": "issue", "name": "made over http"})
    assert r.status_code == 200
    assert api.get("/api/v1/entities/issue:via-api").status_code == 200


def test_put_rejects_a_body_id_that_contradicts_the_url(api):
    """Otherwise PUT /entities/a could write entity b."""
    r = api.put("/api/v1/entities/issue:a", json={"id": "issue:b",
                                                  "doc_type": "issue"})
    assert r.status_code == 400


def test_put_rejects_an_invalid_entity_with_422(api):
    r = api.put("/api/v1/entities/nope:x", json={"doc_type": "nope"})
    assert r.status_code in (400, 422)


def test_put_rejects_a_non_json_body(api):
    r = api.put("/api/v1/entities/issue:x", content=b"not json")
    assert r.status_code == 400


def test_delete_over_http(api):
    api.put("/api/v1/entities/issue:temp", json={"doc_type": "issue"})
    assert api.delete("/api/v1/entities/issue:temp").status_code == 200
    assert api.delete("/api/v1/entities/issue:temp").status_code == 404


def test_a_trace_is_readable_over_http(api):
    api.get("/api/v1/search", params={"q": "payment"},
            headers={"X-Trace-Id": "api-turn-1"})
    body = api.get("/api/v1/traces/api-turn-1").json()
    assert body["reads"] and body["reads"][0]["results"]


# -- auth ----------------------------------------------------------------------


@pytest.fixture
def gated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home2"))
    (tmp_path / "home2").mkdir()
    d = tmp_path / "gated-brain"
    shutil.copytree(EXAMPLE, d)
    Brain.open(d).index()
    monkeypatch.setenv("OPEN_INDEX_DIR", str(d))
    monkeypatch.delenv("OPEN_INDEX_BRAINS_ROOT", raising=False)
    monkeypatch.setenv("OPEN_INDEX_TOKEN", "s3cret")

    from open_index.ui import web

    web.open_brain.cache_clear()
    web.discover.cache_clear()
    return TestClient(web.build_app())


def test_reads_stay_open_when_a_token_is_configured(gated):
    """Matching how the MCP endpoint already behaves: the token gates writes."""
    assert gated.get("/api/v1/search", params={"q": "payment"}).status_code == 200


def test_writes_need_the_token(gated):
    r = gated.put("/api/v1/entities/issue:x", json={"doc_type": "issue"})
    assert r.status_code == 401
    assert "Bearer" in r.headers.get("www-authenticate", "")


def test_a_wrong_token_is_refused(gated):
    r = gated.put("/api/v1/entities/issue:x", json={"doc_type": "issue"},
                  headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_the_right_token_is_accepted(gated):
    r = gated.put("/api/v1/entities/issue:x", json={"doc_type": "issue"},
                  headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200


def test_delete_is_gated_too(gated):
    assert gated.delete("/api/v1/entities/issue:anything").status_code == 401


def test_writes_are_open_when_no_token_is_set(api):
    """The demo-host case: nothing configured, nothing gated."""
    assert api.put("/api/v1/entities/issue:open",
                   json={"doc_type": "issue"}).status_code == 200


def test_a_per_brain_token_overrides_the_shared_one(monkeypatch):
    from open_index.api import token_for

    monkeypatch.setenv("OPEN_INDEX_TOKEN", "shared")
    monkeypatch.setenv("OPEN_INDEX_TOKEN_ALPHA_INDEX", "specific")
    assert token_for("alpha-index") == "specific"
    assert token_for("beta") == "shared"
    monkeypatch.delenv("OPEN_INDEX_TOKEN")
    assert token_for("beta") is None


# -- multi-brain routing -------------------------------------------------------


@pytest.fixture
def many(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home3"))
    (tmp_path / "home3").mkdir()
    root = tmp_path / "brains"
    root.mkdir()
    for name in ("alpha", "beta"):
        shutil.copytree(EXAMPLE, root / name)
        Brain.open(root / name).index()
    monkeypatch.setenv("OPEN_INDEX_BRAINS_ROOT", str(root))
    monkeypatch.delenv("OPEN_INDEX_DIR", raising=False)
    monkeypatch.delenv("OPEN_INDEX_TOKEN", raising=False)

    from open_index.ui import web

    web.open_brain.cache_clear()
    web.discover.cache_clear()
    return TestClient(web.build_app())


def test_the_api_is_per_index(many):
    assert many.get("/alpha/api/v1/search", params={"q": "payment"}).status_code == 200
    assert many.get("/beta/api/v1/schema").status_code == 200


def test_the_api_on_an_unknown_index_is_a_404(many):
    assert many.get("/zzz/api/v1/search").status_code == 404


def test_the_api_route_is_not_mistaken_for_an_index(many):
    """/api/... must not resolve as an index literally named "api"."""
    assert many.get("/alpha").status_code == 200
    assert many.get("/alpha/api/v1/schema").json()["doc_types"]


def test_a_write_lands_in_the_right_index(many):
    many.put("/alpha/api/v1/entities/issue:only-alpha", json={"doc_type": "issue"})
    assert many.get("/alpha/api/v1/entities/issue:only-alpha").status_code == 200
    assert many.get("/beta/api/v1/entities/issue:only-alpha").status_code == 404


# -- the error paths a caller can actually hit ---------------------------------


def test_a_non_numeric_limit_is_a_400(api):
    r = api.get("/api/v1/search", params={"q": "x", "limit": "many"})
    assert r.status_code == 400
    assert "integer" in r.json()["error"]


def test_an_unknown_external_id_over_http_is_a_404(api):
    assert api.get("/api/v1/entities/by-external-id/NOPE-1").status_code == 404


def test_a_json_body_that_is_not_an_object_is_a_400(api):
    r = api.put("/api/v1/entities/issue:x", json=["not", "an", "object"])
    assert r.status_code == 400
    assert "object" in r.json()["error"]


def test_an_unconstructable_entity_is_a_400(api):
    """A field of the wrong shape fails at model construction, before validation
    against the doc_type — still the caller's mistake, still a 4xx."""
    r = api.put("/api/v1/entities/issue:bad",
                json={"doc_type": "issue", "related_to": [{"no_target": 1}]})
    assert r.status_code == 400
    assert "invalid entity" in r.json()["error"]


def test_a_delete_that_cannot_remove_the_file_is_a_500_not_a_silent_success(
        api, tmp_path, monkeypatch):
    """The entity is still indexed, so reporting success would be a lie."""
    api.put("/api/v1/entities/issue:stuck", json={"doc_type": "issue"})

    def refuse(self, missing_ok=False):
        raise OSError("read-only file system")

    monkeypatch.setattr("pathlib.Path.unlink", refuse)
    r = api.delete("/api/v1/entities/issue:stuck")
    assert r.status_code == 500
    assert "still indexed" in r.json()["error"]
