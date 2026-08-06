def test_index_loads_all_entities(brain):
    counts = brain.counts()
    assert counts["product"] == 2
    assert counts["issue"] == 4
    assert counts["user_segment"] == 2
    assert counts["comment"] == 1


def test_round_trip(brain):
    e = brain.get_entity("product:checkout")
    assert e is not None
    assert e.name == "Checkout"
    assert e.fields["owner"] == "payments-team"


def test_search_finds_by_field_text(brain):
    results = brain.search("payment")
    ids = [r["id"] for r in results.results]
    assert "issue:payment-declined" in ids
    assert results.total >= 1


def test_search_doc_type_filter(brain):
    results = brain.search("checkout", doc_types=["product"])
    assert all(r["doc_type"] == "product" for r in results.results)


def test_counts_only_returns_no_documents(brain):
    results = brain.search("payment", counts_only=True)
    assert results.results == []
    assert results.total == sum(results.doc_type_counts.values())
    assert results.doc_type_counts  # non-empty aggregation


def test_boost_ranks_name_match_first(brain):
    # "Search" is a product name (boost 6) and also appears in issue bodies.
    results = brain.search("search")
    assert results.results[0]["id"] == "product:search"


def test_empty_query_lists_everything(brain):
    results = brain.search(None, limit=100)
    assert results.total == 9


def test_per_field_weight_is_exact(brain):
    """A term hit in a boost:6 field scores 6× the same hit in a boost:1 field."""
    from droid_brain.models import Entity

    # 'zephyr' appears once in the name (boost 6) of one issue...
    brain.put_entity(Entity.from_dict({
        "doc_type": "issue", "id": "issue:zephyr-title",
        "name": "zephyr", "description": "unrelated", "status": "open"}))
    # ...and once in the description (boost 1) of another.
    brain.put_entity(Entity.from_dict({
        "doc_type": "issue", "id": "issue:zephyr-body",
        "name": "other", "description": "a zephyr appeared", "status": "open"}))

    # This test checks the exact keyword-only scoring; disable the embedding
    # provider so semantic weighting does not alter the raw per-field scores.
    brain.backend._embedding_provider = None
    brain.backend._embedding_provider_initialized = True
    results = brain.search("zephyr")
    by_id = {r["id"]: r["score"] for r in results.results}
    assert results.results[0]["id"] == "issue:zephyr-title"      # title hit ranks first
    assert by_id["issue:zephyr-title"] == 6 * by_id["issue:zephyr-body"]  # exactly 6×
