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


if __name__ == "__main__":
    unittest.main()
