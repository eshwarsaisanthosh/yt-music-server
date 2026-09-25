"""Shared API dependencies: settings injection and optional bearer auth."""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException

from app.config import Settings, get_settings


def settings_dep() -> Settings:
    return get_settings()


async def require_auth(
    settings: Settings = Depends(settings_dep),
    authorization: str | None = Header(default=None),
) -> None:
    """Enforce the bearer token when one is configured.

    With no token configured the API is open — only acceptable on a trusted
    private network, which is the documented deployment assumption.
    """
    if not settings.api_token:
        return
    if authorization != f"Bearer {settings.api_token}":
        raise HTTPException(
            status_code=401,
            detail={"code": "UNAUTHORIZED", "message": "valid bearer token required"},
        )
