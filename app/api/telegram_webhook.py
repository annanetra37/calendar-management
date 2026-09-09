"""Telegram webhook (T-09).

The secret header is validated on every request and the update is handed to a
background task so Telegram always gets a fast 200 — a slow webhook makes
Telegram retry and duplicate work.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

from app.bot.handlers import process_update
from app.config import get_settings
from app.logging_setup import new_correlation_id

log = logging.getLogger(__name__)
router = APIRouter(tags=["telegram"])


@router.post("/webhooks/telegram")
async def telegram_webhook(
    request: Request,
    background: BackgroundTasks,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict:
    settings = get_settings()
    expected = settings.telegram_webhook_secret
    provided = x_telegram_bot_api_secret_token or ""
    if not expected or not hmac.compare_digest(provided, expected):
        log.warning(
            "telegram_webhook_bad_secret",
            extra={"client": request.client.host if request.client else "?"},
        )
        raise HTTPException(status_code=403, detail="forbidden")

    try:
        update = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json") from None

    if not isinstance(update, dict):
        raise HTTPException(status_code=400, detail="invalid update")

    update["_correlation_id"] = new_correlation_id()
    log.info(
        "telegram_update_received",
        extra={
            "update_id": update.get("update_id"),
            "correlation_id": update["_correlation_id"],
            "kind": "callback_query" if "callback_query" in update else "message",
        },
    )
    background.add_task(process_update, update)
    return {"ok": True}
