"""Shared fixtures: isolated settings/DB per test, ffmpeg-generated audio."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.config import Settings


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    s = Settings(
        data_root=tmp_path / "data",
        job_max_attempts=3,
        retry_base_delay_s=0.01,
        retry_max_delay_s=0.05,
        log_level="WARNING",
    )
    s.ensure_dirs()
    db.init_db(s)
    return s


@pytest.fixture()
def client(settings: Settings) -> TestClient:
    from app.main import create_app

    return TestClient(create_app(settings))


def run_ffmpeg(args: list[str]) -> None:
    subprocess.run(args, check=True, capture_output=True, timeout=120)


@pytest.fixture(scope="session")
def tone_triplet(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """30s Opus file: 10s each of 440/660/880 Hz sine (YouTube-like source)."""
    d = tmp_path_factory.mktemp("fixtures")
    wav = d / "triplet.wav"
    src = d / "triplet.webm"
    run_ffmpeg(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=10:sample_rate=48000",
            "-f", "lavfi", "-i", "sine=frequency=660:duration=10:sample_rate=48000",
            "-f", "lavfi", "-i", "sine=frequency=880:duration=10:sample_rate=48000",
            "-filter_complex", "[0:a][1:a][2:a]concat=n=3:v=0:a=1",
            "-y", str(wav),
        ]
    )
    # Encode like YouTube's best audio: Opus ~160k.
    run_ffmpeg(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", str(wav), "-c:a", "libopus", "-b:a", "160k",
            "-y", str(src),
        ]
    )
    return src


@pytest.fixture(scope="session")
def short_alac(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """5s ALAC file used as a fake published track for stream tests."""
    d = tmp_path_factory.mktemp("fixtures")
    out = d / "track.m4a"
    run_ffmpeg(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=5:sample_rate=48000",
            "-c:a", "alac", "-y", str(out),
        ]
    )
    return out


def seed_ready_video(
    settings: Settings,
    youtube_id: str = "dQw4w9WgXcQ",
    title: str = "Test Video",
    track_files: list[Path] | None = None,
) -> tuple[str, list[str]]:
    """Insert a 'ready' video with tracks; copy audio files into the media tree."""
    from app.core.ids import new_id

    video_id = new_id("vid")
    track_files = track_files or []
    track_ids: list[str] = []
    now = db.utcnow_iso()
    dest_dir = settings.media_root / youtube_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    with db.connect(settings) as conn:
        conn.execute(
            """INSERT INTO videos (id, youtube_id, source_url, title, uploader,
                                   duration_s, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'uploader', 30.0, 'ready', ?, ?)""",
            (video_id, youtube_id,
             f"https://www.youtube.com/watch?v={youtube_id}", title, now, now),
        )
        for i, src in enumerate(track_files, 1):
            dest = dest_dir / f"{i:02d} - track{i}.m4a"
            dest.write_bytes(src.read_bytes())
            tid = new_id("trk")
            conn.execute(
                """INSERT INTO tracks (id, video_id, title, track_no, track_total,
                                       start_s, duration_s, file_path, file_size,
                                       codec, sample_rate, channels, created_at)
                   VALUES (?, ?, ?, ?, ?, 0, 5.0, ?, ?, 'alac', 48000, 2, ?)""",
                (tid, video_id, f"Track {i}", i, len(track_files),
                 str(dest.relative_to(settings.media_root)),
                 dest.stat().st_size, now),
            )
            track_ids.append(tid)
    return video_id, track_ids
