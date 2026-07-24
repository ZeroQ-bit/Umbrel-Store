import builtins
import unittest

from orbit.acquire_legacy import load_engine_settings


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


if __name__ == "__main__":
    unittest.main()
