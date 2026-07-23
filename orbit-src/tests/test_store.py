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
        self.assertTrue(source["created"])

    def test_reconnecting_list_updates_existing_source(self):
        first = self.store.add_list_source({
            "name": "New releases", "kind": "mdblist",
            "url": "https://mdblist.com/lists/example/new-releases/",
            "profile": "best", "max_items": 100,
        })
        second = self.store.add_list_source({
            "name": "New Releases On Stremio", "kind": "mdblist",
            "url": "https://mdblist.com/lists/example/new-releases",
            "profile": "1080p", "max_items": 1000,
        })
        self.assertEqual(first["id"], second["id"])
        self.assertFalse(second["created"])
        self.assertEqual(second["name"], "New Releases On Stremio")
        self.assertEqual(second["profile"], "1080p")
        self.assertEqual(second["max_items"], 1000)
        self.assertEqual(len(self.store.list_sources()), 1)

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

    def test_plex_library_is_replaceable_searchable_and_matchable(self):
        self.store.replace_plex_library([{
            "plex_rating_key": "101",
            "section_id": "4",
            "media_type": "movie",
            "title": "Dune",
            "year": 2021,
            "tmdb_id": 438631,
            "imdb_id": "tt1160419",
            "quality": "720p",
            "versions": [{"resolution": "720p", "dynamic_range": "SDR"}],
            "upgrade_available": True,
        }])
        results = self.store.list_plex_library("dun")
        self.assertEqual(results[0]["quality"], "720p")
        self.assertTrue(results[0]["upgrade_available"])
        match = self.store.match_plex_library({
            "media_type": "movie", "title": "Dune", "tmdb_id": 438631,
        })
        self.assertEqual(match["plex_rating_key"], "101")
        self.assertEqual(self.store.plex_library_status()["item_count"], 1)

    def test_failed_plex_refresh_preserves_last_inventory(self):
        self.store.replace_plex_library([{
            "plex_rating_key": "101", "section_id": "4", "media_type": "movie",
            "title": "Dune", "quality": "1080p", "versions": [],
        }])
        self.store.fail_plex_library_sync("Plex unavailable")
        self.assertEqual(len(self.store.list_plex_library()), 1)
        self.assertEqual(self.store.plex_library_status()["last_error"], "Plex unavailable")

    def test_library_manager_filters_sorts_pages_and_reports_totals(self):
        self.store.replace_plex_library([
            {
                "plex_rating_key": "101", "section_id": "4", "media_type": "movie",
                "title": "Dune", "year": 2021, "quality": "4K HDR",
                "versions": [], "upgrade_available": False,
            },
            {
                "plex_rating_key": "102", "section_id": "5", "media_type": "show",
                "title": "Foundation", "year": 2021, "quality": "720p",
                "versions": [], "upgrade_available": True, "episode_count": 20,
                "seasons": [{"number": 1, "title": "Season 1", "episode_count": 10, "quality": "720p"}],
            },
            {
                "plex_rating_key": "103", "section_id": "4", "media_type": "movie",
                "title": "Arrival", "year": 2016, "quality": "1080p",
                "versions": [], "upgrade_available": False,
            },
        ])
        self.assertEqual(
            [item["title"] for item in self.store.list_plex_library(sort="year", limit=2)],
            ["Dune", "Foundation"],
        )
        self.assertEqual(
            self.store.list_plex_library(status="upgrade")[0]["title"], "Foundation"
        )
        self.assertEqual(
            self.store.list_plex_library(quality="4k")[0]["title"], "Dune"
        )
        self.assertEqual(
            self.store.list_plex_library(limit=1, offset=1)[0]["title"], "Dune"
        )
        stats = self.store.plex_library_stats(media_type="movie")
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["movies"], 2)
        self.assertEqual(stats["shows"], 1)
        self.assertEqual(stats["filtered"], 2)
        show = self.store.get_plex_library_item(
            self.store.list_plex_library(media_type="show")[0]["id"]
        )
        self.assertEqual(show["seasons"][0]["episode_count"], 10)

    def test_automatic_list_skips_titles_already_in_plex(self):
        self.store.replace_plex_library([{
            "plex_rating_key": "101", "section_id": "4", "media_type": "movie",
            "title": "Dune", "year": 2021, "tmdb_id": 438631,
            "quality": "1080p", "versions": [],
        }])
        source = self.store.add_list_source({
            "name": "New releases", "kind": "mdblist",
            "url": "https://mdblist.com/lists/example/new-releases",
        })
        coordinator = Coordinator(self.store, self.temp.name)
        with patch("orbit.worker.fetch_list", return_value=[
            {"media_type": "movie", "title": "Dune", "year": 2021, "tmdb_id": 438631},
            {"media_type": "movie", "title": "Arrival", "year": 2016, "tmdb_id": 329865},
        ]):
            result = coordinator.sync_list(source["id"])
        self.assertEqual(result["skipped_existing"], 1)
        self.assertEqual(result["added"], 1)
        self.assertEqual(self.store.list_requests()[0]["title"], "Arrival")


if __name__ == "__main__":
    unittest.main()
