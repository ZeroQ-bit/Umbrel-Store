from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .store import Store


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, text=True, capture_output=True, check=False)


def _is_mountpoint(path: str) -> bool:
    return _run("mountpoint", "-q", path).returncode == 0


def _filesystem_type(path: str) -> str:
    result = _run("findmnt", "-n", "-o", "FSTYPE", "-M", path)
    return result.stdout.strip() if result.returncode == 0 else ""


class MountSupervisor:
    """Owns only the Plex (Vortexo) rclone process and refuses foreign mounts."""

    def __init__(self):
        self.data_dir = os.path.abspath(os.environ.get("VORTEXO_DATA_DIR", "/data/vortexo"))
        self.mountpoint = os.path.abspath(
            os.environ.get("VORTEXO_MOUNTPOINT", "/downloads/.vortexo-source")
        )
        self.host_mount_path = os.path.abspath(
            os.environ.get("VORTEXO_HOST_MOUNT_PATH", "")
        )
        self.store = Store(self.data_dir)
        self.mount_dir = os.path.join(self.data_dir, "mount")
        self.config_path = os.path.join(self.mount_dir, "rclone.conf")
        self.log_path = os.path.join(self.mount_dir, "rclone.log")
        self.owner_marker = os.path.join(self.mount_dir, "owned-by-plex-vortexo")
        self.process: subprocess.Popen | None = None
        self.owned = False
        self.error = ""
        self.detail = "Mount is not configured"
        self._lock = threading.RLock()
        os.makedirs(self.mount_dir, mode=0o700, exist_ok=True)

    def validate_storage(self):
        if self.mountpoint != "/downloads/.vortexo-source":
            raise RuntimeError(f"Refusing unexpected mount path {self.mountpoint}")
        if not self.host_mount_path:
            raise RuntimeError("Host mount path is missing")
        host_parent = os.path.dirname(self.host_mount_path)
        if not host_parent.endswith("/data/zeroq-media"):
            raise RuntimeError(f"Refusing unexpected host media root {host_parent}")
        if not _is_mountpoint("/downloads"):
            raise RuntimeError("Refusing media root that is not a dedicated bind mount")
        os.makedirs(self.mountpoint, mode=0o775, exist_ok=True)
        if _is_mountpoint(self.mountpoint):
            fs_type = _filesystem_type(self.mountpoint) or "unknown"
            raise RuntimeError(
                f"Another service already owns {self.mountpoint} ({fs_type}); stop it before Plex Vortexo"
            )
        try:
            entries = os.listdir(self.mountpoint)
        except OSError as error:
            raise RuntimeError(f"Cannot inspect mountpoint safely: {error}") from error
        if entries:
            raise RuntimeError(f"Refusing to hide local files in {self.mountpoint}")

    def _obscure(self, password: str) -> str:
        result = _run("rclone", "obscure", password)
        if result.returncode != 0 or not result.stdout.strip():
            raise RuntimeError("Could not prepare TorBox WebDAV credentials")
        return result.stdout.strip()

    def _write_config(self, api_key: str, webdav_url: str):
        hidden = self._obscure(api_key)
        temporary = f"{self.config_path}.tmp"
        content = "\n".join(
            [
                "[torbox]",
                "type = webdav",
                f"url = {webdav_url}",
                "vendor = other",
                "user = torbox",
                f"pass = {hidden}",
                "",
            ]
        )
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.config_path)

    def start(self):
        with self._lock:
            if self.process and self.process.poll() is None:
                return
            self.error = ""
            settings = self.store.settings()
            api_key = str(settings.get("torbox_api_key") or "").strip()
            if not api_key:
                self.detail = "Add a TorBox API key in Plex Vortexo"
                return
            webdav_url = str(
                settings.get("webdav_url") or "https://webdav.torbox.app"
            ).rstrip("/")
            try:
                self.validate_storage()
                self._write_config(api_key, webdav_url)
                command = [
                    "rclone",
                    "mount",
                    "torbox:",
                    self.mountpoint,
                    "--config",
                    self.config_path,
                    "--allow-other",
                    "--read-only",
                    "--vfs-cache-mode",
                    "off",
                    "--dir-cache-time",
                    "5m",
                    "--poll-interval",
                    "30s",
                    "--umask",
                    "002",
                    "--log-file",
                    self.log_path,
                    "--log-level",
                    "INFO",
                ]
                self.process = subprocess.Popen(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                deadline = time.time() + 20
                while time.time() < deadline:
                    if self.process.poll() is not None:
                        raise RuntimeError("rclone exited before the TorBox mount became ready")
                    if _is_mountpoint(self.mountpoint):
                        self.owned = True
                        with open(self.owner_marker, "w", encoding="utf-8") as handle:
                            handle.write(f"{self.process.pid}\n")
                        os.chmod(self.owner_marker, 0o600)
                        self.detail = "TorBox mount is online"
                        return
                    time.sleep(0.5)
                raise RuntimeError("Timed out waiting for the TorBox mount")
            except Exception as error:
                self.error = str(error)
                self.detail = self.error
                self._stop_owned_process()

    def _stop_owned_process(self):
        process = self.process
        if process and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if self.owned and _is_mountpoint(self.mountpoint):
            # This branch is permitted only for the process started by this
            # supervisor in this lifetime.
            result = _run("fusermount3", "-u", self.mountpoint)
            if result.returncode != 0:
                self.error = "Owned rclone stopped, but its FUSE mount did not detach"
        self.process = None
        self.owned = False
        try:
            os.unlink(self.owner_marker)
        except FileNotFoundError:
            pass

    def stop(self):
        with self._lock:
            self._stop_owned_process()
            self.detail = "TorBox mount is stopped"

    def restart(self):
        with self._lock:
            self._stop_owned_process()
            if _is_mountpoint(self.mountpoint):
                self.error = "A mount remains after the owned rclone process stopped"
                self.detail = self.error
                return
        self.start()

    def health(self) -> dict:
        with self._lock:
            running = bool(self.process and self.process.poll() is None)
            mounted = _is_mountpoint(self.mountpoint)
            online = bool(running and mounted and self.owned)
            if running and not mounted and not self.error:
                self.detail = "rclone is starting"
            elif not running and mounted and not self.owned:
                self.detail = "A foreign mount is active; Plex Vortexo will not take ownership"
            return {
                "online": online,
                "mounted": mounted,
                "owned": self.owned,
                "detail": self.detail,
                "error": self.error or None,
            }


class MountHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PlexVortexoMount/0.1"

    @property
    def supervisor(self) -> MountSupervisor:
        return self.server.supervisor  # type: ignore[attr-defined]

    def log_message(self, format, *args):
        path = urllib.parse.urlsplit(self.path).path
        print(f"[mount] {self.command} {path}", flush=True)

    def _json(self, value, status: int = 200):
        body = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        if urllib.parse.urlsplit(self.path).path == "/health":
            self._json(self.supervisor.health())
        else:
            self._json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/restart":
            threading.Thread(target=self.supervisor.restart, daemon=True).start()
            self._json({"accepted": True}, HTTPStatus.ACCEPTED)
        else:
            self._json({"error": "Not found"}, HTTPStatus.NOT_FOUND)


def serve():
    supervisor = MountSupervisor()
    thread = threading.Thread(target=supervisor.start, name="vortexo-rclone-start", daemon=True)
    thread.start()
    port = int(os.environ.get("VORTEXO_MOUNT_PORT", "32501"))
    server = ThreadingHTTPServer(("127.0.0.1", port), MountHandler)
    server.daemon_threads = True
    server.supervisor = supervisor  # type: ignore[attr-defined]
    print(f"[mount] supervisor listening on 127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever()
    finally:
        supervisor.stop()


if __name__ == "__main__":
    serve()
