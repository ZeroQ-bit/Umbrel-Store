import os
import stat
import tempfile
import unittest

from vortexo.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_database_and_directory_are_private(self):
        self.assertEqual(stat.S_IMODE(os.stat(self.temporary.name).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.store.path).st_mode), 0o600)

    def test_public_stream_redacts_urls_headers_and_torrent_identity(self):
        public = self.store.save_streams(
            "discover",
            [
                {
                    "title": "Release",
                    "quality": "4K",
                    "url": "https://signed.example/secret",
                    "headers": {"Authorization": "secret"},
                    "magnet": "magnet:?xt=urn:btih:secret",
                    "info_hash": "secret",
                    "can_play_now": True,
                    "can_add": True,
                }
            ],
        )[0]
        self.assertEqual(public["title"], "Release")
        for forbidden in ("url", "headers", "magnet", "info_hash"):
            self.assertNotIn(forbidden, public)
        private = self.store.stream(public["id"])
        self.assertEqual(private["info_hash"], "secret")

    def test_job_is_idempotent_and_persists_every_stage(self):
        payload = {"media": {"title": "Memento"}, "stream": {"info_hash": "abc"}}
        first, created = self.store.create_or_get_job(
            "discover|abc", "discover", "stream", payload
        )
        duplicate, duplicate_created = self.store.create_or_get_job(
            "discover|abc", "discover", "stream-two", payload
        )
        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(first["id"], duplicate["id"])

        stages = [
            "torbox_accepted",
            "debrid_ready",
            "mount_visible",
            "linked",
            "plex_scan_requested",
            "plex_confirmed",
        ]
        for stage in stages:
            self.store.transition(first["id"], stage, stage.replace("_", " "))
        final = self.store.job(first["id"])
        self.assertEqual(
            [event["status"] for event in final["history"]],
            ["selected", *stages],
        )
        self.assertEqual(final["status"], "plex_confirmed")

    def test_completed_progress_is_sticky(self):
        self.store.save_progress("discover", 95, 100, True)
        saved = self.store.save_progress("discover", 20, 100, False)
        self.assertTrue(saved["completed"])

    def test_watchlist_state_is_persistent_and_job_linked(self):
        media = {
            "discover_id": "movie-discover",
            "type": "movie",
            "title": "Memento",
            "imdb_id": "tt0209144",
        }
        detected = self.store.upsert_watchlist_item("movie:tt0209144", media)
        self.assertEqual(detected["status"], "detected")
        job, _ = self.store.create_or_get_job(
            "movie-discover|release",
            "movie-discover",
            "stream-id",
            {"media": media, "stream": {"info_hash": "release"}},
        )
        self.store.update_watchlist_item(
            "movie:tt0209144",
            "selected",
            "Release selected",
            job_id=job["id"],
            increment_attempts=True,
        )
        self.store.update_watchlist_for_job(
            job["id"], "plex_confirmed", "Plex confirmed the media version"
        )
        final = self.store.watchlist_item("movie:tt0209144")
        self.assertEqual(final["status"], "plex_confirmed")
        self.assertEqual(final["attempts"], 1)

    def test_failed_library_job_can_be_retried_without_losing_history(self):
        payload = {"media": {"title": "Memento"}, "stream": {"info_hash": "abc"}}
        job, _ = self.store.create_or_get_job(
            "discover|abc", "discover", "stream", payload
        )
        self.store.transition(job["id"], "failed", "TorBox was offline")
        retried = self.store.retry_job(job["id"])
        self.assertEqual(retried["status"], "selected")
        self.assertEqual(
            [event["status"] for event in retried["history"]],
            ["selected", "failed", "selected"],
        )

    def test_only_nonterminal_library_jobs_are_resumable(self):
        active, _ = self.store.create_or_get_job(
            "active", "discover-active", "stream-active", {}
        )
        finished, _ = self.store.create_or_get_job(
            "finished", "discover-finished", "stream-finished", {}
        )
        self.store.transition(finished["id"], "plex_confirmed", "Done")
        self.assertEqual(
            [job["id"] for job in self.store.resumable_jobs()],
            [active["id"]],
        )

    def test_watchlist_sync_summary_persists_counts(self):
        self.store.begin_watchlist_sync()
        self.store.complete_watchlist_sync(
            "completed",
            "Queued one item",
            {"found": 3, "queued": 1, "skipped_existing": 2},
        )
        status = self.store.watchlist_status()
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["found"], 3)
        self.assertEqual(status["queued"], 1)
        self.assertEqual(status["skipped_existing"], 2)


if __name__ == "__main__":
    unittest.main()
