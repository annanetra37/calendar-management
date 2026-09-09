"""Health and readiness endpoints (T-01, T-30)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Response
from sqlalchemy import text

from app import __version__
from app.config import get_settings
from app.db import get_engine

log = logging.getLogger(__name__)
router = APIRouter(tags=["ops"])


@router.get("/healthz")
def healthz() -> dict:
    """Liveness — must never touch the database (Railway polls it constantly)."""
    return {"status": "ok", "version": __version__, "environment": get_settings().environment}


@router.get("/readyz")
def readyz(response: Response) -> dict:
    """Readiness — verifies the database is reachable."""
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:
        log.error("readiness_failed", extra={"error": str(exc)})
        response.status_code = 503
        return {"status": "degraded", "database": "unreachable"}
    return {"status": "ok", "database": "ok"}
