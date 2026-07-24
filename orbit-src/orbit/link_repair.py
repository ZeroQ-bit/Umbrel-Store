"""Safe repair helpers for Plex library symlinks backed by TorBox."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
import uuid


VIDEO_EXTENSIONS = {
    ".avi", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg",
    ".mts", ".ts", ".webm", ".wmv",
}
TORBOX_MYLIST = "https://api.torbox.app/v1/api/torrents/mylist"


def _is_video(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in VIDEO_EXTENSIONS


def _normalise_title(value: str) -> str:
    value = re.sub(r"\{(?:tmdb|tvdb)-[^{}]+\}", " ", value or "", flags=re.I)
    value = re.sub(r"\(\d{4}\)", " ", value)
    value = re.split(
        r"(?:\bS\d{1,2}(?:E\d{1,3})?\b|\b\d{1,2}x\d{1,3}\b|"
        r"\b(?:19|20)\d{2}\b|\b2160p\b|\b1080p\b|\b720p\b|\b4k\b|"
        r"\buhd\b|\bcomplete\b|\bseason\s*\d+\b)",
        value,
        maxsplit=1,
        flags=re.I,
    )[0]
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


def _episode_marker(value: str) -> tuple[int, int] | None:
    match = re.search(r"\bS(\d{1,2})E(\d{1,3})\b", value or "", re.I)
    if not match:
        match = re.search(r"\b(\d{1,2})x(\d{1,3})\b", value or "", re.I)
    return (int(match.group(1)), int(match.group(2))) if match else None


def _fetch_torrents(api_key: str, timeout: int = 30) -> list[dict]:
    if not api_key:
        return []
    request = urllib.request.Request(
        TORBOX_MYLIST,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "Orbit/link-repair",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, ValueError):
        return []
    data = body.get("data") if isinstance(body, dict) else None
    return data if isinstance(data, list) else []


def _completed_torrent(torrent: dict) -> bool:
    return bool(
        torrent.get("cached")
        or torrent.get("download_finished")
        or str(torrent.get("download_state") or "").lower()
        in {"cached", "completed", "downloaded", "finished"}
    )


def _source_files(source_root: str, torrent_name: str) -> list[str]:
    source = os.path.join(source_root, torrent_name)
    if os.path.isfile(source) and _is_video(source):
        return [source]
    if not os.path.isdir(source):
        return []
    videos = []
    try:
        for root, directories, files in os.walk(source):
            directories.sort()
            for filename in sorted(files):
                path = os.path.join(root, filename)
                if _is_video(path) and os.path.isfile(path):
                    videos.append(path)
    except OSError:
        return []
    return videos


def _match_torrents(folder_name: str, torrents: list[dict], media_kind: str) -> list[dict]:
    wanted = _normalise_title(folder_name)
    if not wanted:
        return []
    scored = []
    for torrent in torrents:
        name = str(torrent.get("name") or "")
        candidate = _normalise_title(name)
        if not candidate:
            continue
        torrent_kind = "show" if _episode_marker(name) or re.search(
            r"\bS\d{1,2}\b|\bseason\s*\d+\b", name, re.I
        ) else "movie"
        if torrent_kind != media_kind:
            continue
        if candidate == wanted:
            score = 10000 + len(wanted)
        elif candidate.startswith(wanted) or wanted.startswith(candidate):
            score = min(len(candidate), len(wanted))
        else:
            continue
        scored.append((score, torrent))
    return [item for _score, item in sorted(scored, key=lambda value: value[0], reverse=True)]


def _broken_symlinks(folder: str) -> list[str]:
    broken = []
    try:
        for root, _directories, files in os.walk(folder):
            for filename in files:
                path = os.path.join(root, filename)
                if os.path.islink(path) and not os.path.exists(path):
                    broken.append(path)
    except OSError:
        return []
    return broken


def _atomic_retarget(link_path: str, target: str) -> bool:
    """Replace only an existing broken symlink, never a regular file."""
    if not os.path.islink(link_path) or os.path.exists(link_path):
        return False
    if not os.path.isfile(target):
        return False
    temporary = f"{link_path}.orbit-repair-{uuid.uuid4().hex}"
    try:
        os.symlink(target, temporary)
        if not os.path.exists(temporary):
            return False
        os.replace(temporary, link_path)
        return os.path.exists(link_path)
    except OSError:
        return False
    finally:
        try:
            if os.path.lexists(temporary):
                os.unlink(temporary)
        except OSError:
            pass


def repair_broken_symlinks(
    api_key: str,
    mount_dir: str,
    library_dirs: dict[str, str],
    max_repairs: int = 100,
) -> dict:
    """Retarget broken links to a matching completed TorBox torrent.

    Existing working links and regular files are never modified. A repair uses
    an atomic symlink replacement only after the new target resolves.
    """
    source_root = os.path.join(mount_dir, ".vortexo-source")
    if not os.path.isdir(source_root):
        return {
            "checked": 0,
            "broken": 0,
            "repaired": 0,
            "remaining": [],
            "error": "Debrid mount is not available",
        }
    torrents = [
        torrent for torrent in _fetch_torrents(api_key)
        if torrent.get("name") and _completed_torrent(torrent)
    ]
    checked = 0
    repaired = 0
    remaining = []
    for media_kind, library_root in library_dirs.items():
        if not library_root or not os.path.isdir(library_root):
            continue
        try:
            folders = sorted(os.listdir(library_root))
        except OSError:
            continue
        for folder_name in folders:
            folder = os.path.join(library_root, folder_name)
            if not os.path.isdir(folder):
                continue
            broken = _broken_symlinks(folder)
            checked += len(broken)
            if not broken:
                continue
            matches = _match_torrents(folder_name, torrents, media_kind)
            candidate_files = []
            for torrent in matches:
                candidate_files = _source_files(source_root, str(torrent["name"]))
                if candidate_files:
                    break
            for link_path in broken:
                if repaired >= max(1, max_repairs):
                    remaining.append(link_path)
                    continue
                target = None
                if media_kind == "show":
                    marker = _episode_marker(os.path.basename(link_path))
                    if marker is None:
                        marker = _episode_marker(link_path)
                    if marker is not None:
                        target = next(
                            (
                                path for path in candidate_files
                                if _episode_marker(os.path.basename(path)) == marker
                            ),
                            None,
                        )
                elif candidate_files:
                    target = max(
                        candidate_files,
                        key=lambda path: os.path.getsize(path)
                        if os.path.isfile(path) else -1,
                    )
                if target and _atomic_retarget(link_path, target):
                    repaired += 1
                else:
                    remaining.append(link_path)
    return {
        "checked": checked,
        "broken": checked,
        "repaired": repaired,
        "remaining": remaining,
        "error": "",
    }
