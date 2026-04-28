#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml
from plexapi.myplex import MyPlexAccount
from plexapi.server import PlexServer

CONFIG_DIR = Path(os.environ.get("PTS_CONFIG_DIR", "/app/config"))
WEB_DIR = Path(__file__).resolve().parent
ENV_FILE = CONFIG_DIR / ".env"
SERVERS_FILE = CONFIG_DIR / "servers.yml"
CONFIG_FILE = CONFIG_DIR / "config.yml"
PYTRAKT_FILE = CONFIG_DIR / ".pytrakt.json"
LOG_FILE = CONFIG_DIR / "plextraktsync.log"
JOB_LOG_FILE = CONFIG_DIR / "web-sync.log"

TRAKT_API = "https://api.trakt.tv"
DISCOVERIES: dict[str, dict] = {}
TRAKT_SESSIONS: dict[str, dict] = {}
JOB_LOCK = threading.Lock()
JOB: dict = {"running": False, "returncode": None, "started_at": None, "finished_at": None, "message": "Idle"}

SAFE_CONFIG = {
    "sync": {
        "plex_to_trakt": {
            "collection": False,
            "clear_collected": False,
            "ratings": False,
            "watched_status": False,
            "watchlist": False,
        },
        "trakt_to_plex": {
            "liked_lists": False,
            "ratings": False,
            "watched_status": False,
            "watchlist": False,
            "watchlist_as_playlist": False,
            "playback_status": True,
        },
    }
}

ENV_KEYS = ["PLEX_USERNAME", "TRAKT_USERNAME", "PLEX_SERVER", "PLEX_OWNER_TOKEN", "PLEX_ACCOUNT_TOKEN"]


def ensure_files() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.touch(exist_ok=True)
    JOB_LOG_FILE.touch(exist_ok=True)
    if not CONFIG_FILE.exists():
        write_yaml(CONFIG_FILE, SAFE_CONFIG)
    if not ENV_FILE.exists():
        write_env({key: "" for key in ENV_KEYS})


def read_json_body(handler) -> dict:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0:
        return {}
    raw = handler.rfile.read(length).decode("utf-8")
    return json.loads(raw or "{}")


def send_json(handler, payload: dict, status: int = 200) -> None:
    body = json.dumps(payload, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def fail(handler, message: str, status: int = 400) -> None:
    send_json(handler, {"ok": False, "error": message}, status)


def read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data or {}


def write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def read_env() -> dict[str, str]:
    env = {key: "" for key in ENV_KEYS}
    if not ENV_FILE.exists():
        return env
    for line in ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key] = value
    return env


