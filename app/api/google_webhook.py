"""Google Calendar push notifications (T-23).

Google sends an empty body with metadata in headers. The notification is only
a nudge — the actual state comes from an incremental ``events.list``.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, BackgroundTasks, Header, Response

from app.config import get_settings
from app.db import session_scope
from app.integrations.telegram import TelegramClient, TelegramError
from app.logging_setup import correlation_scope
from app.services.google_account import calendar_for
from app.services.reconcile import reconcile_user, user_for_channel

log = logging.getLogger(__name__)
router = APIRouter(tags=["google"])


@router.post("/webhooks/google")
async def google_webhook(
    background: BackgroundTasks,
    x_goog_channel_id: str | None = Header(default=None),
    x_goog_resource_state: str | None = Header(default=None),
    x_goog_channel_token: str | None = Header(default=None),
    x_goog_message_number: str | None = Header(default=None),
) -> Response:
    settings = get_settings()
    expected = settings.google_webhook_token or settings.telegram_webhook_secret
    if expected and not hmac.compare_digest(x_goog_channel_token or "", expected):
        log.warning("google_webhook_bad_token", extra={"channel_id": x_goog_channel_id})
        # 200 anyway: a non-2xx makes Google retry and eventually drop the channel.
        return Response(status_code=200)

    log.info(
        "google_push_received",
        extra={
            "channel_id": x_goog_channel_id,
            "state": x_goog_resource_state,
            "message_number": x_goog_message_number,
        },
    )
    if x_goog_resource_state == "sync":
        # Handshake ping sent when the channel is created.
        return Response(status_code=200)
    if x_goog_channel_id:
        background.add_task(handle_push, x_goog_channel_id)
    return Response(status_code=200)


def handle_push(channel_id: str) -> None:
    with correlation_scope():
        notifications: list[str] = []
        chat_id: int | None = None
        try:
            with session_scope() as session:
                user = user_for_channel(session, channel_id)
                if user is None:
                    log.warning("push_unknown_channel", extra={"channel_id": channel_id})
                    return
                chat_id = user.telegram_user_id
                with calendar_for(user) as calendar:
                    report = reconcile_user(session, user, calendar)
                notifications = report.notifications
                log.info(
                    "push_reconciled",
                    extra={
                        "user_id": user.id,
                        "removed": report.slots_removed,
                        "moved": report.slots_moved,
                        "expired": report.meetings_expired,
                    },
                )
        except Exception:
            log.exception("push_reconcile_failed", extra={"channel_id": channel_id})
            return

        if notifications and chat_id:
            try:
                with TelegramClient() as telegram:
                    telegram.send_message(chat_id, "\n\n".join(notifications))
            except TelegramError:
                log.warning("push_notification_send_failed")
