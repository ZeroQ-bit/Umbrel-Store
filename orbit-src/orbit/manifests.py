"""Credential-free JSON views of Orbit's persisted media inventory."""

from __future__ import annotations

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


def build_media_manifest(item: dict) -> dict:
    """Project one stored media row as a Riven-style virtual JSON record."""
    media_type = "show" if item.get("media_type") == "show" else "movie"
    return {
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
            "stream_url": None,
            "stream_url_policy": "resolve-live-from-mounted-source",
            "implementation": (
                "Plex opens the stable video path; Orbit's rclone mount "
                "resolves and refreshes the provider URL at read time."
            ),
        },
    }
