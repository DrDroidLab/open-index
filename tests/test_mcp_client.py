from open_index.connectors.mcp_client import extract_json


def test_extract_json_spreads_list():
    content = [{"type": "text", "text": '[{"a": 1}, {"a": 2}]'}]
    assert extract_json(content) == [{"a": 1}, {"a": 2}]


def test_extract_json_appends_dict():
    content = [{"type": "text", "text": '{"a": 1}'}]
    assert extract_json(content) == [{"a": 1}]


def test_extract_json_keeps_plain_text():
    content = [{"type": "text", "text": "not json"}]
    assert extract_json(content) == [{"text": "not json"}]


def test_extract_json_ignores_non_text_items():
    content = [{"type": "image", "data": "..."}, {"type": "text", "text": '{"a": 1}'}]
    assert extract_json(content) == [{"a": 1}]
