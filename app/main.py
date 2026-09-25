"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import db
from app.api.deps import settings_dep
from app.api.routes import health, ingest, jobs, library, playlists, tracks, videos
from app.config import Settings, get_settings
from app.logging import configure, get_logger

log = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure(settings.log_level)
    settings.ensure_dirs()
    db.init_db(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):  # noqa: ANN001, ANN202
        log.info("api started", extra={"version": settings.app_version})
        yield
        log.info("api stopped")

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    # Tests inject an isolated Settings; production uses the cached one.
    app.dependency_overrides[settings_dep] = lambda: settings

    @app.exception_handler(Exception)
    async def unhandled(_request: Request, exc: Exception) -> JSONResponse:  # noqa: ANN001, ANN202
        log.exception("unhandled request error")
        return JSONResponse(
            status_code=500,
            content={"code": "INTERNAL_ERROR",
                     "message": "unexpected server error"},
        )

    app.include_router(health.router, tags=["ops"])
    app.include_router(ingest.router, prefix="/v1", tags=["ingest"])
    app.include_router(jobs.router, prefix="/v1", tags=["jobs"])
    app.include_router(videos.router, prefix="/v1", tags=["library"])
    app.include_router(tracks.router, prefix="/v1", tags=["library"])
    app.include_router(playlists.router, prefix="/v1", tags=["playlists"])
    app.include_router(library.router, prefix="/v1", tags=["library"])

    return app


app = create_app()
