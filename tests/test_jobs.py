"""Job queue unit tests: idempotency, claiming, retries, backoff."""

from __future__ import annotations

import pytest

from app import db
from app.config import Settings
from app.core import jobs as jobq

URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
YTID = "dQw4w9WgXcQ"


def test_enqueue_is_idempotent(settings: Settings) -> None:
    job1, video1, existing1 = jobq.enqueue_ingest(settings, YTID, URL)
    job2, video2, existing2 = jobq.enqueue_ingest(settings, YTID, URL)
    assert not existing1
    assert existing2
    assert job1["id"] == job2["id"]
    assert video1["id"] == video2["id"]
    with db.connect(settings) as conn:
        n = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        assert n == 1


def test_enqueue_different_urls_queue_separately(settings: Settings) -> None:
    jobq.enqueue_ingest(settings, YTID, URL)
    jobq.enqueue_ingest(settings, "aaaaaaaaaaa", "https://youtu.be/aaaaaaaaaaa")
    with db.connect(settings) as conn:
        n = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        assert n == 2


def test_claim_moves_to_running_and_increments_attempt(settings: Settings) -> None:
    job, _, _ = jobq.enqueue_ingest(settings, YTID, URL)
    claimed = jobq.claim_next_job(settings)
    assert claimed is not None
    assert claimed["id"] == job["id"]
    assert claimed["status"] == "running"
    assert claimed["attempt"] == 1
    assert jobq.claim_next_job(settings) is None  # nothing left queued


def test_claim_skips_not_yet_due_retries(settings: Settings) -> None:
    job, _, _ = jobq.enqueue_ingest(settings, YTID, URL)
    jobq.claim_next_job(settings)
    jobq.fail_job(settings, job["id"], "TRANSIENT_FAILURE", "boom", retryable=True)
    requeued = jobq.get_job(settings, job["id"])
    assert requeued is not None
    assert requeued["status"] == "queued"
    assert requeued["next_retry_at"] is not None
    # Retry is scheduled ~0.01s out (test settings); not claimable before then
    # only if we force the clock — here just verify a far-future retry is skipped.
    with db.connect(settings) as conn:
        conn.execute(
            "UPDATE jobs SET next_retry_at = '2999-01-01T00:00:00+00:00' WHERE id = ?",
            (job["id"],),
        )
    assert jobq.claim_next_job(settings) is None


def test_fail_exhausts_attempts_to_failed(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Zero backoff so requeued jobs are immediately claimable in the loop.
    monkeypatch.setattr(jobq, "compute_retry_delay_s", lambda *a: 0.0)
    job, video, _ = jobq.enqueue_ingest(settings, YTID, URL)
    for _ in range(settings.job_max_attempts):
        claimed = jobq.claim_next_job(settings)
        assert claimed is not None
        jobq.fail_job(settings, claimed["id"], "TRANSIENT_FAILURE", "x", retryable=True)
    final = jobq.get_job(settings, job["id"])
    assert final is not None
    assert final["status"] == "failed"
    assert final["attempt"] == settings.job_max_attempts
    assert jobq.get_video(settings, video["id"])["status"] == "failed"


def test_permanent_failure_does_not_retry(settings: Settings) -> None:
    job, _, _ = jobq.enqueue_ingest(settings, YTID, URL)
    jobq.claim_next_job(settings)
    jobq.fail_job(settings, job["id"], "VIDEO_UNAVAILABLE", "gone", retryable=False)
    final = jobq.get_job(settings, job["id"])
    assert final is not None
    assert final["status"] == "failed"
    assert final["attempt"] == 1


def test_complete_job(settings: Settings) -> None:
    job, _, _ = jobq.enqueue_ingest(settings, YTID, URL)
    jobq.claim_next_job(settings)
    jobq.update_job(settings, job["id"], stage="split", progress=0.5)
    mid = jobq.get_job(settings, job["id"])
    assert mid is not None and mid["stage"] == "split" and mid["progress"] == 0.5
    jobq.complete_job(settings, job["id"])
    done = jobq.get_job(settings, job["id"])
    assert done is not None and done["status"] == "done" and done["progress"] == 1.0


def test_retry_backoff_grows_and_caps() -> None:
    d1 = jobq.compute_retry_delay_s(1, 30.0, 1800.0)
    d2 = jobq.compute_retry_delay_s(2, 30.0, 1800.0)
    d10 = jobq.compute_retry_delay_s(10, 30.0, 1800.0)
    assert 22.0 <= d1 <= 38.0      # ~30s ± jitter
    assert 45.0 <= d2 <= 75.0      # ~60s ± jitter
    assert d10 <= 1800.0 * 1.25    # capped
    assert d2 > d1                 # grows


def test_youtube_id_extraction_variants() -> None:
    from app.adapters.youtube import extract_youtube_id

    assert extract_youtube_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == YTID
    assert extract_youtube_id("https://youtu.be/dQw4w9WgXcQ") == YTID
    assert extract_youtube_id("https://www.youtube.com/shorts/dQw4w9WgXcQ") == YTID
    assert extract_youtube_id("https://www.youtube.com/embed/dQw4w9WgXcQ") == YTID
    assert extract_youtube_id(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s") == YTID
    assert extract_youtube_id("https://music.youtube.com/watch?v=dQw4w9WgXcQ") == YTID
    assert extract_youtube_id("https://example.com/not-youtube") is None
    assert extract_youtube_id("not a url") is None


def test_classify_error() -> None:
    from app.adapters.youtube import classify_error

    assert classify_error("ERROR: Video unavailable") == ("VIDEO_UNAVAILABLE", False)
    assert classify_error("Private video") == ("VIDEO_PRIVATE", False)
    assert classify_error("HTTP Error 503: Service Unavailable") == (
        "TRANSIENT_FAILURE", True)
