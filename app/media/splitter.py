"""Chapter-first audio splitting with a strict quality policy.

Quality contract (the whole point of this module):
  * The best audio YouTube serves (~160 kbps Opus) is decoded exactly once
    per output track and encoded straight to ALAC. There is deliberately no
    lossy-to-lossy step (no AAC target) — that would throw away information
    twice for zero benefit.
  * Native sample rate and channel count are preserved: no ``-ar``, no
    ``-ac``, no loudness normalization, no filters of any kind.
  * Cuts are sample-accurate: each chapter is decoded from the source with
    an accurate seek (``-ss``/``-t`` as input options, which is
    sample-accurate for audio) and re-encoded. ``-c copy`` is never used on
    the lossy input because packet-boundary cuts are not sample-accurate.

Chapter handling:
  * Chapters come from the source's own chapter metadata (trusted
    timestamps), normalized: sorted, end-filled from the next chapter's
    start, clamped to the real duration, and tiny chapters merged into a
    neighbor instead of producing sub-second tracks.
  * A video with no usable chapters becomes one full-length track and is
    flagged ``needs_review`` by the caller for future manual splitting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.adapters.youtube import Chapter as RawChapter
from app.logging import get_logger
from app.media import ffmpeg

log = get_logger(__name__)

_DURATION_TOLERANCE_S = 0.75  # container rounding slack for validation


@dataclass
class SplitChapter:
    start_s: float
    end_s: float
    title: str


@dataclass
class TrackArtifact:
    track_no: int
    title: str
    start_s: float
    duration_s: float
    path: Path          # staging location; the worker publishes it
    file_size: int
    codec: str
    sample_rate: int
    channels: int


def normalize_chapters(
    raw: list[RawChapter],
    duration_s: float,
    min_track_s: float,
) -> list[SplitChapter]:
    """Clean raw chapter metadata into validated, non-overlapping chapters.

    Steps: sort, clamp to [0, duration], resolve overlaps, fill missing ends
    from the next chapter's start (chapters are contiguous by construction),
    merge sub-minimum chapters into a neighbor instead of emitting
    sub-second tracks, and number untitled chapters.
    """
    ordered = sorted(raw, key=lambda c: c.start_s)

    # Pass 1: clamp into range and resolve overlaps / fill ends.
    segs: list[list] = []  # [start, end, title, end_was_missing]
    for ch in ordered:
        start = max(0.0, ch.start_s)
        end_missing = ch.end_s is None
        end = duration_s if end_missing else ch.end_s
        end = min(max(end, start), duration_s)
        if end <= start:
            continue
        if segs and start < segs[-1][1]:
            # Overlap: trim the previous chapter at this chapter's start.
            segs[-1][1] = start
        segs.append([start, end, ch.title.strip(), end_missing])

    # Chapters with no end run into the next chapter's start (they are
    # contiguous by construction); the last one runs to the duration.
    # Chapters with explicit ends keep them, even if that leaves a gap.
    for i in range(len(segs) - 1):
        if segs[i][3]:
            segs[i][1] = max(segs[i + 1][0], segs[i][0])
    if segs and segs[-1][3]:
        segs[-1][1] = duration_s

    # Pass 2: fold tiny chapters into a neighbor (previous preferred).
    merged: list[list] = []
    for start, end, title, _missing in segs:
        if end - start < min_track_s and merged:
            merged[-1][1] = end
            continue
        merged.append([start, end, title])
    if merged and merged[0][1] - merged[0][0] < min_track_s and len(merged) > 1:
        # First chapter is tiny: absorb it into the second (keep 2nd title).
        merged[1][0] = merged[0][0]
        merged.pop(0)

    final = [
        SplitChapter(start_s=s, end_s=e, title=t)
        for s, e, t in merged
        if e - s >= min_track_s
    ]
    for i, c in enumerate(final, 1):
        if not c.title:
            c.title = f"Track {i}"
    return final


_FILENAME_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(name: str, max_len: int = 120) -> str:
    name = _FILENAME_BAD_CHARS.sub("", name).strip().rstrip(".")
    name = re.sub(r"\s+", " ", name)
    return (name[:max_len] or "untitled").strip()


def split_to_alac(
    src: Path,
    chapters: list[SplitChapter],
    *,
    album: str,
    artist: str,
    out_dir: Path,
) -> list[TrackArtifact]:
    """Decode each chapter from ``src`` and encode to ALAC (.m4a).

    One decode+encode per chapter, straight from the source file. Raises
    FFmpegError / ValidationError on any failure.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    src_info = ffmpeg.probe(src)
    if src_info.duration_s <= 0:
        raise ValueError(f"cannot split zero-duration source: {src}")

    total = len(chapters)
    artifacts: list[TrackArtifact] = []
    for i, ch in enumerate(chapters, 1):
        dur = ch.end_s - ch.start_s
        if dur <= 0:
            raise ValueError(f"chapter {i} has non-positive duration")
        fname = f"{i:02d} - {sanitize_filename(ch.title)}.m4a"
        dst = out_dir / fname
        # -ss/-t BEFORE -i: accurate seek for audio (sample-accurate cuts),
        # fast because the demuxer seeks instead of decoding from zero.
        # No -ar/-ac/-af: native sample rate, channels, no processing.
        ffmpeg.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-ss", f"{ch.start_s:.3f}",
                "-t", f"{dur:.3f}",
                "-i", str(src),
                "-vn",
                "-c:a", "alac",
                "-metadata", f"title={ch.title}",
                "-metadata", f"album={album}",
                "-metadata", f"artist={artist}",
                "-metadata", f"track={i}/{total}",
                "-y", str(dst),
            ]
        )
        artifacts.append(_validate(dst, i, ch, src_info, total))

    # Sanity: splits must tile the source within tolerance.
    split_total = sum(a.duration_s for a in artifacts)
    if abs(split_total - src_info.duration_s) > _DURATION_TOLERANCE_S * total:
        log.warning(
            "split durations do not reconcile with source",
            extra={"split_total": round(split_total, 2),
                   "source": round(src_info.duration_s, 2)},
        )
    return artifacts


def _validate(
    dst: Path, track_no: int, ch: SplitChapter, src_info: ffmpeg.StreamInfo, total: int
) -> TrackArtifact:
    info = ffmpeg.probe(dst)
    expected = ch.end_s - ch.start_s
    problems = []
    if info.codec != "alac":
        problems.append(f"codec={info.codec}")
    if info.sample_rate != src_info.sample_rate:
        problems.append(f"sample_rate={info.sample_rate} != {src_info.sample_rate}")
    if info.channels != src_info.channels:
        problems.append(f"channels={info.channels} != {src_info.channels}")
    if abs(info.duration_s - expected) > _DURATION_TOLERANCE_S:
        problems.append(f"duration={info.duration_s:.2f} != {expected:.2f}")
    size = dst.stat().st_size
    if size == 0:
        problems.append("zero-byte output")
    if problems:
        raise ValueError(f"track {track_no} failed validation: {'; '.join(problems)}")
    log.info(
        "split track",
        extra={"track_no": track_no, "title": ch.title,
               "duration_s": round(info.duration_s, 2),
               "sample_rate": info.sample_rate, "bytes": size},
    )
    return TrackArtifact(
        track_no=track_no,
        title=ch.title,
        start_s=ch.start_s,
        duration_s=info.duration_s,
        path=dst,
        file_size=size,
        codec=info.codec,
        sample_rate=info.sample_rate,
        channels=info.channels,
    )
