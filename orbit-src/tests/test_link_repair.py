import os
import tempfile
import unittest
from unittest.mock import patch

from orbit import link_repair


class LinkRepairTests(unittest.TestCase):
    def test_broken_movie_link_is_atomically_retargeted(self):
        with tempfile.TemporaryDirectory() as directory:
            mount = os.path.join(directory, "downloads")
            source = os.path.join(
                mount, ".vortexo-source", "Dune.2021.1080p.WEB-DL"
            )
            movies = os.path.join(mount, "vortexo", "Movies")
            television = os.path.join(mount, "vortexo", "TV")
            folder = os.path.join(movies, "Dune (2021) {tmdb-438631}")
            os.makedirs(source)
            os.makedirs(folder)
            os.makedirs(television)
            target = os.path.join(source, "Dune.2021.1080p.mkv")
            with open(target, "wb") as handle:
                handle.write(b"video")
            link = os.path.join(folder, "Dune (2021) {tmdb-438631}.mkv")
            os.symlink(
                os.path.join(
                    mount, ".vortexo-source", "Dune.old.release", "Dune.mkv"
                ),
                link,
            )
            self.assertTrue(os.path.islink(link))
            self.assertFalse(os.path.exists(link))

            with patch.object(link_repair, "_fetch_torrents", return_value=[{
                "name": "Dune.2021.1080p.WEB-DL",
                "cached": True,
            }]):
                result = link_repair.repair_broken_symlinks(
                    "token",
                    mount,
                    {"movie": movies, "show": television},
                    candidate_links={link},
                )

            self.assertEqual(result["repaired"], 1)
            self.assertTrue(os.path.exists(link))
            self.assertTrue(os.path.samefile(link, target))

    def test_working_link_and_regular_file_are_never_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            mount = os.path.join(directory, "downloads")
            source = os.path.join(mount, ".vortexo-source", "Arrival.2016.1080p")
            movies = os.path.join(mount, "vortexo", "Movies")
            folder = os.path.join(movies, "Arrival (2016) {tmdb-329865}")
            os.makedirs(source)
            os.makedirs(folder)
            target = os.path.join(source, "Arrival.mkv")
            with open(target, "wb") as handle:
                handle.write(b"video")
            link = os.path.join(folder, "Arrival.mkv")
            os.symlink(target, link)
            regular = os.path.join(folder, "local.mp4")
            with open(regular, "wb") as handle:
                handle.write(b"local")

            with patch.object(link_repair, "_fetch_torrents", return_value=[]):
                result = link_repair.repair_broken_symlinks(
                    "token",
                    mount,
                    {"movie": movies, "show": ""},
                )

            self.assertEqual(result["repaired"], 0)
            self.assertTrue(os.path.samefile(link, target))
            with open(regular, "rb") as handle:
                self.assertEqual(handle.read(), b"local")


if __name__ == "__main__":
    unittest.main()
