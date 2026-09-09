"""User lookup, the allow-list and per-user rate limits (T-09, T-28)."""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import User, VoiceCommand, utcnow

log = logging.getLogger(__name__)


class NotAllowed(PermissionError):
    """The Telegram user is not on the allow-list."""


class RateLimited(RuntimeError):
    """Too many commands in the last hour."""


def is_allowed(telegram_user_id: int) -> bool:
    allowed = get_settings().allowed_telegram_ids
    if not allowed:
        # Fail closed: an empty allow-list means nobody, not everybody.
        log.error("allowlist_empty_denying_all", extra={"telegram_user_id": telegram_user_id})
        return False
    return telegram_user_id in allowed


def get_or_create_user(session: Session, telegram_user_id: int) -> User:
    settings = get_settings()
    user = session.scalar(select(User).where(User.telegram_user_id == telegram_user_id))
    if user is None:
        user = User(
            telegram_user_id=telegram_user_id,
            home_timezone=settings.default_timezone,
            default_duration_minutes=settings.default_duration_minutes,
            google_calendar_id="primary",
        )
        session.add(user)
        session.flush()
        log.info("user_created", extra={"user_id": user.id})
    return user


def check_rate_limit(session: Session, user: User) -> None:
    settings = get_settings()
    cutoff = utcnow() - timedelta(hours=1)
    count = session.scalar(
        select(func.count(VoiceCommand.id)).where(
            VoiceCommand.user_id == user.id, VoiceCommand.created_at >= cutoff
        )
    )
    if (count or 0) >= settings.commands_per_hour:
        raise RateLimited(
            f"That is more than {settings.commands_per_hour} commands in an hour. "
            "Give it a few minutes."
        )
