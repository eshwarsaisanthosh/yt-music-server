"""Ingest pipeline: intake -> metadata -> download -> split -> index -> cleanup.

Each stage is idempotent so a retried job can safely re-run from the top:
metadata overwrites, yt-dlp resumes partial downloads, split outputs are
regenerated in staging, and indexing deletes + reinserts the video's tracks
inside one transaction.

Publication invariant: the library (videos/tracks rows) only ever references
files already moved into the media tree. Staging is disposable.
"""

from __future__ import annotations

import shutil
import sqlite3
import urllib.request
from pathlib import Path

from app import db
from app.adapters.youtube import (
    Chapter as RawChapter,
    PermanentSourceError,
    VideoMetadata,
    classify_error,
    download_audio,
    fetch_metadata,
)
from app.config import Settings
from app.core import jobs as jobq
from app.core.ids import new_id
from app.logging import get_logger
from app.media import ffmpeg
from app.media.splitter import SplitChapter, normalize_chapters, split_to_alac

log = get_logger(__name__)


class PipelineError(Exception):
    """A pipeline failure with a stable code and retryability."""

    def __init__(self, code: str, message: str, retryable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def run_job(settings: Settings, job_id: str) -> None:
    job = jobq.get_job(settings, job_id)
    if job is None:
        log.error("job vanished before run", extra={"job_id": job_id})
        return
    ctx = {"job_id": job_id, "youtube_id": job["youtube_id"], "attempt": job["attempt"]}
    staging = settings.staging_root / job_id
    log.info("job started", extra={**ctx, "stage": "metadata"})

    try:
        staging.mkdir(parents=True, exist_ok=True)

        metadata = _stage_metadata(settings, job, ctx)
        src = _stage_download(settings, job, metadata, staging, ctx)
        artifacts = _stage_split(settings, job, metadata, src, staging, ctx)
        _stage_index(settings, job, metadata, artifacts, ctx)

        jobq.complete_job(settings, job_id)
        log.info("job completed", extra={**ctx, "tracks": len(artifacts)})
    except PipelineError as exc:
        jobq.fail_job(settings, job_id, exc.code, str(exc), exc.retryable)
    except PermanentSourceError as exc:
        jobq.fail_job(settings, job_id, exc.code, str(exc), False)
    except ffmpeg.FFmpegError as exc:
        code, retryable = classify_error(exc.stderr)
        jobq.fail_job(settings, job_id, f"FFMPEG_{code}", str(exc), retryable)
    except Exception as exc:  # noqa: BLE001 - last-resort guard
        code, retryable = classify_error(str(exc))
        log.exception("job crashed", extra={**ctx, "code": code})
        jobq.fail_job(settings, job_id, code, f"{type(exc).__name__}: {exc}", retryable)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# ---------------------------------------------------------------- stages ---


def _stage_metadata(settings: Settings, job: dict, ctx: dict) -> VideoMetadata:
    jobq.update_job(settings, job["id"], stage="metadata", progress=0.02)
    metadata = fetch_metadata(job["source_url"], job["youtube_id"], settings)
    if metadata.duration_s <= 0:
        raise PipelineError("ZERO_DURATION", "video reports zero duration", retryable=False)
    if metadata.duration_s > settings.max_video_duration_s:
        raise PipelineError(
            "DURATION_EXCEEDED",
            f"video is {metadata.duration_s:.0f}s, limit is {settings.max_video_duration_s}s",
            retryable=False,
        )
    thumbnail_path = _fetch_thumbnail(settings, job, metadata)
    now = db.utcnow_iso()
    with db.connect(settings) as conn:
        conn.execute(
            """UPDATE videos SET title = ?, uploader = ?, duration_s = ?,
                      thumbnail_path = ?, status = 'processing', updated_at = ?
               WHERE id = ?""",
            (
                metadata.title, metadata.uploader, metadata.duration_s,
                thumbnail_path, now, job["video_id"],
            ),
        )
    log.info(
        "metadata extracted",
        extra={**ctx, "title": metadata.title,
               "duration_s": round(metadata.duration_s, 1),
               "chapters": len(metadata.chapters)},
    )
    return metadata


def _fetch_thumbnail(settings: Settings, job: dict, metadata: VideoMetadata) -> str | None:
    """Best-effort thumbnail download; never fails the job."""
    if not metadata.thumbnail_url:
        return None
    try:
        dest_dir = settings.media_root / job["youtube_id"]
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / "cover.jpg"
        req = urllib.request.Request(
            metadata.thumbnail_url, headers={"User-Agent": "yt-music-server/0.1"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp, open(dest, "wb") as f:
            shutil.copyfileobj(resp, f)
        return str(dest.relative_to(settings.media_root))
    except Exception as exc:  # noqa: BLE001 - thumbnails are optional
        log.warning("thumbnail download failed", extra={"job_id": job["id"], "err": str(exc)})
        return None


def _stage_download(
    settings: Settings, job: dict, metadata: VideoMetadata, staging: Path, ctx: dict
) -> Path:
    jobq.update_job(settings, job["id"], stage="download", progress=0.05)

    free = shutil.disk_usage(staging).free
    if free < settings.min_free_disk_bytes:
        raise PipelineError(
            "STORAGE_FULL",
            f"only {free // 1024 // 1024}MB free in staging, need "
            f"{settings.min_free_disk_bytes // 1024 // 1024}MB",
            retryable=True,
        )

    def hook(frac: float | None) -> None:
        if frac is not None:
            jobq.update_job(settings, job["id"], progress=0.05 + 0.55 * frac)

    try:
        src = download_audio(
            metadata.webpage_url or job["source_url"],
            outtmpl=str(staging / "source.%(ext)s"),
            progress_hook=hook,
            settings=settings,
        )
    except PermanentSourceError:
        raise
    except Exception as exc:  # noqa: BLE001 - classified below
        code, retryable = classify_error(str(exc))
        raise PipelineError(code, str(exc), retryable) from exc

    info = ffmpeg.probe(src)
    if info.codec == "":
        raise PipelineError("NO_AUDIO_STREAM", "downloaded file has no audio stream",
                             retryable=False)
    log.info(
        "source probed",
        extra={**ctx, "codec": info.codec, "sample_rate": info.sample_rate,
               "channels": info.channels, "duration_s": round(info.duration_s, 1)},
    )
    return src


def _stage_split(
    settings: Settings,
    job: dict,
    metadata: VideoMetadata,
    src: Path,
    staging: Path,
    ctx: dict,
) -> list:
    jobq.update_job(settings, job["id"], stage="split", progress=0.62)

    chapters = normalize_chapters(
        metadata.chapters, metadata.duration_s, settings.min_track_duration_s
    )
    needs_review = False
    if not chapters:
        needs_review = True
        chapters = [
            SplitChapter(
                start_s=0.0,
                end_s=metadata.duration_s,
                title=metadata.title or "Full video",
            )
        ]
        log.info("no usable chapters; single-track fallback", extra=ctx)

    out_dir = staging / "tracks"
    artifacts = split_to_alac(
        src,
        chapters,
        album=metadata.title,
        artist=metadata.uploader,
        out_dir=out_dir,
    )
    jobq.update_job(settings, job["id"], progress=0.88)

    now = db.utcnow_iso()
    with db.connect(settings) as conn:
        conn.execute(
            "UPDATE videos SET needs_review = ?, updated_at = ? WHERE id = ?",
            (1 if needs_review else 0, now, job["video_id"]),
        )
    return artifacts


def _stage_index(
    settings: Settings, job: dict, metadata: VideoMetadata, artifacts: list, ctx: dict
) -> None:
    jobq.update_job(settings, job["id"], stage="index", progress=0.92)

    # Move finished tracks into the published media tree first; the DB
    # transaction below only references files that already exist there.
    dest_dir = settings.media_root / job["youtube_id"]
    dest_dir.mkdir(parents=True, exist_ok=True)
    total = len(artifacts)
    moved: list[tuple] = []
    for art in artifacts:
        dest = dest_dir / art.path.name
        art.path.replace(dest)  # atomic rename on the same filesystem
        rel = str(dest.relative_to(settings.media_root))
        moved.append((art, rel))

    now = db.utcnow_iso()
    with db.connect(settings) as conn:
        # Idempotent re-index: a retried job replaces its own tracks.
        conn.execute("DELETE FROM tracks WHERE video_id = ?", (job["video_id"],))
        for art, rel in moved:
            conn.execute(
                """INSERT INTO tracks (id, video_id, title, track_no, track_total,
                                       start_s, duration_s, file_path, file_size,
                                       codec, sample_rate, channels, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_id("trk"), job["video_id"], art.title, art.track_no, total,
                    art.start_s, art.duration_s, rel, art.file_size,
                    art.codec, art.sample_rate, art.channels, now,
                ),
            )
        conn.execute(
            "UPDATE videos SET status = 'ready', updated_at = ? WHERE id = ?",
            (now, job["video_id"]),
        )
        if job.get("playlist_id"):
            _assert_playlist(conn, job["playlist_id"])
            _add_tracks_to_playlist(conn, job["video_id"], job["playlist_id"], now)

    log.info("indexed tracks", extra={**ctx, "tracks": total,
                                      "dir": str(dest_dir)})


def _assert_playlist(conn: sqlite3.Connection, playlist_id: str) -> None:
    row = conn.execute("SELECT id FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
    if row is None:
        raise PipelineError("PLAYLIST_NOT_FOUND",
                            f"playlist {playlist_id} does not exist", retryable=False)


def _add_tracks_to_playlist(
    conn: sqlite3.Connection, video_id: str, playlist_id: str, now: str
) -> None:
    tracks = conn.execute(
        "SELECT id FROM tracks WHERE video_id = ? ORDER BY track_no", (video_id,)
    ).fetchall()
    pos = conn.execute(
        "SELECT COALESCE(MAX(position), -1) FROM playlist_tracks WHERE playlist_id = ?",
        (playlist_id,),
    ).fetchone()[0] + 1
    for t in tracks:
        try:
            conn.execute(
                """INSERT INTO playlist_tracks (id, playlist_id, track_id, position, added_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (new_id("plt"), playlist_id, t["id"], pos, now),
            )
            pos += 1
        except sqlite3.IntegrityError:
            continue  # already present; keep the original position
