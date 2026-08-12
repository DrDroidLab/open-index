"""The retrieval audit trail: what was returned, why, and under whose trace.

The question this exists to answer is not "what does the index contain" but
"what did it actually hand the agent, ranked how, and on what basis" — which
needs a row per returned document, not per read.
"""

import shutil

import pytest
from starlette.testclient import TestClient

from open_index.brain import Brain
from open_index.tracing import current_trace_id, normalize, trace

EXAMPLE = "examples/support-brain"


@pytest.fixture
def brain(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))   # isolate the state db
    (tmp_path / "home").mkdir()
    d = tmp_path / "b"
    shutil.copytree(EXAMPLE, d)
    b = Brain.open(d)
    b.index()
    return b


# -- the trace id itself -------------------------------------------------------


def test_a_trace_id_is_bound_for_the_block_and_restored_after():
    """A leaked id on a reused worker would credit the next caller's retrievals
    to the previous one."""
    assert current_trace_id() is None
    with trace("turn-1"):
        assert current_trace_id() == "turn-1"
        with trace("turn-2"):
            assert current_trace_id() == "turn-2"
        assert current_trace_id() == "turn-1"
    assert current_trace_id() is None


@pytest.mark.parametrize("value", ["turn-1", "a.b:c-d", "A1", "x" * 128])
def test_reasonable_ids_are_accepted(value):
    assert normalize(value) == value


@pytest.mark.parametrize("value", ["", None, "   ", "x" * 129, "bad id", "<script>",
                                   "a\nb"])
def test_unusable_ids_are_dropped_rather_than_mangled(value):
    """A sanitised id would fail to correlate with whatever the caller thinks it
    sent, which is worse than having none."""
    assert normalize(value) is None


def test_nothing_invents_a_trace_id(brain):
    brain.search(query="payment", source="cli")
    assert all(e["trace_id"] is None for e in brain.analytics_events())


# -- per-result rows -----------------------------------------------------------


def test_a_search_records_a_row_per_returned_document(brain):
    res = brain.search(query="payment", source="cli")
    with trace("t1"):
        pass
    events = brain.analytics_events(limit=1)
    stored = brain.analytics.results_for([events[0]["id"]])[events[0]["id"]]
    assert len(stored) == len(res.results)
    assert [r["entity_id"] for r in stored] == [r["id"] for r in res.results]


def test_each_recorded_row_carries_the_score_and_why_it_matched(brain):
    brain.search(query="payment", source="cli")
    event = brain.analytics_events(limit=1)[0]
    rows = brain.analytics.results_for([event["id"]])[event["id"]]
    assert rows[0]["match_type"] in ("keyword", "semantic", "both")
    assert rows[0]["score"] is not None
    assert rows[0]["rank"] == 1


def test_rank_is_the_order_the_caller_received(brain):
    brain.search(query="payment", source="cli")
    event = brain.analytics_events(limit=1)[0]
    rows = brain.analytics.results_for([event["id"]])[event["id"]]
    assert [r["rank"] for r in rows] == list(range(1, len(rows) + 1))


def test_a_lookup_is_recorded_too(brain):
    """A trace showing what was searched but not what was then opened is half a
    trail."""
    eid = brain.backend.all_entities()[0].id
    with trace("t-lookup"):
        brain.get_entity(eid, source="cli")
    events = brain.analytics_by_trace("t-lookup")
    assert events[0]["results"][0]["entity_id"] == eid
    assert events[0]["results"][0]["match_type"] == "lookup"


