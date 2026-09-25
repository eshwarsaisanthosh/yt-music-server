"""YouTube source adapter: URL parsing, metadata extraction, audio download.

Everything here shells out to yt-dlp (Python API). No shell commands, no
cookies, no credential handling. Network failures are classified as
retryable/permanent so the worker can back off or stop cleanly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import yt_dlp

from app.logging import get_logger

if TYPE_CHECKING:
    from app.config import Settings

log = get_logger(__name__)

# Matches watch?v=, youtu.be/, /shorts/, /embed/, /live/, /v/ on youtube.com,
# music.youtube.com, and youtube-nocookie.com.
_YT_PATTERNS = [
    re.compile(r"(?:youtube\.com|youtube-nocookie\.com)/watch\?.*?v=([A-Za-z0-9_-]{11})"),
    re.compile(r"(?:youtube\.com|youtube-nocookie\.com)/shorts/([A-Za-z0-9_-]{11})"),
    re.compile(r"(?:youtube\.com|youtube-nocookie\.com)/embed/([A-Za-z0-9_-]{11})"),
    re.compile(r"(?:youtube\.com|youtube-nocookie\.com)/live/([A-Za-z0-9_-]{11})"),
    re.compile(r"(?:youtube\.com|youtube-nocookie\.com)/v/([A-Za-z0-9_-]{11})"),
    re.compile(r"youtu\.be/([A-Za-z0-9_-]{11})"),
    re.compile(r"music\.youtube\.com/watch\?.*?v=([A-Za-z0-9_-]{11})"),
]


def extract_youtube_id(url: str) -> str | None:
    """Return the 11-char video ID, or None if the URL is not a YouTube video URL."""
    url = (url or "").strip()
    for pattern in _YT_PATTERNS:
        m = pattern.search(url)
        if m:
            return m.group(1)
    return None


def canonical_url(youtube_id: str) -> str:
    return f"https://www.youtube.com/watch?v={youtube_id}"


@dataclass
class Chapter:
    start_s: float
    end_s: float | None
    title: str


@dataclass
class VideoMetadata:
    youtube_id: str
    title: str
    uploader: str
    duration_s: float
    chapters: list[Chapter] = field(default_factory=list)
    thumbnail_url: str | None = None
    webpage_url: str = ""


# Error text -> (error_code, retryable). Checked in order; first match wins.
_PERMANENT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"video unavailable", re.I), "VIDEO_UNAVAILABLE"),
    (re.compile(r"private video", re.I), "VIDEO_PRIVATE"),
    (re.compile(r"has been removed|no longer available|has been deleted", re.I), "VIDEO_REMOVED"),
    (re.compile(r"copyright", re.I), "VIDEO_BLOCKED_COPYRIGHT"),
    (re.compile(r"sign in to confirm (your age|you.re not a bot)", re.I), "AUTH_REQUIRED"),
    (re.compile(r"requested format not available|no audio", re.I), "NO_AUDIO_STREAM"),
    (re.compile(r"unsupported url|not a valid url", re.I), "UNSUPPORTED_URL"),
]


def classify_error(message: str) -> tuple[str, bool]:
    """Map a tool error message to (error_code, retryable)."""
    message = message or ""
    for pattern, code in _PERMANENT_PATTERNS:
        if pattern.search(message):
            return code, False
    return "TRANSIENT_FAILURE", True


class PermanentSourceError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _base_opts(settings: "Settings | None" = None) -> dict:
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        # Never send browser cookies; this service has none to send.
        "no_cookies": True,
    }
    if settings is not None and settings.yt_dlp_no_check_certificate:
        opts["nocheckcertificate"] = True
    return opts


def fetch_metadata(
    url: str, youtube_id: str, settings: "Settings | None" = None
) -> VideoMetadata:
    """Extract title/duration/chapters/thumbnail without downloading media."""
    opts = {**_base_opts(settings), "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001 - classified below
        code, retryable = classify_error(str(exc))
        log.warning("metadata extraction failed", extra={"youtube_id": youtube_id, "code": code})
        if not retryable:
            raise PermanentSourceError(code, str(exc)) from exc
        raise

    if not info:
        raise PermanentSourceError("METADATA_EMPTY", "yt-dlp returned no metadata")

    chapters = []
    for ch in info.get("chapters") or []:
        try:
            chapters.append(
                Chapter(
                    start_s=float(ch.get("start_time", 0) or 0),
                    end_s=float(ch["end_time"]) if ch.get("end_time") is not None else None,
                    title=str(ch.get("title") or "").strip(),
                )
            )
        except (TypeError, ValueError):
            continue

    return VideoMetadata(
        youtube_id=youtube_id,
        title=str(info.get("title") or "").strip(),
        uploader=str(info.get("uploader") or info.get("channel") or "").strip(),
        duration_s=float(info.get("duration") or 0),
        chapters=chapters,
        thumbnail_url=info.get("thumbnail"),
        webpage_url=str(info.get("webpage_url") or canonical_url(youtube_id)),
    )


ProgressHook = Callable[[float | None], None]  # fraction 0..1, or None if unknown


def download_audio(
    url: str,
    outtmpl: str,
    progress_hook: ProgressHook | None = None,
    settings: "Settings | None" = None,
) -> Path:
    """Download best audio (prefer Opus) to ``outtmpl``; return the media file path.

    ``outtmpl`` is a yt-dlp output template, e.g. ``/staging/<job>/source.%(ext)s``.
    """

    def hook(d: dict) -> None:
        if progress_hook is None or d.get("status") != "downloading":
            return
        downloaded = d.get("downloaded_bytes") or 0
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        progress_hook(downloaded / total if total else None)

    opts = {
        **_base_opts(settings),
        # Highest-quality audio YouTube serves is Opus (~160 kbps, itag 251).
        # Fall back to whatever bestaudio is available.
        "format": "bestaudio[acodec=opus]/bestaudio/best",
        "outtmpl": outtmpl,
        "continuedl": True,
        "retries": 10,
        "fragment_retries": 10,
        "progress_hooks": [hook],
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as exc:  # noqa: BLE001 - classified below
        code, retryable = classify_error(str(exc))
        log.warning("audio download failed", extra={"code": code})
        if not retryable:
            raise PermanentSourceError(code, str(exc)) from exc
        raise

    # yt-dlp fills in %(ext)s; find the produced file.
    parent = Path(outtmpl).parent
    candidates = sorted(
        (p for p in parent.iterdir() if p.is_file() and not p.name.endswith((".part", ".ytdl"))),
        key=lambda p: p.stat().st_size,
        reverse=True,
    )
    if not candidates:
        raise PermanentSourceError("DOWNLOAD_EMPTY", "yt-dlp finished but produced no media file")
    downloaded = candidates[0]
    log.info("downloaded audio", extra={"path": str(downloaded), "bytes": downloaded.stat().st_size})
    return downloaded
