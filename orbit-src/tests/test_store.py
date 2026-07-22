import os
import tempfile
import unittest
from unittest.mock import patch

from orbit.store import Store
from orbit.worker import Coordinator


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.temp.name, "orbit.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_secrets_are_masked_but_preserved(self):
        self.store.set_settings({"tmdb_api_key": "secret", "list_poll_minutes": "60"}, {"tmdb_api_key"})
        self.assertEqual(self.store.get_settings()["tmdb_api_key"], "••••••••")
        self.store.set_settings({"tmdb_api_key": "••••••••"}, {"tmdb_api_key"})
        self.assertEqual(self.store.get_settings(True)["tmdb_api_key"], "secret")

    def test_overlapping_sources_do_not_duplicate_media(self):
        item = {"media_type": "movie", "title": "Dune", "tmdb_id": 438631, "year": 2021}
        first, created = self.store.add_request(item, source="manual")
        second, duplicated = self.store.add_request(item, source="mdblist", source_ref="1")
        self.assertTrue(created)
        self.assertFalse(duplicated)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.store.list_requests()), 1)

    def test_transition_records_visible_timeline(self):
        item, _ = self.store.add_request({"media_type": "show", "title": "Foundation", "tmdb_id": 93740})
        self.store.transition(item["id"], "searching", "Finding a cached release")
        self.store.transition(item["id"], "library_pending", "Waiting for mount")
        events = self.store.events(item["id"])
        self.assertEqual([event["state"] for event in events], ["queued", "searching", "library_pending"])

    def test_list_source_defaults_to_safe_import_only(self):
        source = self.store.add_list_source({
            "name": "New releases", "kind": "mdblist",
            "url": "https://mdblist.com/lists/example/new-releases",
        })
        self.assertEqual(source["mode"], "import_only")
        self.assertEqual(source["enabled"], 1)

    def test_library_link_promotes_request_to_ready(self):
        movies = os.path.join(self.temp.name, "Movies")
        television = os.path.join(self.temp.name, "TV")
        os.makedirs(os.path.join(movies, "Dune (2021) {tmdb-438631}"))
        os.makedirs(television)
        item, _ = self.store.add_request({
            "media_type": "movie", "title": "Dune", "tmdb_id": 438631, "year": 2021,
        })
        self.store.transition(item["id"], "library_pending", "Acquired")
        coordinator = Coordinator(self.store, self.temp.name)
        with patch.dict(os.environ, {"ORBIT_MOVIES_DIR": movies, "ORBIT_TV_DIR": television}):
            coordinator.verify_library_handoffs()
        self.assertEqual(self.store.list_requests()[0]["status"], "ready")


if __name__ == "__main__":
    unittest.main()
