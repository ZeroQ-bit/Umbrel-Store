#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


USER_AGENT = "zeroq-umbrel-store-updater/1.0"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Update Umbrel app manifests and pinned image digests from upstream sources."
    )
    parser.add_argument(
        "--config",
        default="scripts/store-update-config.json",
        help="Path to the updater config JSON file.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write changes back to disk. Without this flag, runs in dry-run mode.",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    config_path = repo_root / args.config
    config = json.loads(config_path.read_text())

    resolver = SourceResolver()
    changes: list[str] = []

    for app in config["apps"]:
        app_changes = process_app(repo_root, app, resolver, write=args.write)
        changes.extend(app_changes)

    if changes:
        mode = "Updated" if args.write else "Planned"
        print(f"{mode} {len(changes)} change(s):")
        for change in changes:
            print(f"- {change}")
    else:
        print("No updates needed.")

    return 0


class SourceResolver:
    def __init__(self) -> None:
        self.cache: dict[str, dict[str, Any]] = {}
        self.github_token = os.environ.get("GITHUB_TOKEN", "").strip()

    def resolve(self, source: dict[str, Any]) -> dict[str, Any]:
        key = json.dumps(source, sort_keys=True)
        if key in self.cache:
            return self.cache[key]

        source_type = source["type"]
        if source_type == "github_branch_head":
            data = self._resolve_github_branch_head(source)
        elif source_type == "github_latest_tag":
            data = self._resolve_github_latest_tag(source)
        elif source_type == "dockerhub_latest_tag":
            data = self._resolve_dockerhub_latest_tag(source)
        else:
            raise ValueError(f"Unsupported source type: {source_type}")

        self.cache[key] = data
        return data

    def _resolve_github_branch_head(self, source: dict[str, Any]) -> dict[str, Any]:
        repo = source["repo"]
        branch = source.get("branch", "main")
        url = f"https://api.github.com/repos/{repo}/commits/{urllib.parse.quote(branch, safe='')}"
        data = self._get_json(url, github_api=True)
        sha = data["sha"]
        sha7 = sha[:7]
        context = {"sha": sha, "sha7": sha7, "branch": branch}
        context["tag"] = source["tag_format"].format(**context)
        return context

    def _resolve_github_latest_tag(self, source: dict[str, Any]) -> dict[str, Any]:
        repo = source["repo"]
        tags = self._get_json(
            f"https://api.github.com/repos/{repo}/tags?per_page=100",
            github_api=True,
        )
        pattern = re.compile(source["tag_regex"])
        candidates: list[dict[str, Any]] = []
        for item in tags:
            tag = item["name"]
            match = pattern.match(tag)
            if not match:
                continue
            context = {"raw_tag": tag, "tag": tag, **match.groupdict()}
            candidates.append(context)

        if not candidates:
            raise RuntimeError(f"No matching tags found for {repo}")

        sort_mode = source.get("sort", "lexicographic")
        candidates.sort(key=lambda item: sort_key(sort_mode, item["tag"], item), reverse=True)
        return candidates[0]

    def _resolve_dockerhub_latest_tag(self, source: dict[str, Any]) -> dict[str, Any]:
        namespace, repo = split_repository(source["repository"])
        pattern = re.compile(source["tag_regex"])
        url = f"https://hub.docker.com/v2/namespaces/{namespace}/repositories/{repo}/tags?page_size=100"

        candidates: list[dict[str, Any]] = []
        while url:
            data = self._get_json(url)
            for item in data.get("results", []):
                tag = item["name"]
                match = pattern.match(tag)
                if not match:
                    continue
                context = {
                    "raw_tag": tag,
                    "tag": tag,
                    "digest": item.get("digest", ""),
                    **match.groupdict(),
                }
                candidates.append(context)
            url = data.get("next")

        if not candidates:
            raise RuntimeError(f"No matching Docker Hub tags found for {source['repository']}")

        sort_mode = source.get("sort", "lexicographic")
        candidates.sort(key=lambda item: sort_key(sort_mode, item["tag"], item), reverse=True)
        return candidates[0]

    def _get_json(self, url: str, github_api: bool = False) -> Any:
        headers = {
            "Accept": "application/vnd.github+json" if github_api else "application/json",
            "User-Agent": USER_AGENT,
        }
        if github_api and self.github_token:
            headers["Authorization"] = f"Bearer {self.github_token}"
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request) as response:
            return json.load(response)