def write_env(env: dict[str, str]) -> None:
    merged = read_env()
    merged.update({key: str(value or "") for key, value in env.items()})
    lines = ["# This is .env file for PlexTraktSync"]
    for key in ENV_KEYS:
        lines.append(f"{key}={merged.get(key, '')}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def append_job_log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with JOB_LOG_FILE.open("a", encoding="utf-8") as fp:
        fp.write(f"[{timestamp}] {message}\n")


def tail(path: Path, max_chars: int = 20000) -> str:
    if not path.exists():
        return ""
    data = path.read_bytes()
    return data[-max_chars:].decode("utf-8", errors="replace")


def redact(value: str | None) -> str:
    if not value:
        return ""
    value = str(value)
    if len(value) <= 8:
        return "********"
    return f"{value[:4]}...{value[-4:]}"


def clean_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        raise ValueError("Plex URL is required")
    if not re.match(r"^https?://", url):
        raise ValueError("Plex URL must start with http:// or https://")
    return url


def save_server(name: str, url: str, token: str, username: str = "", machine_id: str | None = None) -> dict:
    name = (name or "Plex").strip() or "Plex"
    url = clean_url(url)
    token = (token or "").strip()
    if not token:
        raise ValueError("Plex token is required")

    plex = PlexServer(baseurl=url, token=token, timeout=20)
    server_name = name or plex.friendlyName or "Plex"
    machine_id = machine_id or getattr(plex, "machineIdentifier", None)

    servers = read_yaml(SERVERS_FILE).get("servers", {})
    servers[server_name] = {
        "token": token,
        "urls": unique_urls([url, "http://host.docker.internal:32400"]),
        "id": machine_id,
        "config": None,
    }
    write_yaml(SERVERS_FILE, {"servers": servers})
    write_env({"PLEX_SERVER": server_name, "PLEX_USERNAME": username or "", "PLEX_OWNER_TOKEN": "", "PLEX_ACCOUNT_TOKEN": ""})
    return {"name": server_name, "friendlyName": plex.friendlyName, "machineIdentifier": machine_id}


def unique_urls(urls: list[str]) -> list[str]:
    seen = set()
    result = []
    for url in urls:
        if not url:
            continue
        url = str(url).strip().rstrip("/")
        if url and url not in seen:
            seen.add(url)
            result.append(url)
    return result


def save_config_flags(flags: dict) -> dict:
    config = read_yaml(CONFIG_FILE)
    sync = config.setdefault("sync", {})
    p2t = sync.setdefault("plex_to_trakt", {})
    t2p = sync.setdefault("trakt_to_plex", {})

    bools = {
        "plex_to_trakt.collection": (p2t, "collection"),
        "plex_to_trakt.clear_collected": (p2t, "clear_collected"),
        "plex_to_trakt.ratings": (p2t, "ratings"),
        "plex_to_trakt.watched_status": (p2t, "watched_status"),
        "plex_to_trakt.watchlist": (p2t, "watchlist"),
        "trakt_to_plex.liked_lists": (t2p, "liked_lists"),
        "trakt_to_plex.ratings": (t2p, "ratings"),
        "trakt_to_plex.watched_status": (t2p, "watched_status"),
        "trakt_to_plex.watchlist": (t2p, "watchlist"),
        "trakt_to_plex.watchlist_as_playlist": (t2p, "watchlist_as_playlist"),
        "trakt_to_plex.playback_status": (t2p, "playback_status"),
    }
    for dotted, (section, key) in bools.items():
        if dotted in flags:
            section[key] = bool(flags[dotted])

    write_yaml(CONFIG_FILE, config)
    return config


def status_payload() -> dict:
    ensure_files()
    env = read_env()
    servers_config = read_yaml(SERVERS_FILE).get("servers", {}) if SERVERS_FILE.exists() else {}
    trakt_config = {}
    if PYTRAKT_FILE.exists():
        try:
            trakt_config = json.loads(PYTRAKT_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            trakt_config = {}
    config = read_yaml(CONFIG_FILE)

    servers = []
    for name, server in servers_config.items():
        servers.append(
            {
                "name": name,
                "id": server.get("id"),
                "urls": server.get("urls") or [],
                "token": redact(server.get("token")),
            }
        )

    return {
        "ok": True,
        "configured": {
            "plex": bool(servers),
            "trakt": bool(trakt_config.get("OAUTH_TOKEN") and env.get("TRAKT_USERNAME")),
        },
        "env": {
            "PLEX_USERNAME": env.get("PLEX_USERNAME", ""),
            "TRAKT_USERNAME": env.get("TRAKT_USERNAME", ""),
            "PLEX_SERVER": env.get("PLEX_SERVER", ""),
        },
        "servers": servers,
        "trakt": {
            "client_id": redact(trakt_config.get("CLIENT_ID")),
            "username": env.get("TRAKT_USERNAME", ""),
            "expires_at": trakt_config.get("OAUTH_EXPIRES_AT"),
        },
        "sync": (config.get("sync") or {}),
        "job": JOB.copy(),
        "logs": {
            "job": tail(JOB_LOG_FILE, 12000),
            "plextraktsync": tail(LOG_FILE, 16000),
        },
    }


def require_ready_for_sync() -> None:
    env = read_env()
    servers = read_yaml(SERVERS_FILE).get("servers", {}) if SERVERS_FILE.exists() else {}
    if not servers:
        raise ValueError("Plex is not configured yet. Save a Plex server first.")
    if not PYTRAKT_FILE.exists():
        raise ValueError("Trakt is not configured yet. Complete Trakt device authorization first.")
    try:
        trakt_config = json.loads(PYTRAKT_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("Trakt auth file is invalid. Re-run Trakt device authorization.") from exc
    if not trakt_config.get("OAUTH_TOKEN") or not env.get("TRAKT_USERNAME"):
        raise ValueError("Trakt is not fully configured yet. Complete Trakt device authorization first.")


def trakt_headers(client_id: str, token: str | None = None) -> dict:
    headers = {
        "Content-Type": "application/json",
        "trakt-api-version": "2",
        "trakt-api-key": client_id,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def start_sync(dry_run: bool = False) -> dict:
    try:
        require_ready_for_sync()
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    with JOB_LOCK:
        if JOB.get("running"):
            return {"ok": False, "error": "A sync is already running"}
        JOB.update({"running": True, "returncode": None, "started_at": time.time(), "finished_at": None, "message": "Starting sync"})

    def worker() -> None:
        cmd = ["plextraktsync", "--no-progressbar", "sync"]
        if dry_run:
            cmd.append("--dry-run")
        append_job_log("Starting: " + " ".join(cmd))
        try:
            env = os.environ.copy()
            env.update({"PTS_CONFIG_DIR": str(CONFIG_DIR), "PTS_CACHE_DIR": str(CONFIG_DIR), "PTS_LOG_DIR": str(CONFIG_DIR), "PYTHONUNBUFFERED": "1"})
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
            assert process.stdout is not None
            for line in process.stdout:
                append_job_log(line.rstrip())
            returncode = process.wait()
            with JOB_LOCK:
                JOB.update(
                    {
                        "running": False,
                        "returncode": returncode,
                        "finished_at": time.time(),
                        "message": "Sync completed" if returncode == 0 else f"Sync failed with code {returncode}",
                    }
                )
            append_job_log(JOB["message"])
        except Exception as exc:  # noqa: BLE001 - API should report the exact operational failure.
            with JOB_LOCK:
                JOB.update({"running": False, "returncode": -1, "finished_at": time.time(), "message": str(exc)})
            append_job_log(f"ERROR: {exc}")

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True, "job": JOB.copy()}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def log_message(self, fmt, *args):
        append_job_log("web: " + (fmt % args))

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/status":
            return send_json(self, status_payload())
        if parsed.path == "/api/logs":
            return send_json(self, {"ok": True, "job": tail(JOB_LOG_FILE), "plextraktsync": tail(LOG_FILE)})
        if parsed.path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            data = read_json_body(self)
            if parsed.path == "/api/plex/manual":
                saved = save_server(data.get("name") or "Plex", data.get("url") or "", data.get("token") or "", data.get("username") or "")
                return send_json(self, {"ok": True, "server": saved})

            if parsed.path == "/api/plex/discover":
                username = (data.get("username") or "").strip()
                password = data.get("password") or ""
                code = (data.get("code") or "").strip() or None
                if not username or not password:
                    return fail(self, "Plex username and password are required")
                account = MyPlexAccount(username=username, password=password, code=code, timeout=30)
                resources = account.resources()
                discovery_id = secrets.token_urlsafe(16)
                DISCOVERIES[discovery_id] = {"account": account, "resources": resources, "created_at": time.time()}
                servers = []
                for resource in resources:
                    connections = [getattr(conn, "uri", "") for conn in getattr(resource, "connections", [])]
                    servers.append(
                        {
                            "name": resource.name,
                            "owned": bool(getattr(resource, "owned", False)),
                            "product": getattr(resource, "product", ""),
                            "version": getattr(resource, "productVersion", ""),
                            "platform": getattr(resource, "platform", ""),
                            "connections": unique_urls(connections),
                        }
                    )
                return send_json(self, {"ok": True, "discovery_id": discovery_id, "username": account.username, "servers": servers})

            if parsed.path == "/api/plex/save-discovered":
                discovery = DISCOVERIES.get(data.get("discovery_id") or "")
                if not discovery:
                    return fail(self, "Plex discovery session expired. Run discovery again.")
                selected = data.get("server") or ""
                resources = discovery["resources"]
                resource = next((r for r in resources if r.name == selected), None)
                if resource is None:
                    return fail(self, "Selected Plex server was not found in the discovery session")
                plex = resource.connect()
                connections = [getattr(conn, "uri", "") for conn in getattr(resource, "connections", [])]
                urls = unique_urls(connections + ["http://host.docker.internal:32400"])
                server_name = resource.name or plex.friendlyName or "Plex"
                servers = read_yaml(SERVERS_FILE).get("servers", {})
                servers[server_name] = {
                    "token": resource.accessToken,
                    "urls": urls,
                    "id": getattr(plex, "machineIdentifier", None),
                    "config": None,
                }
                write_yaml(SERVERS_FILE, {"servers": servers})
                write_env({"PLEX_SERVER": server_name, "PLEX_USERNAME": discovery["account"].username, "PLEX_OWNER_TOKEN": "", "PLEX_ACCOUNT_TOKEN": getattr(discovery["account"], "_token", "")})
                return send_json(self, {"ok": True, "server": {"name": server_name, "friendlyName": plex.friendlyName}})

            if parsed.path == "/api/trakt/start":
                client_id = (data.get("client_id") or "").strip()
                client_secret = (data.get("client_secret") or "").strip()
                if not client_id or not client_secret:
                    return fail(self, "Trakt client ID and client secret are required")
                response = requests.post(f"{TRAKT_API}/oauth/device/code", json={"client_id": client_id}, headers={"Content-Type": "application/json"}, timeout=30)
                if response.status_code >= 400:
                    return fail(self, f"Trakt rejected the client ID: {response.text}", response.status_code)
                auth = response.json()
                session_id = secrets.token_urlsafe(16)
                TRAKT_SESSIONS[session_id] = {"client_id": client_id, "client_secret": client_secret, "auth": auth, "created_at": time.time()}
                return send_json(self, {"ok": True, "session_id": session_id, **auth})

            if parsed.path == "/api/trakt/poll":
                session = TRAKT_SESSIONS.get(data.get("session_id") or "")
                if not session:
                    return fail(self, "Trakt auth session expired. Start again.")
                auth = session["auth"]
                token_response = requests.post(
                    f"{TRAKT_API}/oauth/device/token",
                    json={"code": auth["device_code"], "client_id": session["client_id"], "client_secret": session["client_secret"]},
                    headers={"Content-Type": "application/json"},
                    timeout=30,
                )
                if token_response.status_code == 400:
                    return send_json(self, {"ok": True, "pending": True, "message": "Waiting for Trakt approval"})
                if token_response.status_code >= 400:
                    return fail(self, f"Trakt authentication failed: {token_response.text}", token_response.status_code)
                token = token_response.json()
                username = ""
                me_response = requests.get(f"{TRAKT_API}/users/me", headers=trakt_headers(session["client_id"], token.get("access_token")), timeout=30)
                if me_response.ok:
                    username = (me_response.json() or {}).get("username", "")
                if not username:
                    return fail(self, f"Trakt authenticated, but the username could not be read: {me_response.text}", 502)
                config = {
                    "APPLICATION_ID": None,
                    "CLIENT_ID": session["client_id"],
                    "CLIENT_SECRET": session["client_secret"],
                    "OAUTH_EXPIRES_AT": token.get("created_at", int(time.time())) + token.get("expires_in", 0),
                    "OAUTH_REFRESH": token.get("refresh_token"),
                    "OAUTH_TOKEN": token.get("access_token"),
                }
                PYTRAKT_FILE.write_text(json.dumps(config, indent=4), encoding="utf-8")
                write_env({"TRAKT_USERNAME": username})
                return send_json(self, {"ok": True, "username": username, "message": "Trakt authentication saved"})

            if parsed.path == "/api/config":
                config = save_config_flags(data.get("flags") or {})
                return send_json(self, {"ok": True, "sync": config.get("sync") or {}})

            if parsed.path == "/api/sync":
                result = start_sync(dry_run=bool(data.get("dry_run")))
                return send_json(self, result, 200 if result.get("ok") else 409)

            return fail(self, "Unknown API endpoint", 404)
        except Exception as exc:  # noqa: BLE001 - The UI needs a human-readable operational error.
            append_job_log(f"API error on {parsed.path}: {exc}")
            return fail(self, str(exc), 500)


if __name__ == "__main__":
    ensure_files()
    host = "0.0.0.0"
    port = int(os.environ.get("WEB_PORT", "3490"))
    append_job_log(f"Starting PlexTraktSync setup UI on {host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
