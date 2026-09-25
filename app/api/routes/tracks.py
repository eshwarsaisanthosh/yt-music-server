"""GET /v1/tracks/{id}/stream — byte-range streaming for AVPlayer.

AVPlayer issues `Range: bytes=N-` requests and requires 206 Partial Content
with a correct Content-Range. Full (non-range) requests return 200.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterator

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse

from app import db
from app.api.deps import require_auth, settings_dep
from app.config import Settings

router = APIRouter()

_CHUNK = 256 * 1024
_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)$")


def _iter_file(path: Path, start: int, end: int) -> Iterator[bytes]:
    with open(path, "rb") as f:
        f.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            chunk = f.read(min(_CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def _resolve_track_file(settings: Settings, track_id: str) -> Path:
    with db.connect(settings) as conn:
        row = conn.execute(
            "SELECT file_path FROM tracks WHERE id = ?", (track_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "TRACK_NOT_FOUND", "message": f"track {track_id} not found"},
        )
    # file_path is server-generated and relative to media_root — never trust
    # it blindly: resolve and confirm it stays inside the media tree.
    path = (settings.media_root / row["file_path"]).resolve()
    if os.path.commonpath([path, settings.media_root.resolve()]) != str(
        settings.media_root.resolve()
    ):
        raise HTTPException(
            status_code=404,
            detail={"code": "TRACK_NOT_FOUND", "message": "track file unavailable"},
        )
    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail={"code": "TRACK_FILE_MISSING",
                    "message": "track file is missing from the media library"},
        )
    return path


@router.get("/tracks/{track_id}/stream", dependencies=[Depends(require_auth)])
async def stream_track(
    track_id: str,
    settings: Settings = Depends(settings_dep),
    range_header: str | None = Header(default=None, alias="Range"),
) -> StreamingResponse:
    path = _resolve_track_file(settings, track_id)
    size = path.stat().st_size

    if not range_header:
        return StreamingResponse(
            _iter_file(path, 0, size - 1),
            media_type="audio/mp4",
            headers={
                "Accept-Ranges": "bytes",
                "Content-Length": str(size),
            },
        )

    m = _RANGE_RE.match(range_header.strip())
    if not m:
        raise HTTPException(
            status_code=416,
            detail={"code": "INVALID_RANGE", "message": "malformed Range header"},
        )
    start_s, end_s = m.groups()
    if start_s == "" and end_s == "":
        raise HTTPException(
            status_code=416,
            detail={"code": "INVALID_RANGE", "message": "malformed Range header"},
        )
    if start_s == "":
        # Suffix range: last N bytes.
        start = max(0, size - int(end_s))
        end = size - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s else size - 1
    if start >= size or end < start:
        raise HTTPException(
            status_code=416,
            headers={"Content-Range": f"bytes */{size}"},
            detail={"code": "RANGE_NOT_SATISFIABLE",
                    "message": "range outside file bounds"},
        )
    end = min(end, size - 1)
    return StreamingResponse(
        _iter_file(path, start, end),
        status_code=206,
        media_type="audio/mp4",
        headers={
            "Accept-Ranges": "bytes",
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(end - start + 1),
        },
    )
