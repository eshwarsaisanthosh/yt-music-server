"""Playlist CRUD plus ordered track membership."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from app import db
from app.api.deps import require_auth, settings_dep
from app.api.routes.videos import _track_out
from app.api.schemas import PlaylistCreate, PlaylistOut, PlaylistTrackAdd, PlaylistUpdate
from app.config import Settings
from app.core.ids import new_id

router = APIRouter()


def _playlist_out(conn: sqlite3.Connection, playlist_id: str,
                  with_tracks: bool) -> PlaylistOut:
    row = conn.execute(
        "SELECT * FROM playlists WHERE id = ?", (playlist_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "PLAYLIST_NOT_FOUND",
                    "message": f"playlist {playlist_id} not found"},
        )
    tracks = None
    if with_tracks:
        rows = conn.execute(
            """SELECT t.* FROM tracks t
               JOIN playlist_tracks pt ON pt.track_id = t.id
               WHERE pt.playlist_id = ? ORDER BY pt.position""",
            (playlist_id,),
        ).fetchall()
        tracks = [_track_out(r) for r in rows]
    count = conn.execute(
        "SELECT COUNT(*) FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,)
    ).fetchone()[0]
    return PlaylistOut(id=row["id"], name=row["name"],
                       track_count=count, tracks=tracks)


@router.post("/playlists", response_model=PlaylistOut, status_code=201,
             dependencies=[Depends(require_auth)])
async def create_playlist(
    req: PlaylistCreate,
    settings: Settings = Depends(settings_dep),
) -> PlaylistOut:
    playlist_id = new_id("pl")
    now = db.utcnow_iso()
    with db.connect(settings) as conn:
        conn.execute(
            "INSERT INTO playlists (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (playlist_id, req.name.strip(), now, now),
        )
        return _playlist_out(conn, playlist_id, with_tracks=False)


@router.get("/playlists", response_model=list[PlaylistOut],
            dependencies=[Depends(require_auth)])
async def list_playlists(settings: Settings = Depends(settings_dep)) -> list[PlaylistOut]:
    with db.connect(settings) as conn:
        rows = conn.execute("SELECT id FROM playlists ORDER BY created_at").fetchall()
        return [_playlist_out(conn, r["id"], with_tracks=False) for r in rows]


@router.get("/playlists/{playlist_id}", response_model=PlaylistOut,
            dependencies=[Depends(require_auth)])
async def get_playlist(
    playlist_id: str,
    settings: Settings = Depends(settings_dep),
) -> PlaylistOut:
    with db.connect(settings) as conn:
        return _playlist_out(conn, playlist_id, with_tracks=True)


@router.patch("/playlists/{playlist_id}", response_model=PlaylistOut,
              dependencies=[Depends(require_auth)])
async def rename_playlist(
    playlist_id: str,
    req: PlaylistUpdate,
    settings: Settings = Depends(settings_dep),
) -> PlaylistOut:
    with db.connect(settings) as conn:
        cur = conn.execute(
            "UPDATE playlists SET name = ?, updated_at = ? WHERE id = ?",
            (req.name.strip(), db.utcnow_iso(), playlist_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(
                status_code=404,
                detail={"code": "PLAYLIST_NOT_FOUND",
                        "message": f"playlist {playlist_id} not found"},
            )
        return _playlist_out(conn, playlist_id, with_tracks=True)


@router.delete("/playlists/{playlist_id}", status_code=204,
               dependencies=[Depends(require_auth)])
async def delete_playlist(
    playlist_id: str,
    settings: Settings = Depends(settings_dep),
) -> None:
    with db.connect(settings) as conn:
        cur = conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
        if cur.rowcount == 0:
            raise HTTPException(
                status_code=404,
                detail={"code": "PLAYLIST_NOT_FOUND",
                        "message": f"playlist {playlist_id} not found"},
            )


@router.post("/playlists/{playlist_id}/tracks", response_model=PlaylistOut,
             dependencies=[Depends(require_auth)])
async def add_track(
    playlist_id: str,
    req: PlaylistTrackAdd,
    settings: Settings = Depends(settings_dep),
) -> PlaylistOut:
    now = db.utcnow_iso()
    with db.connect(settings) as conn:
        pl = conn.execute(
            "SELECT id FROM playlists WHERE id = ?", (playlist_id,)
        ).fetchone()
        if pl is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "PLAYLIST_NOT_FOUND",
                        "message": f"playlist {playlist_id} not found"},
            )
        tr = conn.execute(
            "SELECT id FROM tracks WHERE id = ?", (req.track_id,)
        ).fetchone()
        if tr is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "TRACK_NOT_FOUND",
                        "message": f"track {req.track_id} not found"},
            )
        if req.position is None:
            pos = conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM playlist_tracks "
                "WHERE playlist_id = ?",
                (playlist_id,),
            ).fetchone()[0]
        else:
            conn.execute(
                "UPDATE playlist_tracks SET position = position + 1 "
                "WHERE playlist_id = ? AND position >= ?",
                (playlist_id, req.position),
            )
            pos = req.position
        try:
            conn.execute(
                """INSERT INTO playlist_tracks (id, playlist_id, track_id, position, added_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (new_id("plt"), playlist_id, req.track_id, pos, now),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(
                status_code=409,
                detail={"code": "TRACK_ALREADY_IN_PLAYLIST",
                        "message": "track is already in this playlist"},
            )
        return _playlist_out(conn, playlist_id, with_tracks=True)


@router.delete("/playlists/{playlist_id}/tracks/{track_id}", status_code=204,
               dependencies=[Depends(require_auth)])
async def remove_track(
    playlist_id: str,
    track_id: str,
    settings: Settings = Depends(settings_dep),
) -> None:
    with db.connect(settings) as conn:
        cur = conn.execute(
            "DELETE FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?",
            (playlist_id, track_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(
                status_code=404,
                detail={"code": "PLAYLIST_TRACK_NOT_FOUND",
                        "message": "track is not in this playlist"},
            )
        # Compact positions so ordering stays dense.
        rows = conn.execute(
            "SELECT id FROM playlist_tracks WHERE playlist_id = ? ORDER BY position",
            (playlist_id,),
        ).fetchall()
        for i, r in enumerate(rows):
            conn.execute(
                "UPDATE playlist_tracks SET position = ? WHERE id = ?", (i, r["id"])
            )
