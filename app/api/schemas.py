"""Pydantic request/response models for the v1 API."""

from __future__ import annotations

from pydantic import BaseModel, Field


class IngestRequest(BaseModel):
    url: str = Field(..., min_length=8, description="YouTube video URL")
    playlist_id: str | None = Field(
        default=None, description="Add resulting tracks to this playlist when done"
    )


class JobOut(BaseModel):
    job_id: str
    video_id: str
    youtube_id: str
    status: str
    stage: str
    progress: float
    attempt: int
    max_attempts: int
    error_code: str | None = None
    error_message: str | None = None
    existing: bool = False


class TrackOut(BaseModel):
    id: str
    video_id: str
    title: str
    track_no: int
    track_total: int
    start_s: float
    duration_s: float
    file_size: int
    codec: str
    sample_rate: int
    channels: int


class VideoOut(BaseModel):
    id: str
    youtube_id: str
    source_url: str
    title: str
    uploader: str
    duration_s: float
    needs_review: bool
    status: str
    track_count: int = 0
    tracks: list[TrackOut] | None = None


class PlaylistCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)


class PlaylistUpdate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)


class PlaylistTrackAdd(BaseModel):
    track_id: str
    position: int | None = Field(
        default=None, ge=0, description="Insert at position; default appends"
    )


class PlaylistOut(BaseModel):
    id: str
    name: str
    track_count: int = 0
    tracks: list[TrackOut] | None = None


class SearchOut(BaseModel):
    tracks: list[TrackOut]
    videos: list[VideoOut]
    playlists: list[PlaylistOut]


class HealthOut(BaseModel):
    status: str
    version: str
    database: str
    media_writable: bool
    worker_alive: bool
    worker_heartbeat_age_s: float | None = None
