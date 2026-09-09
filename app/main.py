"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import __version__
from app.api import google_auth, google_webhook, health, telegram_webhook
from app.config import get_settings
from app.logging_setup import configure_logging, correlation_scope, new_correlation_id

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        sentry_dsn=settings.sentry_dsn,
        environment=settings.environment,
    )
    log.info(
        "service_starting",
        extra={
            "version": __version__,
            "environment": settings.environment,
            "timezone": settings.default_timezone,
            "allowlist_size": len(settings.allowed_telegram_ids),
        },
    )
    if not settings.allowed_telegram_ids:
        log.error("allowlist_empty — every Telegram user will be refused")
    # Migrations run as a release step (T-33), never here: two booting
    # instances would race each other.
    yield
    log.info("service_stopping")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Voice-to-Calendar Scheduler",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if not settings.is_production else None,
        redoc_url=None,
        openapi_url="/openapi.json" if not settings.is_production else None,
    )

    @app.middleware("http")
    async def correlation_middleware(request: Request, call_next):
        with correlation_scope(request.headers.get("x-request-id") or new_correlation_id()) as cid:
            response = await call_next(request)
            response.headers["x-request-id"] = cid
            return response

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled_error", extra={"path": request.url.path})
        return JSONResponse(status_code=500, content={"detail": "internal error"})

    app.include_router(health.router)
    app.include_router(telegram_webhook.router)
    app.include_router(google_auth.router)
    app.include_router(google_webhook.router)
    return app


app = create_app()
