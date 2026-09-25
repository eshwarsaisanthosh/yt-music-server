"""Splitter tests against ffmpeg-generated fixtures (no network).

The fixture is 30s of Opus audio: 10s of 440 Hz, 10s of 660 Hz, 10s of
880 Hz. Splitting at 0/10/20 must yield three ALAC tracks with the right
durations, preserved sample rate, and the right tone in each track —
verified by FFT on decoded PCM, which proves the cuts are sample-accurate.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest

from app.adapters.youtube import Chapter as RawChapter
from app.media import ffmpeg
from app.media.splitter import normalize_chapters, split_to_alac


def dominant_freq(track: Path, sample_rate: int = 8000) -> float:
    """Decode the middle third of a track to mono PCM and return peak Hz."""
    info = ffmpeg.probe(track)
    mid = info.duration_s / 2
    proc = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", f"{mid - 1:.2f}", "-t", "2",
            "-i", str(track),
            "-ac", "1", "-ar", str(sample_rate),
            "-f", "f32le", "-",
        ],
        capture_output=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr.decode()[-300:]
    pcm = np.frombuffer(proc.stdout, dtype=np.float32)
    assert pcm.size > sample_rate  # at least 1s of audio
    spectrum = np.abs(np.fft.rfft(pcm))
    freqs = np.fft.rfftfreq(pcm.size, 1 / sample_rate)
    return float(freqs[int(np.argmax(spectrum[1:])) + 1])


def test_split_chapters_sample_accurate(tone_triplet: Path, tmp_path: Path) -> None:
    chapters = normalize_chapters(
        [
            RawChapter(0.0, None, "First"),
            RawChapter(10.0, None, "Second"),
            RawChapter(20.0, None, "Third"),
        ],
        duration_s=30.0,
        min_track_s=3.0,
    )
    assert [(c.start_s, c.end_s) for c in chapters] == [(0.0, 10.0), (10.0, 20.0), (20.0, 30.0)]

    out = tmp_path / "tracks"
    artifacts = split_to_alac(
        tone_triplet, chapters, album="Album", artist="Artist", out_dir=out
    )
    assert len(artifacts) == 3

    for art, expected_freq in zip(artifacts, [440.0, 660.0, 880.0], strict=True):
        assert art.codec == "alac"
        assert art.sample_rate == 48000  # native rate preserved, no resample
        assert art.duration_s == pytest.approx(10.0, abs=0.3)
        assert art.file_size > 0
        # The right tone in the right track => cuts landed on the boundaries.
        assert dominant_freq(art.path) == pytest.approx(expected_freq, abs=25.0)

    # Filenames are ordered and human-readable.
    names = sorted(p.name for p in out.iterdir())
    assert names == ["01 - First.m4a", "02 - Second.m4a", "03 - Third.m4a"]


def test_metadata_embedded(tone_triplet: Path, tmp_path: Path) -> None:
    chapters = normalize_chapters(
        [RawChapter(0.0, 10.0, "One"), RawChapter(10.0, 30.0, "Two")],
        duration_s=30.0, min_track_s=3.0,
    )
    out = tmp_path / "tracks"
    artifacts = split_to_alac(
        tone_triplet, chapters, album="My Album", artist="My Artist", out_dir=out
    )
    proc = subprocess.run(
        ["ffprobe", "-hide_banner", "-loglevel", "error", "-print_format", "json",
         "-show_format", str(artifacts[0].path)],
        capture_output=True, text=True, timeout=30,
    )
    tags = __import__("json").loads(proc.stdout)["format"]["tags"]
    assert tags["title"] == "One"
    assert tags["album"] == "My Album"
    assert tags["artist"] == "My Artist"
    assert tags["track"] == "1/2"


def test_tiny_chapters_merge(tone_triplet: Path, tmp_path: Path) -> None:
    chapters = normalize_chapters(
        [
            RawChapter(0.0, 10.0, "A"),
            RawChapter(10.0, 10.4, "blip"),   # < 3s: merged into A
            RawChapter(10.4, 20.0, "B"),
            RawChapter(20.0, 30.0, "C"),
        ],
        duration_s=30.0, min_track_s=3.0,
    )
    assert len(chapters) == 3
    assert chapters[0].end_s == pytest.approx(10.4)
    assert chapters[0].title == "A"  # survivor keeps its title


def test_untitled_chapters_numbered() -> None:
    chapters = normalize_chapters(
        [RawChapter(0.0, 5.0, ""), RawChapter(5.0, 10.0, "  ")],
        duration_s=10.0, min_track_s=3.0,
    )
    assert [c.title for c in chapters] == ["Track 1", "Track 2"]


def test_overlapping_and_out_of_range_clamped() -> None:
    chapters = normalize_chapters(
        [
            RawChapter(-5.0, 12.0, "A"),     # negative start clamped
            RawChapter(10.0, 20.0, "B"),    # overlaps A: A trimmed to 10
            RawChapter(20.0, 999.0, "C"),   # end clamped to duration
        ],
        duration_s=30.0, min_track_s=3.0,
    )
    assert [(c.start_s, c.end_s) for c in chapters] == [
        (0.0, 10.0), (10.0, 20.0), (20.0, 30.0),
    ]


def test_single_track_fallback_when_no_chapters(
    tone_triplet: Path, tmp_path: Path
) -> None:
    from app.media.splitter import SplitChapter

    chapters = [SplitChapter(0.0, 30.0, "Full video")]
    artifacts = split_to_alac(
        tone_triplet, chapters, album="A", artist="B", out_dir=tmp_path / "t"
    )
    assert len(artifacts) == 1
    assert artifacts[0].duration_s == pytest.approx(30.0, abs=0.3)
    assert artifacts[0].codec == "alac"
