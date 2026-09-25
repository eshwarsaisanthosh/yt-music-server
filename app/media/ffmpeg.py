"""Thin, shell-free wrappers around ffmpeg/ffprobe.

All subprocesses are invoked with argument arrays (never a shell) and a
bounded stderr capture. Nothing here applies filters, resampling, or
loudness changes — those are pipeline decisions, not tool defaults.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.logging import get_logger

log = get_logger(__name__)


class FFmpegError(Exception):
    def __init__(self, args: list[str], returncode: int, stderr: str) -> None:
        self.args = args
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"ffmpeg exited {returncode}: {stderr[-500:]}")


@dataclass
class StreamInfo:
    duration_s: float
    codec: str
    sample_rate: int
    channels: int


def check_tools() -> dict[str, str]:
    """Fail fast at startup if ffmpeg/ffprobe are missing."""
    missing = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
    if missing:
        raise RuntimeError(f"missing required tools: {', '.join(missing)}")
    versions = {}
    for tool in ("ffmpeg", "ffprobe"):
        out = subprocess.run(
            [tool, "-version"], capture_output=True, text=True, timeout=15
        ).stdout
        versions[tool] = out.splitlines()[0] if out else "unknown"
    log.info("media tools ready", extra=versions)
    return versions


def run(args: list[str], timeout_s: int = 3600) -> None:
    """Run an ffmpeg command; raise FFmpegError with diagnostics on failure."""
    log.debug("ffmpeg run", extra={"args": " ".join(args)})
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout_s)
    if proc.returncode != 0:
        raise FFmpegError(args, proc.returncode, proc.stderr or proc.stdout)


def probe(path: Path) -> StreamInfo:
    """Return duration/codec/sample-rate/channels of the first audio stream."""
    proc = subprocess.run(
        [
            "ffprobe", "-hide_banner", "-loglevel", "error",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(path),
        ],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise FFmpegError(["ffprobe", str(path)], proc.returncode, proc.stderr)
    data = json.loads(proc.stdout or "{}")
    audio = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None
    )
    if audio is None:
        raise FFmpegError(["ffprobe", str(path)], 0, "no audio stream found")
    duration = float(data.get("format", {}).get("duration") or audio.get("duration") or 0)
    return StreamInfo(
        duration_s=duration,
        codec=str(audio.get("codec_name") or ""),
        sample_rate=int(audio.get("sample_rate") or 0),
        channels=int(audio.get("channels") or 0),
    )
