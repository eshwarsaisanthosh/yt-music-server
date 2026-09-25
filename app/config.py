"""Typed, environment-backed configuration.

All settings read from the environment with the ``YTM_`` prefix (and
optionally a ``.env`` file). Instantiation validates everything up front,
so misconfiguration fails fast at startup instead of mid-job.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="YTM_", env_file=".env", extra="ignore")

    app_name: str = "yt-music-server"
    app_version: str = "0.1.0"
    log_level: str = "INFO"

    # All durable state lives under this root: library.db, media/, staging/.
    data_root: Path = Path("./data")

    # When set, every /v1/* endpoint requires `Authorization: Bearer <token>`.
    # Leave unset only on a trusted private network.
    api_token: str | None = None

    # Worker behavior
    worker_poll_interval_s: float = 2.0
    worker_heartbeat_ttl_s: float = 60.0
    job_max_attempts: int = 5
    retry_base_delay_s: float = 30.0
    retry_max_delay_s: float = 1800.0

    # Pipeline guards
    max_video_duration_s: int = 6 * 3600
    min_track_duration_s: float = 3.0
    min_free_disk_bytes: int = 500 * 1024 * 1024

    # Escape hatch for networks with TLS-intercepting proxies (common on
    # corporate/sandboxed networks). Default False: certificates are verified.
    yt_dlp_no_check_certificate: bool = False

    @property
    def db_path(self) -> Path:
        return self.data_root / "library.db"

    @property
    def media_root(self) -> Path:
        return self.data_root / "media"

    @property
    def staging_root(self) -> Path:
        return self.data_root / "staging"

    def ensure_dirs(self) -> None:
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.media_root.mkdir(parents=True, exist_ok=True)
        self.staging_root.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
