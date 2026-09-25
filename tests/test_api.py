"""API tests via TestClient: ingest, jobs, videos, playlists, search, streaming."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from tests.conftest import seed_ready_video

YT_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_healthz(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["media_writable"] is True
    assert body["worker_alive"] is False  # no worker running in tests


def test_ingest_queues_job(client: TestClient) -> None:
    r = client.post("/v1/ingest", json={"url": YT_URL})
    assert r.status_code == 202
    body = r.json()
    assert body["youtube_id"] == "dQw4w9WgXcQ"
    assert body["status"] == "queued"
    assert body["existing"] is False

    r2 = client.get(f"/v1/jobs/{body['job_id']}")
    assert r2.status_code == 200
    assert r2.json()["stage"] == "queued"


def test_ingest_is_idempotent(client: TestClient) -> None:
    first = client.post("/v1/ingest", json={"url": YT_URL}).json()
    second = client.post("/v1/ingest", json={"url": "https://youtu.be/dQw4w9WgXcQ"}).json()
    assert second["existing"] is True
    assert second["job_id"] == first["job_id"]


def test_ingest_rejects_non_youtube(client: TestClient) -> None:
    r = client.post("/v1/ingest", json={"url": "https://example.com/video.mp4"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "UNSUPPORTED_URL"


def test_ingest_rejects_unknown_playlist(client: TestClient) -> None:
    r = client.post("/v1/ingest",
                    json={"url": YT_URL, "playlist_id": "pl_nope"})
    assert r.status_code == 404


def test_ingest_with_playlist_records_destination(client: TestClient) -> None:
    pl = client.post("/v1/playlists", json={"name": "Lofi"}).json()
    r = client.post("/v1/ingest",
                    json={"url": YT_URL, "playlist_id": pl["id"]})
    assert r.status_code == 202
    job = client.get(f"/v1/jobs/{r.json()['job_id']}").json()
    assert job["job_id"] == r.json()["job_id"]


def test_job_not_found(client: TestClient) -> None:
    assert client.get("/v1/jobs/job_nope").status_code == 404


def test_playlist_crud(client: TestClient, settings: Settings,
                       short_alac: Path) -> None:
    _, track_ids = seed_ready_video(settings, track_files=[short_alac])

    created = client.post("/v1/playlists", json={"name": "Focus"}).json()
    assert created["name"] == "Focus"
    pid = created["id"]

    listed = client.get("/v1/playlists").json()
    assert [p["id"] for p in listed] == [pid]

    got = client.get(f"/v1/playlists/{pid}").json()
    assert got["track_count"] == 0

    with_tracks = client.post(
        f"/v1/playlists/{pid}/tracks", json={"track_id": track_ids[0]}).json()
    assert with_tracks["track_count"] == 1
    assert with_tracks["tracks"][0]["title"] == "Track 1"

    dup = client.post(f"/v1/playlists/{pid}/tracks",
                      json={"track_id": track_ids[0]})
    assert dup.status_code == 409

    renamed = client.patch(f"/v1/playlists/{pid}", json={"name": "Deep Focus"}).json()
    assert renamed["name"] == "Deep Focus"

    rm = client.delete(f"/v1/playlists/{pid}/tracks/{track_ids[0]}")
    assert rm.status_code == 204
    assert client.get(f"/v1/playlists/{pid}").json()["track_count"] == 0

    assert client.delete(f"/v1/playlists/{pid}").status_code == 204
    assert client.get(f"/v1/playlists/{pid}").status_code == 404


def test_playlist_add_unknown_track(client: TestClient) -> None:
    pid = client.post("/v1/playlists", json={"name": "X"}).json()["id"]
    r = client.post(f"/v1/playlists/{pid}/tracks", json={"track_id": "trk_nope"})
    assert r.status_code == 404


def test_videos_list_and_get(client: TestClient, settings: Settings,
                             short_alac: Path) -> None:
    video_id, _ = seed_ready_video(settings, title="Chill Mix",
                                   track_files=[short_alac, short_alac])
    videos = client.get("/v1/videos").json()
    assert len(videos) == 1
    assert videos[0]["track_count"] == 2
    assert videos[0]["tracks"] is None  # list view omits tracks

    detail = client.get(f"/v1/videos/{video_id}").json()
    assert detail["title"] == "Chill Mix"
    assert [t["track_no"] for t in detail["tracks"]] == [1, 2]
    assert detail["tracks"][0]["codec"] == "alac"

    assert client.get("/v1/videos/vid_nope").status_code == 404


def test_search(client: TestClient, settings: Settings, short_alac: Path) -> None:
    seed_ready_video(settings, title="Chill Beats Mix", track_files=[short_alac])
    client.post("/v1/playlists", json={"name": "Chill Zone"})
    res = client.get("/v1/search", params={"q": "Chill"}).json()
    assert len(res["videos"]) == 1
    assert len(res["tracks"]) == 1
    assert len(res["playlists"]) == 1
    empty = client.get("/v1/search", params={"q": "zzz-no-match"}).json()
    assert empty == {"tracks": [], "videos": [], "playlists": []}


def test_stream_full_and_range(client: TestClient, settings: Settings,
                               short_alac: Path) -> None:
    _, track_ids = seed_ready_video(settings, track_files=[short_alac])
    tid = track_ids[0]
    size = short_alac.stat().st_size

    full = client.get(f"/v1/tracks/{tid}/stream")
    assert full.status_code == 200
    assert full.headers["Accept-Ranges"] == "bytes"
    assert int(full.headers["Content-Length"]) == size
    assert len(full.content) == size

    part = client.get(f"/v1/tracks/{tid}/stream",
                      headers={"Range": "bytes=0-99"})
    assert part.status_code == 206
    assert part.headers["Content-Range"] == f"bytes 0-99/{size}"
    assert len(part.content) == 100
    assert part.content == full.content[:100]

    open_ended = client.get(f"/v1/tracks/{tid}/stream",
                            headers={"Range": f"bytes={size - 10}-"})
    assert open_ended.status_code == 206
    assert open_ended.headers["Content-Range"] == f"bytes {size - 10}-{size - 1}/{size}"
    assert len(open_ended.content) == 10

    suffix = client.get(f"/v1/tracks/{tid}/stream",
                        headers={"Range": "bytes=-50"})
    assert suffix.status_code == 206
    assert len(suffix.content) == 50

    bad = client.get(f"/v1/tracks/{tid}/stream",
                     headers={"Range": f"bytes={size + 100}-{size + 200}"})
    assert bad.status_code == 416

    assert client.get("/v1/tracks/trk_nope/stream").status_code == 404


def test_auth_enforced_when_token_set(tmp_path, monkeypatch) -> None:
    from app.main import create_app

    data = tmp_path / "data"
    monkeypatch.setenv("YTM_DATA_ROOT", str(data))
    monkeypatch.setenv("YTM_API_TOKEN", "s3cret")
    settings = Settings()
    app_client = TestClient(create_app(settings))

    assert app_client.get("/v1/videos").status_code == 401
    # healthz stays open
    assert app_client.get("/healthz").status_code == 200
    ok = app_client.get("/v1/videos", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200
    wrong = app_client.get("/v1/videos",
                           headers={"Authorization": "Bearer wrong"})
    assert wrong.status_code == 401
