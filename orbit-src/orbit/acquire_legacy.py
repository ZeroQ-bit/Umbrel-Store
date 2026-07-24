"""Dispatch one Orbit request through the bundled plex_debrid engine.

This module runs in a short-lived subprocess so the legacy engine's global
settings cannot corrupt Orbit's long-running control plane.
"""

from __future__ import annotations

import builtins
import datetime
import json
import os
import sys
from types import SimpleNamespace


class OrbitWatchlist:
    autoremove = "none"

    @staticmethod
    def remove(*_args, **_kwargs):
        return None


def load_engine_settings(ui) -> None:
    """Apply legacy settings migrations without prompting a background worker."""
    original_input = builtins.input
    builtins.input = lambda *_args, **_kwargs: ""
    try:
        ui.load()
    finally:
        builtins.input = original_input


def main() -> int:
    if len(sys.argv) != 2:
        print(json.dumps({"ok": False, "detail": "missing request file"}))
        return 2
    request_path = sys.argv[1]
    with open(request_path, "r", encoding="utf-8") as handle:
        job = json.load(handle)

    engine_root = os.environ.get("PD_ROOT", "/app/plex_debrid")
    config_dir = os.environ.get("PD_CONFIG_DIR", "/config")
    sys.path.insert(0, engine_root)

    # The legacy engine initializes its package graph from ui. Importing
    # content first leaves content.services partially initialized.
    import ui  # type: ignore
    import content  # type: ignore
    from content.services import overseerr, plex, trakt  # type: ignore
    from ui.ui_print import set_log_dir  # type: ignore

    ui.config_dir = config_dir
    ui.service_mode = True
    set_log_dir(config_dir)
    load_engine_settings(ui)

    media = SimpleNamespace(
        id=job["id"],
        status=3,
        imdbId=job.get("imdb_id") or None,
        tmdbId=job.get("tmdb_id") or None,
        tvdbId=None,
    )
    root = SimpleNamespace(
        type="tv" if job["media_type"] == "show" else "movie",
        title=job["title"],
        updatedAt=datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        media=media,
    )
    item = overseerr.show(root) if job["media_type"] == "show" else overseerr.movie(root)

    matching_service = None
    if plex.users:
        matching_service = "content.services.plex"
    elif trakt.users:
        matching_service = "content.services.trakt"
    if not matching_service:
        print(json.dumps({"ok": False, "detail": "Connect Plex or Trakt before adding media"}))
        return 3

    item.match(matching_service)
    item.watchlist = OrbitWatchlist
    libraries = content.classes.library()
    if not libraries:
        print(json.dumps({"ok": False, "detail": "Configure a Plex or Trakt library service"}))
        return 4
    library = libraries[0]()
    if len(library) == 0:
        print(json.dumps({"ok": False, "detail": "The configured media library could not be read"}))
        return 5

    if job.get("source") == "series-monitor" and item.complete(library):
        print(json.dumps({
            "ok": True,
            "detail": "Series is caught up; future unaired episodes were ignored",
            "paths": [],
        }))
        return 0

    item.download(library=library)
    releases = getattr(item, "downloaded_releases", [])
    if not releases:
        print(json.dumps({"ok": False, "detail": "No suitable cached release was acquired"}))
        return 6
    print(json.dumps({"ok": True, "detail": "Acquired and handed to the library", "paths": releases}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
