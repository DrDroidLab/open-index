def test_search_and_entity_fetch_are_recorded(brain):
    brain.search("payment", doc_types=["issue"], source="cli")
    brain.get_entity("product:checkout", source="mcp")

    summary = brain.analytics_summary()
    assert summary["total_fetches"] == 2
    assert summary["by_source"] == {"cli": 1, "mcp": 1}
    assert summary["by_operation"] == {"get_entity": 1, "search": 1}
    assert summary["by_context"]["payment"] == 1

    events = brain.analytics_events()
    search = next(event for event in events if event["operation"] == "search")
    assert search["query"] == "payment"
    assert search["doc_types"] == '["issue"]'
    assert search["result_count"] >= 1


def test_navigation_and_zero_result_search_are_recorded(brain):
    brain.navigation_guidelines(source="mcp")
    brain.search("anything", doc_types=["missing-type"], source="cli")

    summary = brain.analytics_summary()
    assert summary["by_operation"]["navigation_guidelines"] == 1
    assert summary["zero_result_searches"] == 1


def test_analytics_are_isolated_per_brain(brain, tmp_path):
    from open_index.brain import Brain
    from open_index.scaffold import init_brain

    brain.search("payment", source="cli")
    other_dir = tmp_path / "other"
    init_brain(other_dir, "other")
    other = Brain.open(other_dir)

    assert other.analytics_summary()["total_fetches"] == 0


def test_brain_falls_back_when_analytics_state_cannot_open(brain, monkeypatch):
    from open_index.analytics import AnalyticsStore
    from open_index.brain import Brain

    def unavailable(*args, **kwargs):
        raise PermissionError("read-only state home")

    monkeypatch.setattr(AnalyticsStore, "__init__", unavailable)
    reopened = Brain(brain.config, backend=brain.backend)
    assert reopened.analytics_summary()["available"] is False
    assert reopened.search("payment").total >= 1
