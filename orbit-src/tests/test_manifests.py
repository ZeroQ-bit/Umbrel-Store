import os
import tempfile
import unittest

from orbit.manifests import build_media_manifest


class MediaManifestTests(unittest.TestCase):
    def test_virtual_manifest_keeps_path_and_target_without_private_url(self):
        with tempfile.TemporaryDirectory() as data_dir:
            source_dir = os.path.join(data_dir, "remote")
            library_dir = os.path.join(data_dir, "Movies", "Dune")
            os.makedirs(source_dir)
            os.makedirs(library_dir)
            source = os.path.join(source_dir, "Dune.mkv")
            link = os.path.join(library_dir, "Dune.mkv")
            with open(source, "wb") as handle:
                handle.write(b"video")
            os.symlink(source, link)
            items = [{
                "plex_rating_key": "101",
                "section_id": "4",
                "media_type": "movie",
                "title": "Dune",
                "year": 2021,
                "tmdb_id": 438631,
                "imdb_id": "tt1160419",
                "quality": "4K HDR",
                "versions": [{
                    "file": link,
                    "available": True,
                    "resolution": "4K",
                }],
            }]

            manifest = build_media_manifest(items[0])

            self.assertEqual(manifest["identity"]["tmdb_id"], 438631)
            self.assertEqual(manifest["plex"]["quality"], "4K HDR")
            self.assertEqual(manifest["playback"]["sources"][0]["path"], link)
            self.assertEqual(
                manifest["playback"]["sources"][0]["symlink_target"], source
            )
            self.assertIsNone(manifest["playback"]["stream_url"])
            self.assertNotIn("token", str(manifest).lower())


if __name__ == "__main__":
    unittest.main()
