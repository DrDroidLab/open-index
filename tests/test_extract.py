"""Tests for MCP extraction. Run: python -m unittest discover -s tests"""

import copy
import os
import tempfile
import unittest
from types import SimpleNamespace

from droid_brain import store
from droid_brain.extract import (
    DEMO_SOURCES,
    ItemSkipped,
    _dig,
    _parse_items,
    _result_text,
    _validate_source,
    extract,
    transform_item,
)


class TransformTestCase(unittest.TestCase):
    def test_dig_nested(self):
        item = {"endpoint": {"host": "db.example.com", "port": 5432}}
        self.assertEqual(_dig(item, "endpoint.host"), "db.example.com")
        self.assertIsNone(_dig(item, "endpoint.missing"))  # optional fields -> None
        self.assertEqual(_dig(item, "endpoint.missing", default="x"), "x")

    def test_transform_with_field_mapping_and_constants(self):
        spec = {"name_field": "title", "fields": {"url": "meta.link"}, "constants": {"source": "grafana"}}
        item = {"title": "dash-1", "meta": {"link": "http://x"}, "extra": "dropped"}
        name, data = transform_item(item, spec)
        self.assertEqual(name, "dash-1")
        self.assertEqual(data, {"url": "http://x", "source": "grafana"})

    def test_transform_missing_mapped_field_becomes_none(self):
        spec = {"name_field": "title", "fields": {"url": "meta.link"}}
        name, data = transform_item({"title": "dash-1"}, spec)
        self.assertEqual((name, data), ("dash-1", {"url": None}))

    def test_transform_without_mapping_keeps_whole_item(self):
        spec = {"name_field": "id"}
        item = {"id": "a", "nested": {"deep": [1, 2]}}
        name, data = transform_item(item, spec)
        self.assertEqual((name, data), ("a", {"id": "a", "nested": {"deep": [1, 2]}}))

    def test_transform_skips_bad_name(self):
        with self.assertRaises(ItemSkipped):
            transform_item({"title": "  "}, {"name_field": "title"})
        with self.assertRaises(ItemSkipped):
            transform_item({"other": "x"}, {"name_field": "title"})

    def test_parse_items(self):
        self.assertEqual(_parse_items('[{"a": 1}]', {}), [{"a": 1}])
        self.assertEqual(
            _parse_items('{"result": {"rows": [{"a": 1}]}}', {"items_path": "result.rows"}),
            [{"a": 1}],
        )
        with self.assertRaises(ValueError):
            _parse_items('{"rows": []}', {})  # object without items_path
        with self.assertRaisesRegex(ValueError, "not valid JSON"):
            _parse_items('{oops', {})
        with self.assertRaisesRegex(ValueError, "must be JSON objects"):
            _parse_items('["just", "strings"]', {})

    def test_result_text(self):
        result = SimpleNamespace(content=[SimpleNamespace(text='[{"a": 1}]')])
        self.assertEqual(_result_text(result), '[{"a": 1}]')
        with self.assertRaisesRegex(ValueError, "no text content"):
            _result_text(SimpleNamespace(content=[]))
        with self.assertRaisesRegex(ValueError, "no text content"):
            _result_text(SimpleNamespace(content=[SimpleNamespace(not_text=1)]))

    def test_validate_source(self):
        good = {"name": "x", "command": ["python3", "-m", "mod"], "tools": [{"tool": "t", "doc_type": "d", "name_field": "n"}]}
        _validate_source(good, 0)  # no error
        for bad in [
            {"name": "x", "command": [], "tools": good["tools"]},
            {"name": "x", "command": "python3 -m mod", "tools": good["tools"]},  # string not list
            {"name": "x", "command": good["command"], "tools": []},
            {"name": "x", "command": good["command"], "tools": [{"tool": "t"}]},  # missing keys
            {"name": "x", "command": good["command"], "tools": [{"tool": "t", "doc_type": "d", "name_field": "n", "fields": ["not-a-dict"]}]},
        ]:
            with self.assertRaises(ValueError, msg=str(bad)):
                _validate_source(bad, 0)

    def test_items_without_name_are_skipped_not_fatal(self):
        spec = {"doc_type": "thing", "name_field": "title"}
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["DROID_BRAIN_HOME"] = tmp
            with store.create_brain("b") as brain:
                brain.create_doc_type("thing")
                ok = 0
                skipped = 0
                for item in [{"title": "a"}, {"no_title": True}, {"title": "b"}]:
                    try:
                        name, data = transform_item(item, spec)
                        brain.upsert_entity("thing", name, data)
                        ok += 1
                    except ItemSkipped:
                        skipped += 1
                self.assertEqual((ok, skipped), (2, 1))
                self.assertEqual(len(brain.list_entities(doc_type="thing")), 2)


class ExtractIntegrationTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["DROID_BRAIN_HOME"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("DROID_BRAIN_HOME", None)

    def test_demo_extraction_end_to_end(self):
        with store.create_brain("assets") as brain:
            summary = extract(brain, DEMO_SOURCES)
            self.assertEqual(summary["sources"], 3)
            self.assertEqual(summary["entities"], 7)
            self.assertEqual(summary["by_doc_type"], {"dashboard": 2, "database": 2, "repository": 3})

            # doc_types auto-created, nested data preserved and searchable
            entity = brain.get_entity("database", "redis-sessions")
            self.assertEqual(entity["data"]["endpoint"]["port"], 6379)
            self.assertEqual(entity["data"]["source"], "aws")  # constants applied
            repo = brain.get_entity("repository", "user-service")
            self.assertEqual(repo["data"]["ci"]["status"], "failing")  # fields mapping kept nesting
            self.assertEqual([h["name"] for h in brain.search("kafka")], [])  # sanity
            self.assertEqual([h["name"] for h in brain.search("replication lag")], ["postgres-payments-health"])

            # re-running is idempotent (upserts, no duplicates)
            summary2 = extract(brain, DEMO_SOURCES)
            self.assertEqual(summary2["entities"], 7)
            total = sum(dt["entities"] for dt in brain.list_doc_types())
            self.assertEqual(total, 7)

    def test_failing_source_does_not_lose_others(self):
        good = copy.deepcopy(DEMO_SOURCES[2])  # aws
        bad = copy.deepcopy(DEMO_SOURCES[0])  # grafana with a bogus tool
        bad["tools"][0]["tool"] = "list_nope"
        with store.create_brain("assets") as brain:
            summary = extract(brain, [bad, good])
            self.assertEqual(summary["sources"], 1)  # only aws succeeded
            self.assertEqual(summary["entities"], 2)
            self.assertEqual(len(summary["errors"]), 1)
            self.assertIn("list_nope", summary["errors"][0])
            self.assertEqual(brain.get_entity("database", "redis-sessions")["name"], "redis-sessions")

    def test_nameless_items_skipped_and_counted(self):
        source = copy.deepcopy(DEMO_SOURCES[0])  # grafana dashboards have 'title', not 'id'
        source["tools"][0]["name_field"] = "id"
        with store.create_brain("assets") as brain:
            summary = extract(brain, [source])
            self.assertEqual(summary["entities"], 0)
            self.assertEqual(summary["skipped"], 2)
            self.assertEqual(summary["errors"], [])

    def test_config_errors_fail_fast(self):
        with store.create_brain("assets") as brain:
            with self.assertRaisesRegex(ValueError, "'command'"):
                extract(brain, [{"name": "bad", "command": []}])


if __name__ == "__main__":
    unittest.main()
