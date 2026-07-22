import unittest
from unittest.mock import patch

from orbit import integrations


class IntegrationTests(unittest.TestCase):
    def test_tmdb_search_normalises_movie_and_show(self):
        payload = {"results": [
            {"id": 1, "media_type": "movie", "title": "One", "release_date": "2026-02-03"},
            {"id": 2, "media_type": "tv", "name": "Two", "first_air_date": "2025-01-02"},
            {"id": 3, "media_type": "person", "name": "Skip"},
        ]}
        with patch.object(integrations, "_json_request", return_value=payload):
            results = integrations.search_tmdb("test", "key")
        self.assertEqual([(item["media_type"], item["title"], item["year"]) for item in results], [
            ("movie", "One", 2026), ("show", "Two", 2025),
        ])

    def test_mdblist_url_becomes_authenticated_items_endpoint(self):
        with patch.object(integrations, "_json_request", return_value=[{
            "mediatype": "movie", "title": "Dune", "year": 2021,
            "imdb_id": "tt1160419", "tmdb_id": 438631,
        }]) as request:
            items = integrations.fetch_mdblist(
                "https://mdblist.com/lists/example/great-films", "secret", 25
            )
        self.assertEqual(items[0]["tmdb_id"], 438631)
        self.assertEqual(request.call_args.args[0], "https://api.mdblist.com/lists/example/great-films/items")
        self.assertEqual(request.call_args.args[1]["Authorization"], "Bearer secret")

    def test_trakt_nested_items_are_normalised(self):
        with patch.object(integrations, "_json_request", return_value=[{
            "show": {"title": "Severance", "year": 2022, "ids": {"tmdb": 95396, "imdb": "tt11280740"}}
        }]):
            items = integrations.fetch_trakt(
                "https://trakt.tv/users/example/lists/favourites", "client", 50
            )
        self.assertEqual(items[0]["media_type"], "show")
        self.assertEqual(items[0]["tmdb_id"], 95396)


if __name__ == "__main__":
    unittest.main()
