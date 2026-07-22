"""Background queue and automatic list coordinator."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time
import re

from .integrations import IntegrationError, fetch_list
from .store import Store


class Coordinator:
    def __init__(self, store: Store, data_dir: str):
        self.store = store
        self.data_dir = data_dir
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.last_list_poll = 0.0

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._run, name="orbit-coordinator", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def _run(self):
        while not self.stop_event.wait(3):
            try:
                self.process_one()
                self.verify_library_handoffs()
                interval = int(self.store.get_settings(True).get("list_poll_minutes", "60")) * 60
                if time.monotonic() - self.last_list_poll >= max(300, interval):
                    self.sync_all_lists()
                    self.last_list_poll = time.monotonic()
            except Exception:
                # Keep the dashboard alive even when one background operation fails.
                time.sleep(2)

    def process_one(self):
        job = self.store.next_queued()
        command = os.environ.get("ORBIT_ACQUIRE_COMMAND", "").strip()
        if not job or not command:
            return
        request_path = os.path.join(self.data_dir, "jobs", f"request-{job['id']}.json")
        self.store.export_worker_request(job, request_path)
        self.store.transition(job["id"], "searching", "Searching configured release sources")
        try:
            completed = subprocess.run(
                [*shlex.split(command), request_path],
                capture_output=True,
                text=True,
                timeout=900,
                check=False,
            )
            last_line = (completed.stdout.strip().splitlines() or [""])[-1]
            try:
                result = json.loads(last_line)
            except json.JSONDecodeError:
                result = {"ok": False, "detail": completed.stderr.strip() or last_line or "Acquisition failed"}
            if completed.returncode == 0 and result.get("ok"):
                self.store.transition(job["id"], "library_pending", result.get("detail", "Added to debrid"))
            else:
                self.store.transition(job["id"], "needs_attention", result.get("detail", "Acquisition failed"))
        except subprocess.TimeoutExpired:
            self.store.transition(job["id"], "needs_attention", "Acquisition timed out")

    def verify_library_handoffs(self):
        """Promote requests once their canonical library entry is visible."""
        roots = {
            "movie": os.environ.get("ORBIT_MOVIES_DIR", "/downloads/vortexo/Movies"),
            "show": os.environ.get("ORBIT_TV_DIR", "/downloads/vortexo/TV"),
        }
        pending = [item for item in self.store.list_requests(500) if item["status"] == "library_pending"]
        for item in pending:
            root = roots[item["media_type"]]
            try:
                names = os.listdir(root)
            except OSError:
                continue
            tmdb_marker = f"{{tmdb-{item['tmdb_id']}}}" if item.get("tmdb_id") else ""
            title_key = re.sub(r"[^a-z0-9]+", "", item["title"].lower())
            found = any(
                (tmdb_marker and tmdb_marker in name)
                or (title_key and re.sub(r"[^a-z0-9]+", "", name.lower()).startswith(title_key))
                for name in names
            )
            if found:
                self.store.transition(item["id"], "ready", "Library link created; Plex scan requested")

    def sync_list(self, source_id: int) -> dict:
        source = self.store.get_list_source(source_id)
        if not source:
            raise IntegrationError("Automatic list not found")
        settings = self.store.get_settings(reveal_secrets=True)
        try:
            items = fetch_list(source, settings)
            added = 0
            for item in items:
                item["profile"] = source["profile"]
                _, created = self.store.add_request(item, source=source["kind"], source_ref=str(source["id"]))
                added += int(created)
            self.store.complete_list_sync(source_id)
            return {"found": len(items), "added": added}
        except IntegrationError as error:
            self.store.complete_list_sync(source_id, str(error))
            raise

    def sync_all_lists(self):
        for source in self.store.list_sources():
            if source["enabled"]:
                try:
                    self.sync_list(source["id"])
                except IntegrationError:
                    pass
