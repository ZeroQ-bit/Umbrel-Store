from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager


class Store:
    """Small persistent store for settings, streams, progress, and acquisition jobs."""

    def __init__(self, data_dir: str):
        os.umask(0o077)
        self.data_dir = os.path.abspath(data_dir)
        os.makedirs(self.data_dir, mode=0o700, exist_ok=True)
        os.chmod(self.data_dir, 0o700)
        self.path = os.path.join(self.data_dir, "vortexo.db")
        self._lock = threading.RLock()
        self._initialise()

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialise(self):
        with self._lock, self.connection() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS streams (
                    id TEXT PRIMARY KEY,
                    discover_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS play_sessions (
                    id TEXT PRIMARY KEY,
                    discover_id TEXT NOT NULL,
                    stream_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS progress (
                    discover_id TEXT PRIMARY KEY,
                    position_ms INTEGER NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    completed INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS library_jobs (
                    id TEXT PRIMARY KEY,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    discover_id TEXT NOT NULL,
                    stream_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    plex_rating_key TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS library_job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES library_jobs(id)
                );
                CREATE INDEX IF NOT EXISTS library_job_events_job
                    ON library_job_events(job_id, id);
                CREATE TABLE IF NOT EXISTS watchlist_items (
                    identity TEXT PRIMARY KEY,
                    discover_id TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    job_id TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_retry_at INTEGER NOT NULL DEFAULT 0,
                    last_seen_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS watchlist_items_job
                    ON watchlist_items(job_id);
                CREATE TABLE IF NOT EXISTS watchlist_sync (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    started_at INTEGER NOT NULL,
                    completed_at INTEGER NOT NULL,
                    found INTEGER NOT NULL DEFAULT 0,
                    queued INTEGER NOT NULL DEFAULT 0,
                    skipped_existing INTEGER NOT NULL DEFAULT 0,
                    skipped_active INTEGER NOT NULL DEFAULT 0,
                    unavailable INTEGER NOT NULL DEFAULT 0
                );
                """
            )
        os.chmod(self.path, 0o600)

    def settings(self) -> dict:
        with self.connection() as db:
            rows = db.execute("SELECT key, value FROM settings").fetchall()
        values = {}
        for row in rows:
            try:
                values[row["key"]] = json.loads(row["value"])
            except json.JSONDecodeError:
                values[row["key"]] = row["value"]
        return values

    def update_settings(self, values: dict):
        now = int(time.time())
        with self._lock, self.connection() as db:
            for key, value in values.items():
                db.execute(
                    """
                    INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                    """,
                    (key, json.dumps(value), now),
                )

    def save_streams(self, discover_id: str, streams: list[dict], ttl: int = 3600) -> list[dict]:
        now = int(time.time())
        expires_at = now + ttl
        public = []
        with self._lock, self.connection() as db:
            db.execute("DELETE FROM streams WHERE expires_at < ?", (now,))
            for stream in streams:
                stream_id = uuid.uuid4().hex
                payload = dict(stream)
                payload["id"] = stream_id
                db.execute(
                    "INSERT INTO streams(id, discover_id, payload, expires_at) VALUES (?, ?, ?, ?)",
                    (stream_id, discover_id, json.dumps(payload), expires_at),
                )
                public.append(self.public_stream(payload))
        return public

    @staticmethod
    def public_stream(payload: dict) -> dict:
        allowed = {
            "id", "title", "label", "quality", "cached", "hdr", "dynamic_range",
            "codec", "audio", "size_gb", "file_name", "source", "seeders",
            "can_play_now", "can_add", "season", "episode",
        }
        return {key: value for key, value in payload.items() if key in allowed}

    def stream(self, stream_id: str) -> dict | None:
        now = int(time.time())
        with self.connection() as db:
            row = db.execute(
                "SELECT payload FROM streams WHERE id=? AND expires_at>=?",
                (stream_id, now),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def create_play_session(
        self,
        discover_id: str,
        stream_id: str,
        payload: dict,
        ttl: int = 2 * 3600,
    ) -> str:
        session_id = uuid.uuid4().hex
        with self._lock, self.connection() as db:
            db.execute("DELETE FROM play_sessions WHERE expires_at < ?", (int(time.time()),))
            db.execute(
                """
                INSERT INTO play_sessions(id, discover_id, stream_id, payload, expires_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, discover_id, stream_id, json.dumps(payload), int(time.time()) + ttl),
            )
        return session_id

    def play_session(self, session_id: str) -> dict | None:
        now = int(time.time())
        with self.connection() as db:
            row = db.execute(
                "SELECT payload FROM play_sessions WHERE id=? AND expires_at>=?",
                (session_id, now),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def save_progress(
        self,
        discover_id: str,
        position_ms: int,
        duration_ms: int,
        completed: bool,
    ) -> dict:
        now = int(time.time())
        with self._lock, self.connection() as db:
            db.execute(
                """
                INSERT INTO progress(discover_id, position_ms, duration_ms, completed, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(discover_id) DO UPDATE SET
                    position_ms=excluded.position_ms,
                    duration_ms=excluded.duration_ms,
                    completed=MAX(progress.completed, excluded.completed),
                    updated_at=excluded.updated_at
                """,
                (discover_id, max(0, position_ms), max(0, duration_ms), int(completed), now),
            )
        return self.progress(discover_id) or {}

    def progress(self, discover_id: str) -> dict | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT discover_id, position_ms, duration_ms, completed, updated_at FROM progress WHERE discover_id=?",
                (discover_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "discover_id": row["discover_id"],
            "position_ms": row["position_ms"],
            "duration_ms": row["duration_ms"],
            "completed": bool(row["completed"]),
            "updated_at": row["updated_at"],
        }

    def create_or_get_job(
        self,
        dedupe_key: str,
        discover_id: str,
        stream_id: str,
        payload: dict,
    ) -> tuple[dict, bool]:
        now = int(time.time())
        job_id = uuid.uuid4().hex
        with self._lock, self.connection() as db:
            try:
                db.execute(
                    """
                    INSERT INTO library_jobs(
                        id, dedupe_key, discover_id, stream_id, status, detail,
                        payload, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        dedupe_key,
                        discover_id,
                        stream_id,
                        "selected",
                        "Release selected",
                        json.dumps(payload),
                        now,
                        now,
                    ),
                )
                db.execute(
                    """
                    INSERT INTO library_job_events(job_id, status, detail, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (job_id, "selected", "Release selected", now),
                )
                created = True
            except sqlite3.IntegrityError:
                row = db.execute(
                    "SELECT id FROM library_jobs WHERE dedupe_key=?",
                    (dedupe_key,),
                ).fetchone()
                job_id = row["id"]
                created = False
        return self.job(job_id) or {}, created

    def transition(
        self,
        job_id: str,
        status: str,
        detail: str,
        *,
        payload_updates: dict | None = None,
        plex_rating_key: str | None = None,
    ) -> dict | None:
        now = int(time.time())
        with self._lock, self.connection() as db:
            row = db.execute(
                "SELECT payload, plex_rating_key FROM library_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            if not row:
                return None
            payload = json.loads(row["payload"])
            if payload_updates:
                payload.update(payload_updates)
            db.execute(
                """
                UPDATE library_jobs
                SET status=?, detail=?, payload=?, plex_rating_key=?, updated_at=?
                WHERE id=?
                """,
                (
                    status,
                    detail,
                    json.dumps(payload),
                    plex_rating_key or row["plex_rating_key"],
                    now,
                    job_id,
                ),
            )
            db.execute(
                """
                INSERT INTO library_job_events(job_id, status, detail, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (job_id, status, detail, now),
            )
        return self.job(job_id)

    def job(self, job_id: str) -> dict | None:
        with self.connection() as db:
            row = db.execute(
                """
                SELECT id, discover_id, stream_id, status, detail, payload,
                       plex_rating_key, created_at, updated_at
                FROM library_jobs WHERE id=?
                """,
                (job_id,),
            ).fetchone()
        if not row:
            return None
        payload = json.loads(row["payload"])
        with self.connection() as db:
            event_rows = db.execute(
                """
                SELECT status, detail, created_at
                FROM library_job_events WHERE job_id=? ORDER BY id
                """,
                (job_id,),
            ).fetchall()
        return {
            "id": row["id"],
            "discover_id": row["discover_id"],
            "stream_id": row["stream_id"],
            "status": row["status"],
            "detail": row["detail"],
            "plex_rating_key": row["plex_rating_key"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "media": payload.get("media", {}),
            "history": [
                {
                    "status": event["status"],
                    "detail": event["detail"],
                    "created_at": event["created_at"],
                }
                for event in event_rows
            ],
        }

    def job_payload(self, job_id: str) -> dict | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT payload FROM library_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def resumable_jobs(self) -> list[dict]:
        with self.connection() as db:
            rows = db.execute(
                """
                SELECT id, status FROM library_jobs
                WHERE status NOT IN ('plex_confirmed', 'already_in_plex', 'failed')
                ORDER BY created_at
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def retry_job(self, job_id: str, detail: str = "Retrying selected release") -> dict | None:
        now = int(time.time())
        with self._lock, self.connection() as db:
            row = db.execute(
                "SELECT status FROM library_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            if not row or row["status"] != "failed":
                return None
            db.execute(
                """
                UPDATE library_jobs
                SET status='selected', detail=?, updated_at=?
                WHERE id=?
                """,
                (detail, now, job_id),
            )
            db.execute(
                """
                INSERT INTO library_job_events(job_id, status, detail, created_at)
                VALUES (?, 'selected', ?, ?)
                """,
                (job_id, detail, now),
            )
        return self.job(job_id)

    def upsert_watchlist_item(self, identity: str, media: dict) -> dict:
        now = int(time.time())
        with self._lock, self.connection() as db:
            db.execute(
                """
                INSERT INTO watchlist_items(
                    identity, discover_id, media_type, title, payload, status,
                    detail, last_seen_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'detected', 'Detected in Plex Watchlist', ?, ?)
                ON CONFLICT(identity) DO UPDATE SET
                    discover_id=excluded.discover_id,
                    media_type=excluded.media_type,
                    title=excluded.title,
                    payload=excluded.payload,
                    last_seen_at=excluded.last_seen_at
                """,
                (
                    identity,
                    str(media.get("discover_id") or ""),
                    str(media.get("type") or ""),
                    str(media.get("title") or "Unknown"),
                    json.dumps(media),
                    now,
                    now,
                ),
            )
        return self.watchlist_item(identity) or {}

    def update_watchlist_item(
        self,
        identity: str,
        status: str,
        detail: str,
        *,
        job_id: str | None = None,
        next_retry_at: int = 0,
        increment_attempts: bool = False,
    ) -> dict | None:
        now = int(time.time())
        with self._lock, self.connection() as db:
            db.execute(
                """
                UPDATE watchlist_items
                SET status=?, detail=?, job_id=COALESCE(?, job_id),
                    next_retry_at=?, attempts=attempts+?, updated_at=?
                WHERE identity=?
                """,
                (
                    status,
                    detail,
                    job_id,
                    max(0, int(next_retry_at)),
                    int(increment_attempts),
                    now,
                    identity,
                ),
            )
        return self.watchlist_item(identity)

    def update_watchlist_for_job(
        self,
        job_id: str,
        status: str,
        detail: str,
        *,
        next_retry_at: int = 0,
    ):
        now = int(time.time())
        with self._lock, self.connection() as db:
            db.execute(
                """
                UPDATE watchlist_items
                SET status=?, detail=?, next_retry_at=?, updated_at=?
                WHERE job_id=?
                """,
                (status, detail, max(0, int(next_retry_at)), now, job_id),
            )

    def watchlist_item(self, identity: str) -> dict | None:
        with self.connection() as db:
            row = db.execute(
                """
                SELECT identity, discover_id, media_type, title, status, detail,
                       job_id, attempts, next_retry_at, last_seen_at, updated_at
                FROM watchlist_items WHERE identity=?
                """,
                (identity,),
            ).fetchone()
        return dict(row) if row else None

    def watchlist_items(self, limit: int = 100) -> list[dict]:
        with self.connection() as db:
            rows = db.execute(
                """
                SELECT identity, discover_id, media_type, title, status, detail,
                       job_id, attempts, next_retry_at, last_seen_at, updated_at
                FROM watchlist_items
                ORDER BY last_seen_at DESC, title COLLATE NOCASE
                LIMIT ?
                """,
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def begin_watchlist_sync(self):
        now = int(time.time())
        with self._lock, self.connection() as db:
            db.execute(
                """
                INSERT INTO watchlist_sync(
                    id, status, detail, started_at, completed_at,
                    found, queued, skipped_existing, skipped_active, unavailable
                ) VALUES (1, 'running', 'Reading Plex Watchlist', ?, 0, 0, 0, 0, 0, 0)
                ON CONFLICT(id) DO UPDATE SET
                    status='running', detail='Reading Plex Watchlist',
                    started_at=excluded.started_at
                """,
                (now,),
            )

    def complete_watchlist_sync(self, status: str, detail: str, counts: dict | None = None):
        now = int(time.time())
        counts = counts or {}
        with self._lock, self.connection() as db:
            db.execute(
                """
                INSERT INTO watchlist_sync(
                    id, status, detail, started_at, completed_at,
                    found, queued, skipped_existing, skipped_active, unavailable
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status, detail=excluded.detail,
                    completed_at=excluded.completed_at,
                    found=excluded.found, queued=excluded.queued,
                    skipped_existing=excluded.skipped_existing,
                    skipped_active=excluded.skipped_active,
                    unavailable=excluded.unavailable
                """,
                (
                    status,
                    detail,
                    now,
                    now,
                    int(counts.get("found", 0)),
                    int(counts.get("queued", 0)),
                    int(counts.get("skipped_existing", 0)),
                    int(counts.get("skipped_active", 0)),
                    int(counts.get("unavailable", 0)),
                ),
            )

    def watchlist_status(self) -> dict:
        with self.connection() as db:
            row = db.execute(
                """
                SELECT status, detail, started_at, completed_at, found, queued,
                       skipped_existing, skipped_active, unavailable
                FROM watchlist_sync WHERE id=1
                """
            ).fetchone()
        if not row:
            return {
                "status": "never",
                "detail": "Plex Watchlist has not been synced",
                "started_at": 0,
                "completed_at": 0,
                "found": 0,
                "queued": 0,
                "skipped_existing": 0,
                "skipped_active": 0,
                "unavailable": 0,
            }
        return dict(row)
