"""SQLite persistence (WAL mode) with forward-only migrations.

Single-writer friendly: the worker is the only writer of jobs/tracks, the API
writes playlists and enqueues jobs. Connections are short-lived (opened per
operation) with a busy timeout so readers never wedge the writer.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from app.config import Settings
from app.logging import get_logger

log = get_logger(__name__)

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE videos (
            id            TEXT PRIMARY KEY,
            youtube_id    TEXT NOT NULL UNIQUE,
            source_url    TEXT NOT NULL,
            title         TEXT NOT NULL DEFAULT '',
            uploader      TEXT NOT NULL DEFAULT '',
            duration_s    REAL NOT NULL DEFAULT 0,
            thumbnail_path TEXT,
            needs_review  INTEGER NOT NULL DEFAULT 0,
            status        TEXT NOT NULL DEFAULT 'pending',
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        );
        CREATE TABLE tracks (
            id          TEXT PRIMARY KEY,
            video_id    TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            title       TEXT NOT NULL,
            track_no    INTEGER NOT NULL,
            track_total INTEGER NOT NULL,
            start_s     REAL NOT NULL,
            duration_s  REAL NOT NULL,
            file_path   TEXT NOT NULL,
            file_size   INTEGER NOT NULL DEFAULT 0,
            codec       TEXT NOT NULL DEFAULT '',
            sample_rate INTEGER NOT NULL DEFAULT 0,
            channels    INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL
        );
        CREATE INDEX idx_tracks_video ON tracks(video_id);
        CREATE TABLE playlists (
            id         TEXT PRIMARY KEY,
            name       TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE playlist_tracks (
            id          TEXT PRIMARY KEY,
            playlist_id TEXT NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
            track_id    TEXT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
            position    INTEGER NOT NULL,
            added_at    TEXT NOT NULL,
            UNIQUE(playlist_id, track_id)
        );
        CREATE INDEX idx_playlist_tracks_order ON playlist_tracks(playlist_id, position);
        CREATE TABLE jobs (
            id            TEXT PRIMARY KEY,
            youtube_id    TEXT NOT NULL,
            video_id      TEXT REFERENCES videos(id) ON DELETE SET NULL,
            source_url    TEXT NOT NULL,
            playlist_id   TEXT NULL,
            status        TEXT NOT NULL DEFAULT 'queued',
            stage         TEXT NOT NULL DEFAULT 'queued',
            progress      REAL NOT NULL DEFAULT 0.0,
            attempt       INTEGER NOT NULL DEFAULT 0,
            max_attempts  INTEGER NOT NULL DEFAULT 5,
            error_code    TEXT,
            error_message TEXT,
            next_retry_at TEXT,
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        );
        CREATE INDEX idx_jobs_claim ON jobs(status, next_retry_at, created_at);
        CREATE TABLE kv (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """,
    ),
]


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=10000;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


@contextmanager
def connect(settings: Settings) -> Iterator[sqlite3.Connection]:
    """Yield a fresh connection; commit on success, roll back on error."""
    conn = _connect(settings.db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(settings: Settings) -> None:
    """Apply any pending forward migrations. Idempotent and crash-safe."""
    settings.ensure_dirs()
    conn = _connect(settings.db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
        for version, sql in sorted(MIGRATIONS):
            if version in applied:
                continue
            log.info("applying migration", extra={"version": version})
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, utcnow_iso()),
            )
            conn.commit()
    finally:
        conn.close()
