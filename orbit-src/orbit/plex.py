"""Plex library inventory and media-quality inspection."""

from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from .integrations import IntegrationError


PLEX_HEADERS = {
    "Accept": "application/xml",
    "User-Agent": "Orbit/0.2.0",
    "X-Plex-Product": "Orbit",
    "X-Plex-Client-Identifier": "zeroq-orbit",
}


def _plex_xml(base_url: str, token: str, path: str, params: dict | None = None) -> ET.Element:
    if not base_url or not token:
        raise IntegrationError("Add the Plex server URL and token in Settings")
    query = urllib.parse.urlencode(params or {})
    url = f"{base_url.rstrip('/')}{path}"
    if query:
        url = f"{url}?{query}"
    request = urllib.request.Request(
        url,
        headers={**PLEX_HEADERS, "X-Plex-Token": token},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return ET.fromstring(response.read())
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise IntegrationError("Plex rejected the saved token") from error
        raise IntegrationError(f"Plex returned HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, ET.ParseError) as error:
        raise IntegrationError(f"Could not read the Plex library: {error}") from error


def _metadata_ids(node: ET.Element) -> tuple[int | None, str]:
    values = [node.get("guid", "")]
    values.extend(guid.get("id", "") for guid in node.findall("./Guid"))
    tmdb_id = None
    imdb_id = ""
    for value in values:
        tmdb = re.search(r"(?:tmdb|themoviedb)(?:://|/)(\d+)", value, re.I)
        imdb = re.search(r"(tt\d{5,})", value, re.I)
        if tmdb and tmdb_id is None:
            tmdb_id = int(tmdb.group(1))
        if imdb and not imdb_id:
            imdb_id = imdb.group(1)
    return tmdb_id, imdb_id


def _normalise_resolution(media: ET.Element) -> str:
    raw = (media.get("videoResolution") or "").lower()
    stream = media.find(".//Stream[@streamType='1']")
    width = int(media.get("width") or (stream.get("width") if stream is not None else "") or 0)
    height = int(media.get("height") or (stream.get("height") if stream is not None else "") or 0)
    if raw in ("4k", "2160") or width >= 3000 or height >= 2000:
        return "4K"
    if raw in ("1080", "1080p") or height >= 1000:
        return "1080p"
    if raw in ("720", "720p") or height >= 700:
        return "720p"
    if raw in ("576", "576p"):
        return "576p"
    if raw in ("480", "480p", "sd") or height:
        return "SD"
    return "Unknown"


def _dynamic_range(media: ET.Element) -> str:
    stream = media.find(".//Stream[@streamType='1']")
    stream_detail = " ".join(
        stream.get(name, "") if stream is not None else ""
        for name in ("displayTitle", "extendedDisplayTitle", "colorTrc", "DOVIPresent")
    )
    value = f"{media.get('videoDynamicRange') or ''} {stream_detail}".upper()
    if "DOVI" in value or "DOLBY" in value:
        return "Dolby Vision"
    if "HDR" in value or "SMPTE2084" in value:
        return "HDR"
    return "SDR"


def _media_versions(node: ET.Element) -> list[dict]:
    versions = []
    for media in node.findall("./Media"):
        part = media.find("./Part")
        video_stream = media.find(".//Stream[@streamType='1']")
        audio_stream = media.find(".//Stream[@streamType='2']")
        try:
            size = int((part.get("size") if part is not None else "") or 0)
        except ValueError:
            size = 0
        try:
            bitrate = int(media.get("bitrate") or 0)
        except ValueError:
            bitrate = 0
        versions.append({
            "resolution": _normalise_resolution(media),
            "dynamic_range": _dynamic_range(media),
            "video_codec": (
                (video_stream.get("codec") if video_stream is not None else "")
                or media.get("videoCodec") or ""
            ).upper(),
            "audio_codec": (
                (audio_stream.get("codec") if audio_stream is not None else "")
                or media.get("audioCodec") or ""
            ).upper(),
            "container": (media.get("container") or "").upper(),
            "bitrate": bitrate,
            "size": size,
            "file": part.get("file", "") if part is not None else "",
        })
    return versions


def _quality_rank(resolution: str) -> int:
    return {"Unknown": 0, "SD": 1, "576p": 2, "720p": 3, "1080p": 4, "4K": 5}.get(
        resolution, 0
    )


def quality_summary(versions: list[dict]) -> str:
    labels = []
    for version in sorted(
        versions,
        key=lambda item: (
            _quality_rank(item.get("resolution", "Unknown")),
            item.get("dynamic_range", "SDR") != "SDR",
        ),
        reverse=True,
    ):
        resolution = version.get("resolution") or "Unknown"
        dynamic_range = version.get("dynamic_range") or "SDR"
        label = f"{resolution} {dynamic_range}" if dynamic_range != "SDR" else resolution
        if label not in labels:
            labels.append(label)
    return " · ".join(labels) if labels else "Quality unavailable"


def upgrade_available(versions: list[dict]) -> bool:
    best = max((_quality_rank(item.get("resolution", "Unknown")) for item in versions), default=0)
    return 0 < best < _quality_rank("1080p")


def _distinct_qualities(versions: list[dict]) -> list[dict]:
    result = []
    seen = set()
    for version in versions:
        key = tuple(
            version.get(name, "")
            for name in ("resolution", "dynamic_range", "video_codec", "audio_codec", "container")
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(version)
    return result


def _library_item(node: ET.Element, section_id: str, versions: list[dict] | None = None) -> dict:
    media_type = "show" if node.get("type") == "show" else "movie"
    tmdb_id, imdb_id = _metadata_ids(node)
    resolved_versions = versions if versions is not None else _media_versions(node)
    try:
        year = int(node.get("year") or 0) or None
    except ValueError:
        year = None
    return {
        "plex_rating_key": node.get("ratingKey") or "",
        "section_id": str(section_id),
        "media_type": media_type,
        "title": node.get("title") or "Unknown",
        "year": year,
        "tmdb_id": tmdb_id,
        "imdb_id": imdb_id,
        "thumb": node.get("thumb") or "",
        "quality": quality_summary(resolved_versions),
        "versions": resolved_versions,
        "upgrade_available": upgrade_available(resolved_versions),
        "episode_count": 0,
    }


def scan_plex_library(base_url: str, token: str, section_ids: list[str]) -> list[dict]:
    if not section_ids:
        raise IntegrationError("Add at least one Plex movie or TV section ID in Settings")
    items = []
    for section_id in section_ids:
        section = _plex_xml(
            base_url,
            token,
            f"/library/sections/{urllib.parse.quote(str(section_id))}/all",
            {"includeGuids": "1"},
        )
        section_nodes = list(section.findall("./Video")) + list(section.findall("./Directory"))
        shows = {
            node.get("ratingKey"): _library_item(node, str(section_id), [])
            for node in section_nodes
            if node.get("type") == "show" and node.get("ratingKey")
        }
        for node in section_nodes:
            if node.get("type") == "movie":
                items.append(_library_item(node, str(section_id)))
        if shows:
            episodes = _plex_xml(
                base_url,
                token,
                f"/library/sections/{urllib.parse.quote(str(section_id))}/all",
                {"type": "4", "includeGuids": "1"},
            )
            for episode in episodes.findall("./Video"):
                show = shows.get(episode.get("grandparentRatingKey"))
                if not show:
                    continue
                show["versions"].extend(_media_versions(episode))
                show["episode_count"] += 1
            for show in shows.values():
                show["versions"] = _distinct_qualities(show["versions"])
                show["quality"] = quality_summary(show["versions"])
                show["upgrade_available"] = upgrade_available(show["versions"])
                items.append(show)
    return items
