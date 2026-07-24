"""Durable, credential-free JSON snapshots of Orbit's Plex inventory."""

from __future__ import annotations

import json
import os


def _source(path: str, available: bool) -> dict:
    target = ""
    if path and os.path.islink(path):
        try:
            target = os.readlink(path)
        except OSError:
            target = ""
    return {
        "path": path,
        "available": bool(available and path and os.path.exists(path)),
        "symlink_target": target,
    }


def _sources(item: dict) -> list[dict]:
    sources = []
    seen = set()

    def add(version: dict):
        path = str(version.get("file") or "")
        if not path or path in seen:
            return
        seen.add(path)
        sources.append(_source(path, version.get("available", True)))

    for version in item.get("versions") or []:
        add(version)
    for season in item.get("seasons") or []:
        for episode in season.get("episodes") or []:
            for version in episode.get("versions") or []:
                add(version)
    return sources


def _write_if_changed(path: str, payload: dict) -> bool:
    body = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    try:
        with open(path, "r", encoding="utf-8") as handle:
            if handle.read() == body:
                return False
    except OSError:
        pass
    temporary = path + ".orbit.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(body)
    os.replace(temporary, path)
    return True


def write_library_manifests(data_dir: str, items: list[dict]) -> dict:
    """Write one JSON file per title plus an index, without expiring URLs."""
    root = os.path.join(data_dir, "manifests")
    os.makedirs(root, exist_ok=True)
    index_items = []
    written = 0
    for item in items:
        media_type = "show" if item.get("media_type") == "show" else "movie"
        directory = os.path.join(root, media_type)
        os.makedirs(directory, exist_ok=True)
        filename = f"{item['section_id']}-{item['plex_rating_key']}.json"
        relative_path = f"{media_type}/{filename}"
        payload = {
            "schema": "orbit.media.v1",
            "identity": {
                "media_type": media_type,
                "title": item.get("title") or "Unknown",
                "year": item.get("year"),
                "tmdb_id": item.get("tmdb_id"),
                "imdb_id": item.get("imdb_id") or "",
            },
            "plex": {
                "rating_key": str(item["plex_rating_key"]),
                "section_id": str(item["section_id"]),
                "quality": item.get("quality") or "Quality unavailable",
                "episode_count": int(item.get("episode_count") or 0),
                "versions": item.get("versions") or [],
                "seasons": item.get("seasons") or [],
            },
            "playback": {
                "kind": "debrid-mount",
                "sources": _sources(item),
                # Provider URLs expire and may contain credentials. Orbit keeps
                # the stable mount path here and resolves the remote URL live.
                "stream_url": None,
                "stream_url_policy": "resolve-live-from-mounted-source",
                "implementation": (
                    "Plex opens the stable video path; Orbit's rclone mount "
                    "resolves and refreshes the provider URL at read time."
                ),
            },
        }
        written += int(_write_if_changed(os.path.join(root, relative_path), payload))
        index_items.append({
            "media_type": media_type,
            "title": item.get("title") or "Unknown",
            "plex_rating_key": str(item["plex_rating_key"]),
            "manifest": relative_path,
        })
    index = {
        "schema": "orbit.library-index.v1",
        "count": len(index_items),
        "items": index_items,
    }
    written += int(_write_if_changed(os.path.join(root, "index.json"), index))
    return {"count": len(index_items), "written": written, "path": root}
