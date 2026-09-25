"""GET /v1/search — simple substring search across the library."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app import db
from app.api.deps import require_auth, settings_dep
from app.api.routes.videos import _track_out, _video_out
from app.api.schemas import PlaylistOut, SearchOut
from app.config import Settings

router = APIRouter()


@router.get("/search", response_model=SearchOut,
            dependencies=[Depends(require_auth)])
async def search(
    q: str = Query(..., min_length=1, max_length=200),
    settings: Settings = Depends(settings_dep),
) -> SearchOut:
    like = f"%{q}%"
    with db.connect(settings) as conn:
        track_rows = conn.execute(
            """SELECT t.* FROM tracks t JOIN videos v ON v.id = t.video_id
               WHERE t.title LIKE ? OR v.title LIKE ? OR v.uploader LIKE ?
               ORDER BY t.title LIMIT 50""",
            (like, like, like),
        ).fetchall()
        video_rows = conn.execute(
            """SELECT v.*, COUNT(t.id) AS track_count
               FROM videos v LEFT JOIN tracks t ON t.video_id = v.id
               WHERE v.title LIKE ? OR v.uploader LIKE ?
               GROUP BY v.id ORDER BY v.title LIMIT 25""",
            (like, like),
        ).fetchall()
        pl_rows = conn.execute(
            "SELECT * FROM playlists WHERE name LIKE ? ORDER BY name LIMIT 25",
            (like,),
        ).fetchall()
        playlists: list[PlaylistOut] = []
        for p in pl_rows:
            count = conn.execute(
                "SELECT COUNT(*) FROM playlist_tracks WHERE playlist_id = ?",
                (p["id"],),
            ).fetchone()[0]
            playlists.append(
                PlaylistOut(id=p["id"], name=p["name"], track_count=count)
            )
    return SearchOut(
        tracks=[_track_out(t) for t in track_rows],
        videos=[_video_out(v, v["track_count"]) for v in video_rows],
        playlists=playlists,
    )
