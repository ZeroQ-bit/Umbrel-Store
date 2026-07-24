import os
import tempfile
import unittest
from unittest import mock

from vortexo.mount import MountSupervisor


class MountSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.host_root = os.path.join(
            self.temporary.name, "umbrel", "data", "zeroq-media"
        )
        os.makedirs(self.host_root)
        self.environment = mock.patch.dict(
            os.environ,
            {
                "VORTEXO_DATA_DIR": os.path.join(self.temporary.name, "data"),
                "VORTEXO_MOUNTPOINT": "/downloads/.vortexo-source",
                "VORTEXO_HOST_MOUNT_PATH": os.path.join(
                    self.host_root, ".vortexo-source"
                ),
            },
        )
        self.environment.start()
        self.supervisor = MountSupervisor()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def test_refuses_foreign_mount_without_detaching_it(self):
        def mounted(path):
            return path in {"/downloads", "/downloads/.vortexo-source"}

        with mock.patch("vortexo.mount._is_mountpoint", side_effect=mounted):
            with mock.patch("vortexo.mount._filesystem_type", return_value="fuse.rclone"):
                with mock.patch("vortexo.mount.os.makedirs"):
                    with self.assertRaisesRegex(RuntimeError, "Another service already owns"):
                        self.supervisor.validate_storage()
        self.assertFalse(self.supervisor.owned)

    def test_missing_key_stays_unmounted(self):
        with mock.patch("vortexo.mount._is_mountpoint", return_value=False):
            self.supervisor.start()
            health = self.supervisor.health()
        self.assertFalse(health["online"])
        self.assertIn("TorBox API key", health["detail"])

    def test_refuses_unexpected_host_media_root(self):
        self.supervisor.host_mount_path = "/tmp/not-the-umbrel-root/source"
        with self.assertRaisesRegex(RuntimeError, "unexpected host media root"):
            self.supervisor.validate_storage()


if __name__ == "__main__":
    unittest.main()
