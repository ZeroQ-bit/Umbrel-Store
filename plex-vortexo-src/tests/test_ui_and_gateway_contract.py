from pathlib import Path
import importlib.util
import unittest


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = ROOT.parent


class UIContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.javascript = (ROOT / "web" / "plex-vortexo.js").read_text()
        cls.nginx = (ROOT / "nginx.conf").read_text()
        cls.entrypoint = (ROOT / "entrypoint.sh").read_text()
        cls.service = (ROOT / "vortexo" / "service.py").read_text()
        cls.mount = (ROOT / "vortexo" / "mount.py").read_text()

    def test_discover_card_targets_the_existing_english_provider_row(self):
        self.assertIn('"Watch from these locations"', self.javascript)
        self.assertIn("row.insertBefore(card, moreCard || row.firstChild)", self.javascript)
        self.assertIn("card.dataset.vortexoTorbox", self.javascript)
        self.assertIn("!state.authenticated", self.javascript)
        self.assertIn("MutationObserver", self.javascript)

    def test_ui_contains_setup_episode_results_player_and_escape_states(self):
        for expected in (
            "Connect TorBox to Plex",
            "Season<select",
            "Play Now",
            "Add to Plex",
            "vortexo-player-overlay",
            'event.key !== "Escape"',
            'window.addEventListener("popstate"',
            "Automatically import my Plex Watchlist",
            "/vortexo/api/watchlist/sync",
        ):
            self.assertIn(expected, self.javascript)

    def test_owner_token_is_not_written_to_browser_storage_or_logged(self):
        self.assertNotIn("localStorage", self.javascript)
        self.assertNotIn("sessionStorage", self.javascript)
        self.assertNotIn("console.log", self.javascript)
        self.assertIn('HttpOnly; SameSite=Strict', (ROOT / "vortexo" / "service.py").read_text())

    def test_gateway_injects_assets_only_into_plex_web_and_proxies_everything_else(self):
        self.assertIn("location = /web/index.html", self.nginx)
        self.assertIn('sub_filter "</head>"', self.nginx)
        self.assertIn("location / {", self.nginx)
        self.assertIn("proxy_set_header Upgrade $http_upgrade", self.nginx)
        self.assertIn("proxy_set_header Range $http_range", self.nginx)
        self.assertIn("listen 32500", self.nginx)

    def test_companion_ports_do_not_overlap_plex_internal_ports(self):
        self.assertIn("127.0.0.1:32502", self.nginx)
        self.assertIn('"http://127.0.0.1:32501"', self.service)
        self.assertIn('"VORTEXO_API_PORT", "32502"', self.service)
        self.assertIn('"VORTEXO_MOUNT_PORT", "32501"', self.mount)
        for reserved_port in ("32401", "32402", "32403"):
            self.assertNotIn(reserved_port, self.nginx)
            self.assertNotIn(reserved_port, self.service)
            self.assertNotIn(reserved_port, self.mount)

    def test_unprivileged_nginx_uses_only_writable_runtime_paths(self):
        for runtime in ("client", "fastcgi", "proxy", "scgi", "uwsgi"):
            self.assertIn(f"/tmp/nginx/{runtime}", self.nginx)
            self.assertIn(f"/tmp/nginx/{runtime}", self.entrypoint)
        self.assertIn("error_log /tmp/nginx/error.log", self.nginx)
        self.assertIn("nginx -e /tmp/nginx/error.log", self.entrypoint)
        self.assertIn("tail -n 0 -f /tmp/nginx/error.log", self.entrypoint)
        self.assertNotIn("/dev/stderr", self.nginx)
        self.assertNotIn("/dev/stderr", self.entrypoint)
        self.assertNotIn("sub_filter_types text/html", self.nginx)

    def test_store_updater_digest_pins_both_companion_roles(self):
        module_path = REPOSITORY_ROOT / "scripts" / "update_store_apps.py"
        spec = importlib.util.spec_from_file_location("store_updater", module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        compose = """
services:
  gateway:
    image: ghcr.io/zeroq-bit/plex-vortexo:main
  mount:
    image: ghcr.io/zeroq-bit/plex-vortexo:main
"""
        digest = "sha256:" + ("a" * 64)
        updated, changed = module.replace_image_reference(
            compose,
            repository="ghcr.io/zeroq-bit/plex-vortexo",
            tag="main",
            digest=digest,
        )
        self.assertTrue(changed)
        self.assertEqual(updated.count(f"plex-vortexo:main@{digest}"), 2)


if __name__ == "__main__":
    unittest.main()
