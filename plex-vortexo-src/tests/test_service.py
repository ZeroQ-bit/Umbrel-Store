import os
import tempfile
import unittest
from unittest import mock

from vortexo.integrations import IntegrationError
from vortexo.service import VortexoService


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = self.temporary.name
        self.data = os.path.join(root, "data")
        self.source = os.path.join(root, "source")
        self.movies = os.path.join(root, "library", "Movies")
        self.tv = os.path.join(root, "library", "TV")
        self.preferences = os.path.join(root, "Preferences.xml")
        os.makedirs(self.source)
        os.makedirs(self.movies)
        os.makedirs(self.tv)
        with open(self.preferences, "w", encoding="utf-8") as handle:
            handle.write('<Preferences PlexOnlineToken="owner-token"/>')
        self.environment = mock.patch.dict(
            os.environ,
            {
                "VORTEXO_DATA_DIR": self.data,
                "VORTEXO_SOURCE_ROOT": self.source,
                "VORTEXO_MOVIES_ROOT": self.movies,
                "VORTEXO_TV_ROOT": self.tv,
                "VORTEXO_PLEX_PREFERENCES": self.preferences,
            },
        )
        self.environment.start()
        self.service = VortexoService()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def _source_file(self, name="Memento.2000.1080p.mkv"):
        path = os.path.join(self.source, name)
        with open(path, "wb") as handle:
            handle.write(b"video")
        return path

    def test_owner_session_requires_exact_local_token(self):
        with mock.patch(
            "vortexo.service.plex_account",
            side_effect=[
                {"id": "owner", "uuid": "", "email": ""},
                {"id": "someone-else", "uuid": "", "email": ""},
            ],
        ):
            with self.assertRaises(PermissionError):
                self.service.establish_session("not-owner")
        session = self.service.establish_session("owner-token")
        self.assertTrue(self.service.valid_session(session))

    def test_distinct_plex_web_token_is_accepted_for_same_owner_account(self):
        with mock.patch(
            "vortexo.service.plex_account",
            side_effect=[
                {"id": "owner-account", "uuid": "", "email": ""},
                {"id": "owner-account", "uuid": "", "email": ""},
            ],
        ):
            session = self.service.establish_session("owner-web-token")
        self.assertTrue(self.service.valid_session(session))

    def test_movie_link_preserves_versions_and_is_idempotent(self):
        source = self._source_file()
        media = {"type": "movie", "title": "Memento", "year": 2000}
        first, existed = self.service._link_media(
            media, {"quality": "1080p", "info_hash": "abcdef123"}, source
        )
        self.assertFalse(existed)
        self.assertTrue(os.path.islink(first))
        repeated, existed = self.service._link_media(
            media, {"quality": "1080p", "info_hash": "abcdef123"}, source
        )
        self.assertTrue(existed)
        self.assertEqual(first, repeated)

        second_source = self._source_file("Memento.2000.2160p.mkv")
        second, existed = self.service._link_media(
            media, {"quality": "4K", "info_hash": "different987"}, second_source
        )
        self.assertFalse(existed)
        self.assertNotEqual(first, second)
        self.assertTrue(os.path.lexists(first))

    def test_episode_link_uses_exact_season_episode(self):
        source = self._source_file("Show.S02E04.mkv")
        linked, _ = self.service._link_media(
            {
                "type": "episode",
                "title": "Episode",
                "parent_title": "Show",
                "season": 2,
                "episode": 4,
            },
            {"quality": "4K", "info_hash": "episodehash"},
            source,
        )
        self.assertIn(os.path.join("Show", "Season 02"), linked)
        self.assertIn("S02E04", os.path.basename(linked))

    def test_refuses_source_outside_torbox_mount(self):
        outside = os.path.join(self.temporary.name, "outside.mkv")
        with open(outside, "wb") as handle:
            handle.write(b"video")
        with self.assertRaisesRegex(IntegrationError, "outside"):
            self.service._link_media(
                {"type": "movie", "title": "Unsafe"},
                {"quality": "4K", "info_hash": "abc"},
                outside,
            )

    def test_player_session_returns_local_url_and_never_raw_source(self):
        public = self.service.store.save_streams(
            "discover",
            [
                {
                    "url": "https://signed.example/private.mp4",
                    "file_name": "Movie.mp4",
                    "codec": "H.264",
                    "audio": "AAC",
                    "can_play_now": True,
                }
            ],
        )[0]
        response = self.service.create_play_session(
            {"discover_id": "discover", "stream_id": public["id"]}
        )
        self.assertEqual(response["mode"], "direct")
        self.assertTrue(response["play_url"].startswith("/vortexo/play/"))
        self.assertNotIn("signed.example", str(response))

    def test_progress_marks_complete_at_ninety_percent(self):
        with mock.patch.object(self.service, "_mark_discover_watched") as watched:
            with mock.patch.object(
                self.service, "_job_rating_key_for_discover", return_value=""
            ):
                saved = self.service.save_progress(
                    {"discover_id": "discover", "position_ms": 90, "duration_ms": 100}
                )
        self.assertTrue(saved["completed"])
        watched.assert_called_once_with("discover")

    def test_invalid_torbox_key_is_not_persisted(self):
        with mock.patch(
            "vortexo.service.TorBoxClient.health",
            side_effect=IntegrationError("TorBox rejected the API key"),
        ):
            with self.assertRaisesRegex(IntegrationError, "rejected"):
                self.service.update_settings({"torbox_api_key": "invalid-secret"})
        self.assertNotIn("torbox_api_key", self.service.store.settings())

    def test_status_reports_component_health_without_secrets(self):
        self.service.store.update_settings(
            {
                "torbox_api_key": "private-key",
                "stream_manifest_urls": ["https://sources.example/manifest.json"],
            }
        )

        def json_response(url, **_kwargs):
            if url.endswith("/health"):
                return {"online": True, "detail": "Mount online"}
            if "sources.example" in url:
                return {"resources": [{"name": "stream", "types": ["movie"]}]}
            raise AssertionError(url)

        response = mock.MagicMock()
        response.status = 200
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        with mock.patch("vortexo.service.json_request", side_effect=json_response):
            with mock.patch("urllib.request.urlopen", return_value=response):
                with mock.patch(
                    "vortexo.service.TorBoxClient.health",
                    return_value={"online": True, "detail": "Connected"},
                ):
                    status = self.service.public_status()
        self.assertTrue(status["plex"]["online"])
        self.assertTrue(status["torbox"]["online"])
        self.assertTrue(status["source_lookup"]["online"])
        self.assertTrue(status["mount"]["online"])
        self.assertNotIn("private-key", str(status))

    def test_plex_confirmation_requires_exact_linked_episode_version(self):
        def response(url, **_kwargs):
            if url.endswith("/allLeaves"):
                return {
                    "MediaContainer": {
                        "Metadata": [
                            {
                                "ratingKey": "episode-key",
                                "parentIndex": 2,
                                "index": 4,
                            }
                        ]
                    }
                }
            if url.endswith("/library/metadata/episode-key"):
                return {
                    "MediaContainer": {
                        "Metadata": [
                            {
                                "Media": [
                                    {
                                        "Part": [
                                            {
                                                "file": (
                                                    "/downloads/vortexo/TV/Show/Season 02/"
                                                    "Show - S02E04 - 4K.mkv"
                                                )
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                }
            raise AssertionError(url)

        with mock.patch("vortexo.service.json_request", side_effect=response):
            episode_key = self.service._episode_rating_key("show-key", 2, 4)
            self.assertEqual(episode_key, "episode-key")
            self.assertTrue(
                self.service._plex_item_contains_file(
                    episode_key,
                    "/downloads/vortexo/TV/Show/Season 02/Show - S02E04 - 4K.mkv",
                )
            )


if __name__ == "__main__":
    unittest.main()
