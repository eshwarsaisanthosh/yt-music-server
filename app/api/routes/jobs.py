"""GET /v1/jobs/{id} — job status, stage, progress, and errors."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import require_auth, settings_dep
from app.api.schemas import JobOut
from app.config import Settings
from app.core import jobs as jobq

router = APIRouter()


@router.get("/jobs/{job_id}", response_model=JobOut,
            dependencies=[Depends(require_auth)])
async def get_job(
    job_id: str,
    settings: Settings = Depends(settings_dep),
) -> JobOut:
    job = jobq.get_job(settings, job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "JOB_NOT_FOUND", "message": f"job {job_id} not found"},
        )
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
    )
