# yt-music-server

Private backend for a personal iOS music player. Paste a YouTube URL, and the
server downloads the best audio, splits it into tracks using the video's
chapter markers, encodes each track to **ALAC** (Apple Lossless), and serves
a small REST API the iOS app will use for library, playlists, and streaming.

This is step 1 of the project (backend). The SwiftUI client comes later.

## How it works

```
iPhone (later)                    this server
   │                                  │
   │  POST /v1/ingest {url}           │  worker pipeline (separate process)
   │ ───────────────────────────────▶ │ ──▶ intake → metadata → download
   │  202 {job_id}                    │         → chapter-split → index
   │ ◀─────────────────────────────── │         → cleanup
   │                                  │
   │  GET /v1/jobs/{id}  (poll)       │  SQLite (WAL) + media files on disk
   │  GET /v1/tracks/{id}/stream      │  data/media/<youtube_id>/01 - Title.m4a
```

* **API** (`uvicorn app.main:app`) — FastAPI, validates input, enqueues jobs,
  serves the library and byte-range audio streams.
* **Worker** (`python -m app.worker`) — single sequential worker that claims
  jobs from the `jobs` table and runs the pipeline. Retries use exponential
  backoff with jitter; every stage is idempotent so a retried job can safely
  re-run from the top.
* **Idempotency** — one `videos` row per YouTube ID (`UNIQUE`). Re-submitting
  the same URL returns the existing job/video instead of queueing new work.

## Audio quality rationale

The requirement is *highest quality*, so the pipeline is deliberately simple:

1. **Source**: `bestaudio[acodec=opus]/bestaudio/best` — the best audio
   YouTube serves is Opus at ~160 kbps. We take that, nothing lower.
2. **One generation only**: each chapter is decoded from the Opus source
   exactly once and encoded straight to **ALAC** (Apple Lossless in `.m4a`,
   which iOS plays natively). There is intentionally **no lossy-to-lossy
   step** — no AAC target. Transcoding Opus → AAC would discard information
   twice for zero audible benefit.
3. **No processing**: native sample rate and channel count are preserved
   (no `-ar`, `-ac`, no resampling), no loudness normalization, no filters.
   What YouTube served is what you get, bit-for-bit through a lossless codec.
4. **Sample-accurate splits**: each chapter is cut with an accurate seek
   (`-ss`/`-t` as input options — sample-accurate for audio) and re-encoded.
   `-c copy` is never used on the lossy input because packet-boundary cuts
   are not sample-accurate.
5. **Validation**: every track is probed after encoding — codec must be
   `alac`, sample rate/channels must match the source, duration must match
   the chapter within tolerance, no zero-byte files. Anything else fails the
   job loudly instead of publishing a bad track.
6. **Metadata**: track title = chapter title, album = video title,
   artist = uploader, track numbers embedded as tags.

Trade-off: ALAC files are ~5× larger than the 160 kbps Opus source
(roughly 700–900 kbps for 48 kHz stereo). For a personal library this is the
right call — storage is cheap, re-downloads are not.

Videos with no usable chapters become a single full-length track flagged
`needs_review: true`, for a future manual split editor.

## Run locally

Requirements: Python 3.11+, `ffmpeg`/`ffprobe` on `PATH`.

```bash
cd yt-music-server
python3 -m venv .venv && .venv/bin/pip install .
cp .env.example .env   # optional; defaults work

# terminal 1 — API
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000

# terminal 2 — worker
.venv/bin/python -m app.worker
```

Then:

```bash
curl -X POST localhost:8000/v1/ingest \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://www.youtube.com/watch?v=..."}'
# → {"job_id": "job_...", "status": "queued", ...}

curl localhost:8000/v1/jobs/job_...   # poll stage/progress
curl localhost:8000/v1/videos         # library
```

## Run with docker

```bash
cp .env.example .env
docker compose up --build
```

Two services share `./data` (SQLite DB + media library + staging):
`api` on port 8000, `worker` running the pipeline. Set `YTM_API_TOKEN` in
`.env` to require a bearer token on all `/v1/*` endpoints.

> The Dockerfile was not build-tested in the sandbox (no docker daemon
> available); it follows the standard `python:3.11-slim` + apt ffmpeg pattern.

## API overview (`/v1`)

| Method & path | Purpose |
|---|---|
| `POST /v1/ingest` | Queue a YouTube URL. Body: `{url, playlist_id?}`. Always `202` + `job_id`; `existing: true` when the URL was already ingested/queued. |
| `GET /v1/jobs/{id}` | Job status: `status` (queued/running/done/failed), `stage`, `progress` 0–1, `attempt`, `error_code`/`error_message`. |
| `GET /v1/videos` | Library list (track counts, no embedded tracks). |
| `GET /v1/videos/{id}` | Video detail with ordered tracks. |
| `GET /v1/tracks/{id}/stream` | Audio bytes. Supports `Range: bytes=N-M` → `206 Partial Content` (required by AVPlayer); full `200` otherwise; `416` for bad ranges. |
| `GET /v1/playlists` / `POST /v1/playlists` | List / create (`{name}` → `201`). |
| `GET /v1/playlists/{id}` | Playlist with ordered tracks. |
| `PATCH /v1/playlists/{id}` | Rename (`{name}`). |
| `DELETE /v1/playlists/{id}` | Delete. |
| `POST /v1/playlists/{id}/tracks` | Add track (`{track_id, position?}`; appends by default; `409` on duplicates). |
| `DELETE /v1/playlists/{id}/tracks/{track_id}` | Remove track (positions compacted). |
| `GET /v1/search?q=` | Substring search across tracks, videos, playlists. |
| `GET /healthz` | Liveness + checks: DB, media writability, worker heartbeat. No auth. |

