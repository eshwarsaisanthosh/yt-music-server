"""Durable job queue backed by the jobs table.

State machine: queued -> running -> done | failed.
A failed job with attempts remaining goes back to queued with
``next_retry_at`` set (exponential backoff + jitter); the worker's claim
query only picks up jobs whose retry time has arrived.

Idempotency: one video row per youtube_id (UNIQUE). Re-submitting the same
URL returns the existing job/video instead of queueing new work.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import datetime, timedelta, timezone

from app import db
from app.config import Settings
from app.core.ids import new_id
from app.logging import get_logger

log = get_logger(__name__)


def compute_retry_delay_s(attempt: int, base_s: float, max_s: float) -> float:
    """Exponential backoff with ±25% jitter. ``attempt`` is 1-based."""
    delay = min(base_s * (2 ** max(0, attempt - 1)), max_s)
    jitter = delay * random.uniform(-0.25, 0.25)
    return max(1.0, delay + jitter)


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def get_job(settings: Settings, job_id: str) -> dict | None:
    with db.connect(settings) as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_dict(row) if row else None


def get_video(settings: Settings, video_id: str) -> dict | None:
    with db.connect(settings) as conn:
        row = conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
        return _row_to_dict(row) if row else None


def get_video_by_youtube_id(settings: Settings, youtube_id: str) -> dict | None:
    with db.connect(settings) as conn:
        row = conn.execute("SELECT * FROM videos WHERE youtube_id = ?", (youtube_id,)).fetchone()
        return _row_to_dict(row) if row else None


def latest_job_for_video(settings: Settings, video_id: str) -> dict | None:
    with db.connect(settings) as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE video_id = ? ORDER BY created_at DESC LIMIT 1",
            (video_id,),
        ).fetchone()
        return _row_to_dict(row) if row else None


def enqueue_ingest(
    settings: Settings,
    youtube_id: str,
    source_url: str,
    playlist_id: str | None = None,
) -> tuple[dict, dict, bool]:
    """Create (or reuse) the video + job rows for a YouTube URL.

    Returns (job, video, existing). Idempotent: if a video row already exists
    for the youtube_id, no new work is queued.
    """
    now = db.utcnow_iso()
    with db.connect(settings) as conn:
        video = conn.execute(
            "SELECT * FROM videos WHERE youtube_id = ?", (youtube_id,)
        ).fetchone()
        if video is None:
            video_id = new_id("vid")
            conn.execute(
                """INSERT INTO videos (id, youtube_id, source_url, status, created_at, updated_at)
                   VALUES (?, ?, ?, 'pending', ?, ?)""",
                (video_id, youtube_id, source_url, now, now),
            )
            video = conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
        video_d = _row_to_dict(video)

        job_row = conn.execute(
            """SELECT * FROM jobs WHERE video_id = ? AND status IN ('queued','running')
               ORDER BY created_at DESC LIMIT 1""",
            (video_d["id"],),
        ).fetchone()
        if job_row is not None:
            return _row_to_dict(job_row), video_d, True

        if video_d["status"] == "ready":
            # Already ingested: surface the last job as the no-op result.
            last = conn.execute(
                "SELECT * FROM jobs WHERE video_id = ? ORDER BY created_at DESC LIMIT 1",
                (video_d["id"],),
            ).fetchone()
            if playlist_id:
                _add_video_tracks_to_playlist(conn, video_d["id"], playlist_id)
            return _row_to_dict(last), video_d, True

        job_id = new_id("job")
        conn.execute(
            """INSERT INTO jobs (id, youtube_id, video_id, source_url, playlist_id,
                                 status, stage, attempt, max_attempts, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'queued', 'queued', 0, ?, ?, ?)""",
            (
                job_id, youtube_id, video_d["id"], source_url, playlist_id,
                settings.job_max_attempts, now, now,
            ),
        )
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        log.info("job enqueued", extra={"job_id": job_id, "youtube_id": youtube_id})
        return _row_to_dict(job), video_d, False


def _add_video_tracks_to_playlist(
    conn: sqlite3.Connection, video_id: str, playlist_id: str
) -> None:
    tracks = conn.execute(
        "SELECT id FROM tracks WHERE video_id = ? ORDER BY track_no", (video_id,)
    ).fetchall()
    pos_row = conn.execute(
        "SELECT COALESCE(MAX(position), -1) FROM playlist_tracks WHERE playlist_id = ?",
        (playlist_id,),
    ).fetchone()
    pos = int(pos_row[0]) + 1
    now = db.utcnow_iso()
    for t in tracks:
        try:
            conn.execute(
                """INSERT INTO playlist_tracks (id, playlist_id, track_id, position, added_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (new_id("plt"), playlist_id, t["id"], pos, now),
            )
            pos += 1
        except sqlite3.IntegrityError:
            continue  # already in the playlist


