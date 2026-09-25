"""GET /healthz — liveness plus dependency checks (no auth)."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from app import db
from app.api.deps import settings_dep
from app.api.schemas import HealthOut
from app.config import Settings
from app.core import jobs as jobq

router = APIRouter()


@router.get("/healthz", response_model=HealthOut)
async def healthz(settings: Settings = Depends(settings_dep)) -> HealthOut:
    database = "ok"
    try:
        with db.connect(settings) as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception:
        database = "error"

    media_writable = False
    try:
        settings.media_root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=settings.media_root, delete=True):
            media_writable = True
    except OSError:
        pass

    heartbeat = jobq.get_heartbeat(settings)
    age_s: float | None = None
    worker_alive = False
    if heartbeat:
        try:
            ts = datetime.fromisoformat(heartbeat)
            age_s = (datetime.now(timezone.utc) - ts).total_seconds()
            worker_alive = age_s < settings.worker_heartbeat_ttl_s
        except ValueError:
            pass

    return HealthOut(
        status="ok" if database == "ok" else "degraded",
        version=settings.app_version,
        database=database,
        media_writable=media_writable,
        worker_alive=worker_alive,
        worker_heartbeat_age_s=age_s,
    )
