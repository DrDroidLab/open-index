"""Core tests for droid_brain.store / seed. Run: python -m unittest discover -s tests"""

import os
import tempfile
import unittest

from droid_brain import store
from droid_brain.seed import seed_demo


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["DROID_BRAIN_HOME"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("DROID_BRAIN_HOME", None)

    def _seeded(self):
        brain = store.create_brain("acme")
        seed_demo(brain)
        return brain

    def test_create_and_reopen_brain(self):
        store.create_brain("acme", description="demo").close()
        self.assertEqual(store.most_recent_brain(), "acme")
        with store.open_brain("acme") as brain:
            self.assertEqual(brain.get_meta("description"), "demo")

    def test_duplicate_brain_rejected(self):
        store.create_brain("acme").close()
        with self.assertRaises(ValueError):
            store.create_brain("acme")

    def test_invalid_and_traversal_names_rejected(self):
        for bad in ["bad name", "../escape", "", "a/b"]:
            with self.assertRaises(ValueError, msg=bad):
                store.create_brain(bad)
            with self.assertRaises(ValueError, msg=bad):
                store.open_brain(bad)

    def test_doc_type_rules(self):
        with store.create_brain("acme") as brain:
            brain.create_doc_type("service", boost=2.0)
            with self.assertRaises(ValueError):
                brain.create_doc_type("service")  # duplicate
            with self.assertRaises(ValueError):
                brain.create_doc_type("svc", boost=0)  # boost must be > 0
            self.assertEqual([dt["name"] for dt in brain.list_doc_types()], ["service"])

    def test_upsert_get_update_delete_entity(self):
        with self._seeded() as brain:
            eid = brain.upsert_entity("service", "  orders-api  ", {"team": "ops"})
            entity = brain.get_entity("service", "orders-api")  # name stripped
            self.assertEqual(entity["id"], eid)
            self.assertEqual(entity["data"], {"team": "ops"})

            eid2 = brain.upsert_entity("service", "orders-api", {"team": "core"})
            self.assertEqual(eid2, eid)  # same entity updated, not duplicated
            self.assertEqual(brain.get_entity("service", "orders-api")["data"], {"team": "core"})

            hits = brain.search("orders")
            self.assertEqual(len([h for h in hits if h["name"] == "orders-api"]), 1)
            brain.delete_entity(eid)
            self.assertIsNone(brain.get_entity("service", "orders-api"))
            self.assertEqual([h for h in brain.search("orders") if h["name"] == "orders-api"], [])

    def test_entity_validation(self):
        with self._seeded() as brain:
            with self.assertRaises(ValueError):
                brain.upsert_entity("ghost-type", "x", {})
            with self.assertRaises(ValueError):
                brain.upsert_entity("service", "   ", {})
            with self.assertRaises(ValueError):
                brain.upsert_entity("service", "x", ["not", "a", "dict"])
            with self.assertRaises(ValueError):
                brain.upsert_entity("service", "x", {"bad": object()})

    def test_type_booster_ranks_boosted_type_first(self):
        with self._seeded() as brain:  # service boost=2.0, dashboard boost=1.0
            names = [r["name"] for r in brain.search("payments")]
            self.assertLess(names.index("payments-service"), names.index("payments-slo"))

    def test_search_filter_and_fallbacks(self):
        with self._seeded() as brain:
            only = brain.search("payments", doc_type="dashboard")
            self.assertEqual({r["doc_type"] for r in only}, {"dashboard"})
            self.assertEqual(brain.search("zzz-no-such-token"), [])
            recent = brain.search("")  # empty query -> most recently updated
            self.assertEqual(len(recent), len(brain.list_entities(limit=50)))

    def test_structure_text(self):
        with self._seeded() as brain:
            text = brain.structure_text()
            self.assertIn("Brain: acme", text)
            self.assertIn("service", text)
            self.assertIn("api-gateway", text)

    def test_seed_demo_is_idempotent(self):
        with self._seeded() as brain:
            first = sum(dt["entities"] for dt in brain.list_doc_types())
            seed_demo(brain)  # re-seeding skips existing doc_types, upserts entities
            second = sum(dt["entities"] for dt in brain.list_doc_types())
            self.assertEqual(first, second)

    def test_concurrent_writers_same_brain(self):
        with self._seeded() as brain:
            other = store.open_brain("acme")  # second connection, WAL mode
            try:
                brain.upsert_entity("service", "svc-a", {"n": 1})
                other.upsert_entity("service", "svc-b", {"n": 2})
                names = {e["name"] for e in brain.list_entities(doc_type="service")}
                self.assertTrue({"svc-a", "svc-b"} <= names)
            finally:
                other.close()


if __name__ == "__main__":
    unittest.main()
