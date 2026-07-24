import os
import subprocess
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

    def test_recovers_only_disconnected_mount_with_owner_marker(self):
        os.makedirs(os.path.dirname(self.supervisor.owner_marker), exist_ok=True)
        with open(self.supervisor.owner_marker, "w", encoding="utf-8") as handle:
            handle.write("123\n")
        detached = [False]

        def disconnected(_path):
            return not detached[0]

        def run(*args):
            self.assertEqual(args, ("fusermount3", "-uz", self.supervisor.mountpoint))
            detached[0] = True
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch("vortexo.mount._is_disconnected", side_effect=disconnected):
            with mock.patch("vortexo.mount._run", side_effect=run):
                self.supervisor._recover_stale_owned_mount()
        self.assertFalse(os.path.exists(self.supervisor.owner_marker))

    def test_refuses_disconnected_mount_without_owner_marker(self):
        with mock.patch("vortexo.mount._is_disconnected", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "without Plex Vortexo ownership"):
                self.supervisor._recover_stale_owned_mount()

    def test_owned_mount_is_detached_before_process_is_stopped(self):
        events = []
        process = mock.Mock()
        process.pid = 123
        process.poll.return_value = None
        process.wait.return_value = None
        self.supervisor.process = process
        self.supervisor.owned = True

        def run(*args):
            events.append(("run", args))
            return subprocess.CompletedProcess(args, 0, "", "")

        def killpg(*args):
            events.append(("killpg", args))

        with mock.patch("vortexo.mount._run", side_effect=run):
            with mock.patch("vortexo.mount.os.killpg", side_effect=killpg):
                self.supervisor._stop_owned_process()

        self.assertEqual(
            events[0],
            ("run", ("fusermount3", "-u", self.supervisor.mountpoint)),
        )
        self.assertEqual(events[1][0], "killpg")


if __name__ == "__main__":
    unittest.main()
