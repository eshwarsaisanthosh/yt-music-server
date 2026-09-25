"""POST /v1/ingest — queue a YouTube URL for ingestion (idempotent)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app import db
from app.adapters.youtube import extract_youtube_id
from app.api.deps import require_auth, settings_dep
from app.api.schemas import IngestRequest, JobOut
from app.config import Settings
from app.core import jobs as jobq

router = APIRouter()


def _job_out(job: dict, existing: bool) -> JobOut:
    return JobOut(
        job_id=job["id"],
        video_id=job["video_id"],
        youtube_id=job["youtube_id"],
        status=job["status"],
        stage=job["stage"],
        progress=job["progress"],
        attempt=job["attempt"],
        max_attempts=job["max_attempts"],
        error_code=job.get("error_code"),
        error_message=job.get("error_message"),
        existing=existing,
    )


@router.post("/ingest", response_model=JobOut, status_code=202,
             dependencies=[Depends(require_auth)])
async def ingest(
    req: IngestRequest,
    settings: Settings = Depends(settings_dep),
) -> JobOut:
    youtube_id = extract_youtube_id(req.url)
    if youtube_id is None:
        raise HTTPException(
            status_code=422,
            detail={"code": "UNSUPPORTED_URL",
                    "message": "URL is not a recognized YouTube video URL"},
        )
    if req.playlist_id:
        with db.connect(settings) as conn:
            row = conn.execute(
                "SELECT id FROM playlists WHERE id = ?", (req.playlist_id,)
            ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "PLAYLIST_NOT_FOUND",
                        "message": f"playlist {req.playlist_id} does not exist"},
            )
    job, _video, existing = jobq.enqueue_ingest(
        settings, youtube_id, req.url, req.playlist_id
    )
    return _job_out(job, existing)
