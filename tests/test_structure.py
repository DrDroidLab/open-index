def test_structure_reports_doc_types_and_counts(brain):
    s = brain.structure()
    assert s["name"] == "support-brain"
    assert s["total_entities"] == 9
    by_type = {d["doc_type"]: d for d in s["doc_types"]}
    assert set(by_type) == {"product", "issue", "user_segment", "comment"}
    assert by_type["issue"]["count"] == 4
    assert by_type["product"]["examples"]  # some example ids present
    # schema surfaced with boosts
    name_field = next(f for f in by_type["product"]["fields"] if f["name"] == "name")
    assert name_field["boost"] == 6