def test_a_failed_read_records_no_documents(brain, monkeypatch):
    monkeypatch.setattr(type(brain.backend), "search",
                        lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    with pytest.raises(RuntimeError):
        brain.search(query="payment", source="cli")
    event = brain.analytics_events(limit=1)[0]
    assert event["success"] == 0
    assert brain.analytics.results_for([event["id"]]) == {}


def test_only_the_rows_that_survived_filtering_are_recorded(brain):
    """A document dropped by a confidence filter must not look like it was
    handed over."""
    brain.search(query="payment", source="cli", min_confidence=0.99)
    event = brain.analytics_events(limit=1)[0]
    rows = brain.analytics.results_for([event["id"]]).get(event["id"], [])
    assert len(rows) == event["result_count"]


# -- lookup by trace -----------------------------------------------------------


def test_a_trace_gathers_every_read_of_that_turn(brain):
    with trace("turn-abc"):
        brain.search(query="payment", source="mcp")
        brain.search(query="checkout", source="mcp")
    events = brain.analytics_by_trace("turn-abc")
    assert len(events) == 2
    assert [e["query"] for e in events] == ["payment", "checkout"]
    assert all(e["results"] for e in events)


def test_an_unknown_trace_is_empty_not_an_error(brain):
    assert brain.analytics_by_trace("never-used") == []
    assert brain.analytics_by_trace("") == []


def test_reads_outside_a_trace_are_not_swept_into_one(brain):
    brain.search(query="payment", source="cli")
    with trace("turn-x"):
        brain.search(query="checkout", source="cli")
    assert len(brain.analytics_by_trace("turn-x")) == 1


def test_which_queries_retrieved_a_document(brain):
    """The reverse question: not what did this query return, but what keeps
    returning this."""
    with trace("turn-y"):
        brain.search(query="payment", source="cli")
    target = brain.analytics_events(limit=1)[0]
    rows = brain.analytics.results_for([target["id"]])[target["id"]]
    eid = rows[0]["entity_id"]

    history = brain.retrievals_of(eid)
    assert history
    assert history[0]["query"] == "payment"
    assert history[0]["trace_id"] == "turn-y"


# -- retention -----------------------------------------------------------------


def test_old_reads_are_pruned_with_their_documents(tmp_path, monkeypatch):
    """A trace should be wholly present or wholly gone — never a read whose
    documents were collected out from under it."""
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    (tmp_path / "h").mkdir()
    monkeypatch.setenv("OPEN_INDEX_ANALYTICS_MAX", "5")
    d = tmp_path / "b"
    shutil.copytree(EXAMPLE, d)
    b = Brain.open(d)
    b.index()

    for i in range(12):
        with trace(f"turn-{i}"):
            b.search(query="payment", source="cli")

    assert len(b.analytics_events(limit=100)) <= 5
    assert b.analytics_by_trace("turn-0") == []
    survivor = b.analytics_by_trace("turn-11")
    assert survivor and survivor[0]["results"]


# -- the HTTP edge -------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    d = tmp_path / "support-brain"
    shutil.copytree(EXAMPLE, d)
    Brain.open(d).index()
    monkeypatch.setenv("OPEN_INDEX_DIR", str(d))
    monkeypatch.delenv("OPEN_INDEX_BRAINS_ROOT", raising=False)

    from open_index.ui import web

    web.open_brain.cache_clear()
    web.discover.cache_clear()
    return TestClient(web.build_app())


def test_the_header_binds_the_trace_for_a_page(client, tmp_path):
    client.get("/explore", params={"q": "payment"},
               headers={"X-Trace-Id": "web-turn-1"})
    brain = Brain.open(tmp_path / "support-brain")
    events = brain.analytics_by_trace("web-turn-1")
    assert events and events[0]["results"]


def test_the_trace_is_echoed_back_so_a_caller_can_confirm_it_took(client):
    r = client.get("/explore", params={"q": "payment"},
                   headers={"X-Trace-Id": "web-turn-2"})
    assert r.headers.get("x-trace-id") == "web-turn-2"


def test_a_malformed_header_is_dropped_not_stored(client):
    r = client.get("/explore", params={"q": "payment"},
                   headers={"X-Trace-Id": "not a valid id!"})
    assert "x-trace-id" not in r.headers


def test_the_trace_does_not_leak_between_requests(client, tmp_path):
    client.get("/explore", params={"q": "payment"},
               headers={"X-Trace-Id": "web-turn-3"})
    client.get("/explore", params={"q": "checkout"})
    brain = Brain.open(tmp_path / "support-brain")
    assert len(brain.analytics_by_trace("web-turn-3")) == 1


def test_the_analytics_page_looks_up_a_trace(client):
    client.get("/explore", params={"q": "payment"},
               headers={"X-Trace-Id": "web-turn-4"})
    body = client.get("/analytics", params={"trace": "web-turn-4"}).text
    assert "issue:payment-declined" in body


def test_an_unknown_trace_says_so_on_the_page(client):
    body = client.get("/analytics", params={"trace": "nope"}).text
    assert "Nothing recorded" in body


def test_the_entity_page_shows_what_retrieved_it(client):
    client.get("/explore", params={"q": "payment"},
               headers={"X-Trace-Id": "web-turn-5"})
    body = client.get("/entity/issue:payment-declined").text
    assert "What retrieved this" in body
    assert "web-turn-5" in body


# -- the no-op fallback --------------------------------------------------------


def test_the_null_store_answers_every_question_the_real_one_does():
    """It stands in when the state directory is unwritable, so a missing method
    would raise on exactly the machine where analytics were already degraded —
    a failure in the path whose whole job is not to fail.
    """
    from open_index.analytics import AnalyticsStore, NullAnalyticsStore

    public = {n for n in dir(AnalyticsStore) if not n.startswith("_")}
    assert public <= set(dir(NullAnalyticsStore))

    null = NullAnalyticsStore()
    assert null.record(source="x", operation="y", duration_ms=1.0) is None
    assert null.by_trace("t") == []
    assert null.retrievals_of("issue:x") == []
    assert null.results_for([1]) == {}
    assert null.summary()["available"] is False


def test_a_brain_with_unwritable_state_still_reads(tmp_path, monkeypatch):
    from open_index.analytics import NullAnalyticsStore

    d = tmp_path / "b"
    shutil.copytree(EXAMPLE, d)
    b = Brain.open(d)
    b.index()
    b.analytics = NullAnalyticsStore()
    assert b.search(query="payment", source="cli").results
    assert b.analytics_by_trace("anything") == []


# -- defensive paths -----------------------------------------------------------


def test_a_bad_retention_setting_falls_back_rather_than_crashing(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    (tmp_path / "h").mkdir()
    monkeypatch.setenv("OPEN_INDEX_ANALYTICS_MAX", "not-a-number")
    from open_index.analytics import AnalyticsStore

    store = AnalyticsStore(tmp_path / "b")
    assert store._max_fetches > 0


def test_retention_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "h2"))
    (tmp_path / "h2").mkdir()
    monkeypatch.setenv("OPEN_INDEX_ANALYTICS_MAX", "0")
    from open_index.analytics import AnalyticsStore

    store = AnalyticsStore(tmp_path / "b2")
    for i in range(5):
        store.record(source="cli", operation="search", duration_ms=1.0, query=f"q{i}")
    assert len(store.recent(limit=50)) == 5


def test_an_unscorable_value_is_recorded_as_absent_not_as_a_crash():
    from open_index.analytics import _as_float

    assert _as_float(None) is None
    assert _as_float("not a number") is None
    assert _as_float("1.5") == 1.5


def test_set_trace_id_binds_without_a_block():
    from open_index.tracing import set_trace_id

    with trace(None):
        set_trace_id("direct-1")
        assert current_trace_id() == "direct-1"
        set_trace_id("bad id!")
        assert current_trace_id() is None


def test_unreadable_headers_do_not_break_a_read():
    """Headers come from outside; a hostile or odd object must not fail the
    request it is attached to."""
    from open_index.tracing import trace_from_headers

    class Hostile:
        def get(self, _name):
            raise RuntimeError("nope")

    assert trace_from_headers(Hostile()) is None
