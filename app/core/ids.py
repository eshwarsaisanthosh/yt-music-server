"""Human-friendly unique IDs: <prefix>_<16 hex chars>."""

from __future__ import annotations

import uuid


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"