def process_app(
    repo_root: Path,
    app: dict[str, Any],
    resolver: SourceResolver,
    *,
    write: bool,
) -> list[str]:
    changes: list[str] = []

    manifest_path = repo_root / app["manifest_path"]
    manifest_text = manifest_path.read_text()
    manifest_source = resolver.resolve(app["manifest_version_source"])
    desired_version = app["manifest_version_format"].format(**manifest_source)
    updated_manifest_text, version_changed = replace_manifest_version(manifest_text, desired_version)
    if version_changed:
        changes.append(f"{app['id']}: version -> {desired_version}")
        if write:
            manifest_path.write_text(updated_manifest_text)

    for image_update in app["images"]:
        source = resolver.resolve(image_update["source"])
        desired_tag = image_update.get("tag_format", "{tag}").format(**source)
        repository = image_update["repository"]
        desired_digest = source.get("digest") or resolve_image_digest(repository, desired_tag)

        compose_path = repo_root / image_update["compose_path"]
        compose_text = compose_path.read_text()
        updated_compose_text, image_changed = replace_image_reference(
            compose_text,
            repository=repository,
            tag=desired_tag,
            digest=desired_digest,
        )
        if image_changed:
            changes.append(
                f"{app['id']}: {compose_path.relative_to(repo_root)} -> {repository}:{desired_tag}@{desired_digest}"
            )
            if write:
                compose_path.write_text(updated_compose_text)

    return changes


def replace_manifest_version(text: str, new_version: str) -> tuple[str, bool]:
    pattern = re.compile(r'(^version:\s*")([^"]+)(")', re.MULTILINE)
    match = pattern.search(text)
    if not match:
        raise RuntimeError("Could not find manifest version field")
    current_version = match.group(2)
    if current_version == new_version:
        return text, False
    return pattern.sub(lambda m: f'{m.group(1)}{new_version}{m.group(3)}', text, count=1), True


def replace_image_reference(text: str, *, repository: str, tag: str, digest: str) -> tuple[str, bool]:
    pattern = re.compile(
        rf'(^\s*image:\s*{re.escape(repository)}:)([^@\s]+)(@sha256:[0-9a-f]{{64}})',
        re.MULTILINE,
    )
    match = pattern.search(text)
    if not match:
        raise RuntimeError(f"Could not find image reference for {repository}")

    current_ref = f"{repository}:{match.group(2)}{match.group(3)}"
    desired_ref = f"{repository}:{tag}@{digest}"
    if current_ref == desired_ref:
        return text, False

    replaced = pattern.sub(lambda m: f"{m.group(1)}{tag}@{digest}", text, count=1)
    return replaced, True


def resolve_image_digest(repository: str, tag: str) -> str:
    if repository.startswith("ghcr.io/"):
        return ghcr_digest(repository[len("ghcr.io/") :], tag)
    return dockerhub_digest(repository, tag)


def ghcr_digest(repo_path: str, tag: str) -> str:
    token_url = f"https://ghcr.io/token?service=ghcr.io&scope=repository:{repo_path}:pull"
    token_request = urllib.request.Request(token_url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(token_request) as response:
        token = json.load(response)["token"]

    manifest_url = f"https://ghcr.io/v2/{repo_path}/manifests/{tag}"
    request = urllib.request.Request(
        manifest_url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": ", ".join(
                [
                    "application/vnd.oci.image.index.v1+json",
                    "application/vnd.docker.distribution.manifest.list.v2+json",
                    "application/vnd.oci.image.manifest.v1+json",
                    "application/vnd.docker.distribution.manifest.v2+json",
                ]
            ),
            "User-Agent": USER_AGENT,
        },
        method="HEAD",
    )
    with urllib.request.urlopen(request) as response:
        digest = response.headers.get("Docker-Content-Digest")
        if not digest:
            raise RuntimeError(f"Missing digest for ghcr image {repo_path}:{tag}")
        return digest


def dockerhub_digest(repository: str, tag: str) -> str:
    namespace, repo = split_repository(repository)
    url = f"https://hub.docker.com/v2/namespaces/{namespace}/repositories/{repo}/tags/{urllib.parse.quote(tag, safe='')}"
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(request) as response:
        data = json.load(response)
    digest = data.get("digest")
    if not digest:
        raise RuntimeError(f"Missing digest for Docker Hub image {repository}:{tag}")
    return digest


def split_repository(repository: str) -> tuple[str, str]:
    parts = repository.split("/", 1)
    if len(parts) != 2:
        raise ValueError(f"Repository must be <namespace>/<name>, got: {repository}")
    return parts[0], parts[1]


def sort_key(mode: str, tag: str, context: dict[str, Any]) -> Any:
    if mode == "semver":
        return numeric_tuple(tag)
    if mode == "plex_lsio":
        version_core = context.get("version_core", "")
        ls_build = context.get("ls_build", "0")
        return (*numeric_tuple(version_core), int(ls_build))
    return tag


def numeric_tuple(text: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", text)
    if not numbers:
        return (0,)
    return tuple(int(number) for number in numbers)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"HTTP error {exc.code} while fetching {exc.url}\n{body}", file=sys.stderr)
        raise
