"""Worker entrypoint: claim jobs from the DB queue and run the pipeline.

Single worker, sequential jobs — deliberate for personal scale. If parallel
workers are ever needed, add a lease column and heartbeat per claim; the
claim query is already atomic.
"""

from __future__ import annotations

import signal
import time

from app import db
from app.config import get_settings
from app.core import jobs as jobq
from app.logging import configure, get_logger
from app.media import ffmpeg
from app.worker.pipeline import run_job

log = get_logger(__name__)

_stop = False


def _handle_signal(signum, _frame) -> None:  # noqa: ANN001, ANN202
    global _stop
    log.info("shutdown requested", extra={"signal": signum})
    _stop = True


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    settings = get_settings()
    configure(settings.log_level)
    settings.ensure_dirs()
    db.init_db(settings)
    ffmpeg.check_tools()
    log.info("worker started", extra={"poll_s": settings.worker_poll_interval_s})

    idle_cycles = 0
    while not _stop:
        try:
            job = jobq.claim_next_job(settings)
            if job is None:
                idle_cycles += 1
                if idle_cycles % 15 == 0:  # ~every 30s at default poll
                    jobq.heartbeat(settings)
                time.sleep(settings.worker_poll_interval_s)
                continue
            idle_cycles = 0
            jobq.heartbeat(settings)
            run_job(settings, job["id"])
        except Exception:  # noqa: BLE001 - worker must never die on a bad job
            log.exception("worker loop error")
            time.sleep(settings.worker_poll_interval_s)

    log.info("worker stopped")


if __name__ == "__main__":
    main()