Error shape: `{code: "SNAKE_CASE", message: "..."}` with an appropriate HTTP
status. Stable job error codes include `VIDEO_UNAVAILABLE`, `VIDEO_PRIVATE`,
`AUTH_REQUIRED` (YouTube bot check), `DURATION_EXCEEDED`, `STORAGE_FULL`,
`NO_AUDIO_STREAM`, and `TRANSIENT_FAILURE` (retried with backoff).

## Configuration

All settings are env vars with the `YTM_` prefix (see `.env.example`):

| Var | Default | Meaning |
|---|---|---|
| `YTM_DATA_ROOT` | `./data` | DB + `media/` + `staging/` live here (`/data` in docker). |
| `YTM_API_TOKEN` | _(unset)_ | When set, all `/v1/*` need `Authorization: Bearer <token>`. Set it unless you're on a trusted LAN. |
| `YTM_JOB_MAX_ATTEMPTS` | `5` | Total tries before a job is marked failed. |
| `YTM_RETRY_BASE_DELAY_S` / `YTM_RETRY_MAX_DELAY_S` | `30` / `1800` | Exponential backoff with ±25% jitter. |
| `YTM_MAX_VIDEO_DURATION_S` | `21600` | Videos longer than 6h are rejected. |
| `YTM_MIN_TRACK_DURATION_S` | `3.0` | Shorter chapters are merged into a neighbor. |
| `YTM_YT_DLP_NO_CHECK_CERTIFICATE` | `false` | Only for networks with TLS-intercepting proxies. |

## Tests

```bash
.venv/bin/pip install ".[test]"
.venv/bin/python -m pytest tests/ -q
```

* `test_splitter.py` — ffmpeg-generated 30 s Opus fixture (three 10 s tones
  at 440/660/880 Hz): asserts three ALAC tracks, durations ≈ 10 s, native
  48 kHz preserved, and uses FFT on decoded PCM to prove each track contains
  the right tone (sample-accurate cuts). Also covers chapter merging,
  clamping, numbering, and metadata tags. No network.
* `test_jobs.py` — idempotent enqueue, atomic claim, retry/backoff math,
  permanent vs transient failures, URL-variant parsing.
* `test_api.py` — full HTTP surface via TestClient: ingest, jobs, videos,
  playlists CRUD, search, byte-range streaming (200/206/416), auth.

## Project layout

```
app/
  main.py            FastAPI factory, error shape, router wiring
  config.py          pydantic-settings (YTM_* env vars)
  db.py              SQLite WAL + forward-only migrations
  logging.py         JSON structured logging
  api/
    deps.py          settings injection + optional bearer auth
    schemas.py       pydantic request/response models
    routes/          ingest, jobs, videos, tracks (range stream),
                     playlists, library (search), health
  core/
    jobs.py          DB-backed queue: enqueue (idempotent), atomic claim,
                     retries/backoff, heartbeat
    ids.py           prefixed unique IDs (job_…, vid_…, trk_…, pl_…)
  adapters/
    youtube.py       URL→video-ID parsing, yt-dlp metadata + audio download,
                     error classification (retryable vs permanent)
  media/
    ffmpeg.py        shell-free ffmpeg/ffprobe wrappers, probing
    splitter.py      chapter normalization + sample-accurate ALAC splitting
  worker/
    pipeline.py      stage runner: metadata → download → split → index
    __main__.py      worker loop (claim → run → heartbeat)
tests/               splitter / queue / API tests (all passing)
Dockerfile, compose.yaml, .env.example
```

## Limits & upgrade paths

* **Single worker, sequential jobs** — fine for one person. For parallel
  workers, add a lease column + heartbeat to the claim query (it's already
  atomic), or swap the `jobs` table for RQ/Celery+Redis. The iOS contract
  doesn't change.
* **YouTube bot checks** — YouTube rate-limits datacenter IPs with "Sign in
  to confirm you're not a bot". From a home IP this is rare. If it happens,
  the job fails permanently with `AUTH_REQUIRED` and a clear message rather
  than retrying forever. Deliberately, there is no cookie support: browser
  cookies are high-value credentials and don't belong in this pipeline.
* **Chapter quality** — splits are only as good as the video's chapters.
  Chapter-less videos are kept whole and flagged `needs_review` for the
  future manual split editor (Phase 2 in the arch doc).
* **No loudness normalization, no resampling, no DRC** — by design (see
  audio rationale). If you ever want ReplayGain-style playback adjustment,
  do it at playback time in the iOS app, never by rewriting the files.
