"""SQLite-backed state for Orbit requests, list imports, and settings."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._migrate()

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _migrate(self):
        with self._lock, self.connection() as db:
            db.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    secret INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    media_key TEXT NOT NULL,
                    media_type TEXT NOT NULL CHECK(media_type IN ('movie', 'show')),
                    title TEXT NOT NULL,
                    year INTEGER,
                    tmdb_id INTEGER,
                    imdb_id TEXT,
                    poster_path TEXT,
                    overview TEXT,
                    source TEXT NOT NULL DEFAULT 'manual',
                    source_ref TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'queued',
                    status_detail TEXT NOT NULL DEFAULT '',
                    profile TEXT NOT NULL DEFAULT 'best',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(media_key, source, source_ref)
                );
                CREATE TABLE IF NOT EXISTS request_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
                    state TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS list_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('mdblist', 'trakt')),
                    url TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    mode TEXT NOT NULL DEFAULT 'import_only',
                    profile TEXT NOT NULL DEFAULT 'best',
                    max_items INTEGER NOT NULL DEFAULT 100,
                    last_sync_at TEXT,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(kind, url)
                );
                """
            )

    def get_settings(self, reveal_secrets: bool = False) -> dict:
        with self.connection() as db:
            rows = db.execute("SELECT key, value, secret FROM settings").fetchall()
        result = {}
        for row in rows:
            value = row["value"]
            if row["secret"] and value and not reveal_secrets:
                value = "••••••••"
            result[row["key"]] = value
        return result

    def set_settings(self, values: dict, secret_keys: set[str]):
        now = utc_now()
        with self._lock, self.connection() as db:
            for key, value in values.items():
                if key in secret_keys and value == "••••••••":
                    continue
                db.execute(
                    """INSERT INTO settings(key, value, secret, updated_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                         secret=excluded.secret, updated_at=excluded.updated_at""",
                    (key, str(value), int(key in secret_keys), now),
                )

    def add_request(self, item: dict, source: str = "manual", source_ref: str = "") -> tuple[dict, bool]:
        media_type = "show" if item.get("media_type") in ("tv", "show") else "movie"
        tmdb_id = item.get("tmdb_id") or item.get("id")
        imdb_id = item.get("imdb_id") or ""
        media_key = f"tmdb:{media_type}:{tmdb_id}" if tmdb_id else f"imdb:{media_type}:{imdb_id}"
        now = utc_now()
        with self._lock, self.connection() as db:
            # One title should enter the acquisition pipeline once even when it
            # appears in several automatic lists or is also requested manually.
            existing = db.execute(
                "SELECT * FROM requests WHERE media_key=? ORDER BY id DESC LIMIT 1",
                (media_key,),
            ).fetchone()
            if existing:
                return dict(existing), False
            cursor = db.execute(
                """INSERT INTO requests
                   (media_key, media_type, title, year, tmdb_id, imdb_id,
                    poster_path, overview, source, source_ref, profile,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    media_key, media_type, item.get("title") or item.get("name") or "Unknown",
                    item.get("year"), tmdb_id, imdb_id,
                    item.get("poster_path") or "", item.get("overview") or "",
                    source, source_ref, item.get("profile") or "best", now, now,
                ),
            )
            request_id = cursor.lastrowid
            db.execute(
                "INSERT INTO request_events(request_id, state, detail, created_at) VALUES (?, 'queued', ?, ?)",
                (request_id, f"Added from {source}", now),
            )
            row = db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            return dict(row), True

    def list_requests(self, limit: int = 100) -> list[dict]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM requests ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),)
            ).fetchall()
        return [dict(row) for row in rows]

    def next_queued(self) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT * FROM requests WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
        return dict(row) if row else None

    def transition(self, request_id: int, state: str, detail: str = ""):
        now = utc_now()
        with self._lock, self.connection() as db:
            db.execute(
                "UPDATE requests SET status=?, status_detail=?, updated_at=? WHERE id=?",
                (state, detail, now, request_id),
            )
            db.execute(
                "INSERT INTO request_events(request_id, state, detail, created_at) VALUES (?, ?, ?, ?)",
                (request_id, state, detail, now),
            )

    def events(self, request_id: int) -> list[dict]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT state, detail, created_at FROM request_events WHERE request_id=? ORDER BY id",
                (request_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_list_source(self, data: dict) -> dict:
        now = utc_now()
        with self._lock, self.connection() as db:
            cursor = db.execute(
                """INSERT INTO list_sources(name, kind, url, enabled, mode, profile, max_items, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    data["name"], data.get("kind", "mdblist"), data["url"],
                    int(data.get("enabled", True)), data.get("mode", "import_only"),
                    data.get("profile", "best"), max(1, min(int(data.get("max_items", 100)), 1000)), now,
                ),
            )
            row = db.execute("SELECT * FROM list_sources WHERE id=?", (cursor.lastrowid,)).fetchone()
        return dict(row)

    def list_sources(self) -> list[dict]:
        with self.connection() as db:
            rows = db.execute("SELECT * FROM list_sources ORDER BY id DESC").fetchall()
        return [dict(row) for row in rows]

    def get_list_source(self, source_id: int) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT * FROM list_sources WHERE id=?", (source_id,)).fetchone()
        return dict(row) if row else None

    def complete_list_sync(self, source_id: int, error: str = ""):
        with self._lock, self.connection() as db:
            db.execute(
                "UPDATE list_sources SET last_sync_at=?, last_error=? WHERE id=?",
                (utc_now(), error, source_id),
            )

    def dashboard(self) -> dict:
        with self.connection() as db:
            counts = {
                row["status"]: row["count"]
                for row in db.execute("SELECT status, COUNT(*) AS count FROM requests GROUP BY status")
            }
            sources = db.execute("SELECT COUNT(*) FROM list_sources WHERE enabled=1").fetchone()[0]
        return {"requests": counts, "active_lists": sources}

    def export_worker_request(self, request: dict, path: str):
        """Atomically expose one request to the acquisition worker."""
        payload = {key: request.get(key) for key in (
            "id", "media_type", "title", "year", "tmdb_id", "imdb_id", "profile"
        )}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        os.replace(temporary, path)
