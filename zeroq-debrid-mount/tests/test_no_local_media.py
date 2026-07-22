import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT_MODULE_PATH = Path(__file__).resolve().parents[1] / "web_ui.py"
MODULE_PATH = Path(__file__).resolve().parents[1] / "hooks" / "runtime" / "web_ui.py"


def load_module(config_dir, status_dir, mountpoint, ram_cache):
    values = {
        "DEBRID_CONFIG_DIR": str(config_dir),
        "DEBRID_STATUS_DIR": str(status_dir),
        "DEBRID_MOUNTPOINT": str(mountpoint),
        "DEBRID_RAM_CACHE_DIR": str(ram_cache),
    }
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        spec = importlib.util.spec_from_file_location("debrid_mount_web_ui_test", MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class NoLocalMediaTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.config = root / "config"
        self.status = root / "status"
        self.mountpoint = root / "source"
        self.ram_cache = root / "ram-cache"
        self.config.mkdir()
        self.status.mkdir()
        self.module = load_module(
            self.config, self.status, self.mountpoint, self.ram_cache)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_stale_full_cache_config_is_migrated_without_losing_credentials(self):
        Path(self.module.CONFIG_FILE).write_text(
            "DEBRID_MODE='webdav'\n"
            "DEBRID_WEBDAV_USER='torbox-user'\n"
            "DEBRID_WEBDAV_PASS='torbox-pass'\n"
            "DEBRID_RCLONE_VFS_CACHE_MODE='full'\n"
            "DEBRID_RCLONE_VFS_CACHE_MAX_SIZE='20G'\n"
            "DEBRID_RCLONE_VFS_CACHE_MAX_AGE='6h'\n"
        )

        self.module.enforce_no_local_media_config()

        config = self.module.read_config()
        self.assertEqual(config["DEBRID_WEBDAV_USER"], "torbox-user")
        self.assertEqual(config["DEBRID_WEBDAV_PASS"], "torbox-pass")
        self.assertEqual(config["DEBRID_RCLONE_VFS_CACHE_MODE"], "off")
        self.assertNotIn("DEBRID_RCLONE_VFS_CACHE_MAX_SIZE", config)
        self.assertNotIn("DEBRID_RCLONE_VFS_CACHE_MAX_AGE", config)

    def test_deployed_runtime_matches_repository_mirror(self):
        self.assertEqual(MODULE_PATH.read_bytes(), ROOT_MODULE_PATH.read_bytes())

    def test_submitted_cache_mode_is_coerced_to_off(self):
        self.module.write_sample_config()
        self.module.write_config({"DEBRID_RCLONE_VFS_CACHE_MODE": "full"})

        self.assertEqual(
            self.module.read_config()["DEBRID_RCLONE_VFS_CACHE_MODE"], "off")
        fields = {field["key"]: field for field in self.module.config_for_ui()["fields"]}
        self.assertEqual(fields["DEBRID_RCLONE_VFS_CACHE_MODE"]["options"], ["off"])

    def test_rclone_args_force_read_only_streaming_without_disk_vfs_flags(self):
        self.module.write_sample_config()
        args = self.module.Mount()._rclone_args()

        self.assertIn("--read-only", args)
        self.assertEqual(args[args.index("--vfs-cache-mode") + 1], "off")
        self.assertEqual(args[args.index("--cache-dir") + 1], str(self.ram_cache))
        self.assertEqual(
            args[args.index("--buffer-size") + 1],
            self.module.STREAM_BUFFER_SIZE)
        self.assertEqual(
            args[args.index("--vfs-read-wait") + 1],
            self.module.STREAM_READ_WAIT)
        self.assertEqual(args[args.index("--attr-timeout") + 1], "30s")
        self.assertEqual(args[args.index("--max-read-ahead") + 1], "4M")
        self.assertIn("--no-checksum", args)
        self.assertIn("--no-modtime", args)
        self.assertIn("--vfs-fast-fingerprint", args)
        self.assertNotIn("--allow-non-empty", args)
        self.assertNotIn("--vfs-cache-max-size", args)
        self.assertNotIn("--vfs-cache-max-age", args)

    def test_legacy_short_directory_cache_is_migrated_for_plex_probes(self):
        Path(self.module.CONFIG_FILE).write_text(
            "DEBRID_MODE='webdav'\n"
            "DEBRID_RCLONE_DIR_CACHE_TIME='10s'\n"
        )

        self.module.enforce_no_local_media_config()

        self.assertEqual(
            self.module.read_config()["DEBRID_RCLONE_DIR_CACHE_TIME"], "1m")

    def test_legacy_cache_cleanup_does_not_follow_symlinks(self):
        outside = Path(self.temp_dir.name) / "outside-media.mkv"
        outside.write_bytes(b"keep")
        cache = Path(self.module.LEGACY_RCLONE_CACHE)
        (cache / "vfs" / "nested").mkdir(parents=True)
        (cache / "vfs" / "nested" / "cached-media.mkv").write_bytes(b"remove")
        (cache / "outside-link").symlink_to(outside)

        self.module.purge_legacy_rclone_cache()

        self.assertFalse(cache.exists())
        self.assertEqual(outside.read_bytes(), b"keep")

    def test_top_level_legacy_cache_symlink_is_unlinked_only(self):
        outside_dir = Path(self.temp_dir.name) / "outside-cache"
        outside_dir.mkdir()
        outside_file = outside_dir / "keep.mkv"
        outside_file.write_bytes(b"keep")
        Path(self.module.LEGACY_RCLONE_CACHE).symlink_to(outside_dir, target_is_directory=True)

        self.module.purge_legacy_rclone_cache()

        self.assertFalse(os.path.lexists(self.module.LEGACY_RCLONE_CACHE))
        self.assertEqual(outside_file.read_bytes(), b"keep")

    def test_legacy_cache_cleanup_rejects_any_nested_mount(self):
        cache = Path(self.module.LEGACY_RCLONE_CACHE)
        cache.mkdir()
        cached_file = cache / "keep.mkv"
        cached_file.write_bytes(b"keep")

        with patch.object(
                self.module, "_mountpoints_at_or_below",
                return_value=[str(cache / "nested-bind")]):
            with self.assertRaisesRegex(RuntimeError, "mounted legacy cache"):
                self.module.purge_legacy_rclone_cache()

        self.assertEqual(cached_file.read_bytes(), b"keep")

    def test_nonempty_mountpoint_is_rejected_without_deleting_files(self):
        self.mountpoint.mkdir()
        local_media = self.mountpoint / "local-media.mkv"
        local_media.write_bytes(b"keep")

        with self.assertRaisesRegex(RuntimeError, "contains local files"):
            self.module.ensure_empty_mountpoint()

        self.assertEqual(local_media.read_bytes(), b"keep")

    def test_non_tmpfs_cache_path_is_rejected(self):
        with patch.object(
                self.module, "_mount_filesystem_type", return_value="ext4"):
            with self.assertRaisesRegex(RuntimeError, "not RAM-backed tmpfs"):
                self.module.ensure_ram_only_cache_dir()

    def test_health_safety_requires_host_hook_marker(self):
        self.assertFalse(self.module.safety_status()["ok"])
        Path(self.module.HOST_SAFETY_MARKER).write_text(
            str(Path(self.module.HOST_MOUNT_PATH).parent) + "\n")
        self.assertTrue(self.module.safety_status()["ok"])


if __name__ == "__main__":
    unittest.main()
