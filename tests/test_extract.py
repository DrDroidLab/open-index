"""Tests for MCP extraction. Run: python -m unittest discover -s tests"""

import os
import tempfile
import unittest

from droid_brain import store
from droid_brain.extract import DEMO_SOURCES, ItemSkipped, _dig, _parse_items, extract, transform_item


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


if __name__ == "__main__":
    unittest.main()
