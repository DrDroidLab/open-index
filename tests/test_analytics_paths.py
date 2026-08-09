"""Analytics must never be the reason a query fails.

It is observational: a read-only state directory, a broken recorder, or a
backend error must all still let context through. These cover the degraded
paths that the happy-path analytics tests don't reach.
"""

import pytest

from open_index.analytics import NullAnalyticsStore
from open_index.brain import Brain


# -- the null store (used when the state dir isn't writable) ------------------


def test_null_store_reports_unavailable():
    summary = NullAnalyticsStore().summary()
    assert summary["available"] is False


def test_null_store_records_and_reads_nothing():
    store = NullAnalyticsStore()
    store.record(source="ui", operation="search", duration_ms=1.0)
    assert store.recent() == []
    assert store.recent(limit=10) == []


def test_brain_falls_back_to_the_null_store(brain, monkeypatch):
    """An unwritable state directory must not stop the brain from opening."""
    import open_index.brain as brain_module

    def unwritable(_root):
        raise OSError("read-only file system")

    monkeypatch.setattr(brain_module, "AnalyticsStore", unwritable)
    degraded = Brain(brain.config, backend=brain.backend)

    assert isinstance(degraded.analytics, NullAnalyticsStore)
    assert degraded.search(query="payment").total >= 0
    assert degraded.analytics_summary()["available"] is False
    assert degraded.analytics_events() == []


# -- recording is best-effort -------------------------------------------------


def test_a_broken_recorder_does_not_break_queries(brain, monkeypatch):
    def boom(**_kwargs):
        raise RuntimeError("analytics exploded")

    monkeypatch.setattr(brain.analytics, "record", boom)
    assert brain.search(query="payment", source="ui").total >= 0
    assert brain.get_entity("product:checkout", source="ui") is not None
    assert brain.navigation_guidelines(source="ui")


# -- failures are recorded, then re-raised ------------------------------------


def test_a_failing_search_is_recorded_and_still_raises(brain, monkeypatch):
    monkeypatch.setattr(brain.backend, "search",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    with pytest.raises(RuntimeError):
        brain.search(query="x", source="cli")

    summary = brain.analytics_summary()
    assert summary["failed_fetches"] >= 1


def test_a_failing_get_entity_is_recorded_and_still_raises(brain, monkeypatch):
    monkeypatch.setattr(brain.backend, "get_entity",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    with pytest.raises(RuntimeError):
        brain.get_entity("product:checkout", source="cli")

    assert brain.analytics_summary()["failed_fetches"] >= 1


def test_failures_without_a_source_are_not_recorded(brain, monkeypatch):
    """No source means no attribution — recording it would be a nameless row."""
    before = brain.analytics_summary()["failed_fetches"]
    monkeypatch.setattr(brain.backend, "search",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    with pytest.raises(RuntimeError):
        brain.search(query="x")
    assert brain.analytics_summary()["failed_fetches"] == before


# -- filtered searches still record the post-filter totals --------------------


def test_filtered_search_records_the_returned_count(brain):
    """The dashboard must agree with what the caller actually got back."""
    brain.search(query=None, doc_types=["issue"], min_confidence=0.9, source="cli")
    events = brain.analytics_events(limit=1)
    assert events
    # Everything in the example brain is unattributed, so a 0.9 floor drops all.
    assert events[0]["result_count"] == 0


def test_zero_result_searches_are_counted(brain):
    brain.search(query="nothingmatchesthisquery", source="cli")
    assert brain.analytics_summary()["zero_result_searches"] >= 1


# -- MCP server instructions --------------------------------------------------


def test_server_without_instructions_support_still_builds(brain, monkeypatch):
    """Older SDKs take only a name; the guide is then fetched via the tool."""
    import open_index.mcp_server as mcp_server

    class Bare:
        def __init__(self, name):
            self.name = name

        def tool(self):
            return lambda fn: fn

    monkeypatch.setattr(mcp_server, "_load_server_class", lambda: Bare)
    server = mcp_server.build_server(brain)
    assert server.name.startswith("open-index:")
