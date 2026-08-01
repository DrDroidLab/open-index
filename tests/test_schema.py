"""Tests for doc_type schemas (nested JSON). Run: python -m unittest discover -s tests"""

import os
import tempfile
import unittest

from droid_brain import store

SERVICE_SCHEMA = {
    "type": "object",
    "properties": {
        "team": {"type": "string"},
        "spec": {
            "type": "object",
            "properties": {
                "replicas": {"type": "integer"},
                "env": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}}}},
            },
        },
    },
    "required": ["team"],
}


class SchemaTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["DROID_BRAIN_HOME"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("DROID_BRAIN_HOME", None)

    def test_schema_stored_and_returned(self):
        with store.create_brain("acme") as brain:
            brain.create_doc_type("service", schema=SERVICE_SCHEMA)
            self.assertEqual(brain.get_doc_type("service")["schema"], SERVICE_SCHEMA)
            self.assertEqual(brain.list_doc_types()[0]["schema"], SERVICE_SCHEMA)
            self.assertIsNone(brain.get_doc_type("ghost"))

    def test_schema_must_be_object(self):
        with store.create_brain("acme") as brain:
            with self.assertRaises(ValueError):
                brain.create_doc_type("bad", schema=["not", "an", "object"])

    def test_schema_shape_validated_at_creation(self):
        with store.create_brain("acme") as brain:
            with self.assertRaisesRegex(ValueError, "required.*list"):
                brain.create_doc_type("bad1", schema={"required": "team"})  # string, not list
            with self.assertRaisesRegex(ValueError, "properties"):
                brain.create_doc_type("bad2", schema={"properties": ["team"]})
            with self.assertRaisesRegex(ValueError, "serializable"):
                brain.create_doc_type("bad3", schema={"f": object()})

    def test_corrupt_schema_row_degrades_to_none(self):
        with store.create_brain("acme") as brain:
            brain.create_doc_type("service", schema=SERVICE_SCHEMA)
            brain.conn.execute("UPDATE doc_types SET schema_json = '{not json' WHERE name = 'service'")
            brain.conn.commit()
            self.assertIsNone(brain.get_doc_type("service")["schema"])  # no crash
            self.assertIsNone(brain.list_doc_types()[0]["schema"])

    def test_required_fields_enforced(self):
        with store.create_brain("acme") as brain:
            brain.create_doc_type("service", schema=SERVICE_SCHEMA)
            with self.assertRaisesRegex(ValueError, "team"):
                brain.upsert_entity("service", "x", {"spec": {"replicas": 1}})
            brain.upsert_entity("service", "x", {"team": "ops", "spec": {"replicas": 1}})

    def test_nested_entities_searchable(self):
        with store.create_brain("acme") as brain:
            brain.create_doc_type("service", schema=SERVICE_SCHEMA)
            brain.upsert_entity(
                "service", "api", {"team": "ops", "spec": {"replicas": 3, "env": [{"name": "kafka-broker"}]}}
            )
            hits = brain.search("kafka-broker")  # nested array-of-object value
            self.assertEqual([h["name"] for h in hits], ["api"])

    def test_field_paths_and_template(self):
        self.assertEqual(
            store.schema_field_paths(SERVICE_SCHEMA),
            ["team", "spec", "spec.replicas", "spec.env", "spec.env[].name"],
        )
        self.assertEqual(
            store.entity_template(SERVICE_SCHEMA),
            {"team": "", "spec": {"replicas": 0, "env": []}},
        )
        self.assertEqual(store.entity_template(None), {})

    def test_structure_text_mentions_schema_fields(self):
        with store.create_brain("acme") as brain:
            brain.create_doc_type("service", schema=SERVICE_SCHEMA)
            text = brain.structure_text()
            self.assertIn("Schema fields:", text)
            self.assertIn("spec.replicas", text)

    def test_existing_brains_migrate(self):
        with store.create_brain("acme") as brain:
            # simulate a brain file from before schemas existed
            brain.conn.execute("ALTER TABLE doc_types DROP COLUMN schema_json")
            brain.conn.commit()
        with store.open_brain("acme") as brain:  # re-open triggers _migrate
            brain.create_doc_type("service", schema=SERVICE_SCHEMA)  # would fail if column missing
            self.assertEqual(brain.get_doc_type("service")["schema"], SERVICE_SCHEMA)


if __name__ == "__main__":
    unittest.main()
