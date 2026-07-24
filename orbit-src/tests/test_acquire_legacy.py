import builtins
import unittest
from types import SimpleNamespace

from orbit.acquire_legacy import (
    apply_quality_profile,
    load_engine_settings,
    replacement_scope,
    restrict_replacement_item,
)


class FakeUI:
    def __init__(self):
        self.answer = None

    def load(self):
        self.answer = input("Press Enter to update your settings:")


class AcquireLegacyTests(unittest.TestCase):
    def test_settings_migration_is_non_interactive_and_restores_input(self):
        fake_ui = FakeUI()
        original_input = builtins.input

        load_engine_settings(fake_ui)

        self.assertEqual(fake_ui.answer, "")
        self.assertIs(builtins.input, original_input)

    def test_episode_replacement_restricts_matched_show(self):
        show = SimpleNamespace(Seasons=[
            SimpleNamespace(index=1, Episodes=[
                SimpleNamespace(index=1), SimpleNamespace(index=2),
            ]),
            SimpleNamespace(index=2, Episodes=[SimpleNamespace(index=1)]),
        ])
        scope = {"scope": "episode", "season_number": 1, "episode_number": 2}
        self.assertTrue(restrict_replacement_item(show, scope))
        self.assertEqual([season.index for season in show.Seasons], [1])
        self.assertEqual([episode.index for episode in show.Seasons[0].Episodes], [2])

    def test_replacement_scope_and_quality_profile(self):
        job = {
            "source": "library-replace",
            "source_ref": '{"scope":"season","season_number":3}',
        }
        self.assertEqual(replacement_scope(job)["season_number"], 3)
        releases = SimpleNamespace(sort=SimpleNamespace(versions=[]))
        apply_quality_profile(releases, "4k")
        rules = releases.sort.versions[0][3]
        self.assertIn(["resolution", "requirement", "==", "2160"], rules)


if __name__ == "__main__":
    unittest.main()