def add_video_tracks_to_playlist(settings: Settings, video_id: str, playlist_id: str) -> None:
    with db.connect(settings) as conn:
        _add_video_tracks_to_playlist(conn, video_id, playlist_id)


def claim_next_job(settings: Settings) -> dict | None:
    """Atomically move one due queued job to running. Returns None if empty."""
    now = db.utcnow_iso()
    with db.connect(settings) as conn:
        row = conn.execute(
            """UPDATE jobs
               SET status = 'running', attempt = attempt + 1, updated_at = ?
               WHERE id = (
                   SELECT id FROM jobs
                   WHERE status = 'queued'
                     AND (next_retry_at IS NULL OR next_retry_at <= ?)
                   ORDER BY created_at ASC
                   LIMIT 1
               )
               RETURNING *""",
            (now, now),
        ).fetchone()
        if row:
            log.info(
                "job claimed",
                extra={"job_id": row["id"], "attempt": row["attempt"]},
            )
            return _row_to_dict(row)
        return None


def update_job(
    settings: Settings,
    job_id: str,
    *,
    stage: str | None = None,
    progress: float | None = None,
) -> None:
    updates: list[str] = ["updated_at = ?"]
    params: list = [db.utcnow_iso()]
    if stage is not None:
        updates.append("stage = ?")
        params.append(stage)
    if progress is not None:
        updates.append("progress = ?")
        params.append(max(0.0, min(1.0, progress)))
    params.append(job_id)
    with db.connect(settings) as conn:
        conn.execute(f"UPDATE jobs SET {', '.join(updates)} WHERE id = ?", params)


def complete_job(settings: Settings, job_id: str) -> None:
    with db.connect(settings) as conn:
        conn.execute(
            """UPDATE jobs SET status = 'done', stage = 'done', progress = 1.0,
                      error_code = NULL, error_message = NULL, updated_at = ?
               WHERE id = ?""",
            (db.utcnow_iso(), job_id),
        )
    log.info("job done", extra={"job_id": job_id})


def fail_job(
    settings: Settings,
    job_id: str,
    error_code: str,
    error_message: str,
    retryable: bool,
) -> dict:
    """Mark a job failed; requeue with backoff when attempts remain."""
    job = get_job(settings, job_id)
    assert job is not None
    now = datetime.now(timezone.utc)
    if retryable and job["attempt"] < job["max_attempts"]:
        delay = compute_retry_delay_s(
            job["attempt"], settings.retry_base_delay_s, settings.retry_max_delay_s
        )
        next_retry = (now + timedelta(seconds=delay)).isoformat()
        with db.connect(settings) as conn:
            conn.execute(
                """UPDATE jobs SET status = 'queued', stage = 'queued',
                          error_code = ?, error_message = ?, next_retry_at = ?,
                          updated_at = ? WHERE id = ?""",
                (error_code, error_message[:2000], next_retry, db.utcnow_iso(), job_id),
            )
        log.warning(
            "job failed, will retry",
            extra={"job_id": job_id, "code": error_code, "retry_in_s": round(delay)},
        )
    else:
        with db.connect(settings) as conn:
            conn.execute(
                """UPDATE jobs SET status = 'failed', stage = 'failed',
                          error_code = ?, error_message = ?, updated_at = ?
                   WHERE id = ?""",
                (error_code, error_message[:2000], db.utcnow_iso(), job_id),
            )
            if job.get("video_id"):
                conn.execute(
                    "UPDATE videos SET status = 'failed', updated_at = ? WHERE id = ?",
                    (db.utcnow_iso(), job["video_id"]),
                )
        log.error("job failed permanently", extra={"job_id": job_id, "code": error_code})
    return get_job(settings, job_id)  # type: ignore[return-value]


def heartbeat(settings: Settings) -> None:
    with db.connect(settings) as conn:
        conn.execute(
            """INSERT INTO kv (key, value, updated_at) VALUES ('worker_heartbeat', ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
               updated_at = excluded.updated_at""",
            (db.utcnow_iso(), db.utcnow_iso()),
        )


def get_heartbeat(settings: Settings) -> str | None:
    with db.connect(settings) as conn:
        row = conn.execute(
            "SELECT value FROM kv WHERE key = 'worker_heartbeat'"
        ).fetchone()
        return row["value"] if row else None
