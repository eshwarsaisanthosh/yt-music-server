"""Video library reads: list videos, get one video with its tracks."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from app import db
from app.api.deps import require_auth, settings_dep
from app.api.schemas import TrackOut, VideoOut
from app.config import Settings

router = APIRouter()


def _track_out(row: sqlite3.Row) -> TrackOut:
    return TrackOut(
        id=row["id"],
        video_id=row["video_id"],
        title=row["title"],
        track_no=row["track_no"],
        track_total=row["track_total"],
        start_s=row["start_s"],
        duration_s=row["duration_s"],
        file_size=row["file_size"],
        codec=row["codec"],
        sample_rate=row["sample_rate"],
        channels=row["channels"],
    )


def _video_out(row: sqlite3.Row, track_count: int,
               tracks: list[TrackOut] | None = None) -> VideoOut:
    return VideoOut(
        id=row["id"],
        youtube_id=row["youtube_id"],
        source_url=row["source_url"],
        title=row["title"],
        uploader=row["uploader"],
        duration_s=row["duration_s"],
        needs_review=bool(row["needs_review"]),
        status=row["status"],
        track_count=track_count,
        tracks=tracks,
    )


@router.get("/videos", response_model=list[VideoOut],
            dependencies=[Depends(require_auth)])
async def list_videos(settings: Settings = Depends(settings_dep)) -> list[VideoOut]:
    with db.connect(settings) as conn:
        rows = conn.execute(
            """SELECT v.*, COUNT(t.id) AS track_count
               FROM videos v LEFT JOIN tracks t ON t.video_id = v.id
               GROUP BY v.id ORDER BY v.created_at DESC"""
        ).fetchall()
    return [_video_out(r, r["track_count"]) for r in rows]


@router.get("/videos/{video_id}", response_model=VideoOut,
            dependencies=[Depends(require_auth)])
async def get_video(
    video_id: str,
    settings: Settings = Depends(settings_dep),
) -> VideoOut:
    with db.connect(settings) as conn:
        video = conn.execute(
            "SELECT * FROM videos WHERE id = ?", (video_id,)
        ).fetchone()
        if video is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "VIDEO_NOT_FOUND",
                        "message": f"video {video_id} not found"},
            )
        tracks = conn.execute(
            "SELECT * FROM tracks WHERE video_id = ? ORDER BY track_no",
            (video_id,),
        ).fetchall()
    return _video_out(video, len(tracks), [_track_out(t) for t in tracks])
