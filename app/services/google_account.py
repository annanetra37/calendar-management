"""Bridge between a stored ``User`` and a ready-to-use ``CalendarClient``.

Access tokens are cached in process memory until shortly before they expire;
refresh tokens live encrypted in Postgres and are only decrypted here.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from app.crypto import decrypt, encrypt
from app.integrations.google_calendar import CalendarClient
from app.integrations.google_oauth import (
    GoogleReauthRequired,
    refresh_access_token,
)
from app.models import User

log = logging.getLogger(__name__)


@dataclass(slots=True)
class _CachedToken:
    access_token: str
    expires_at: datetime


_cache: dict[int, _CachedToken] = {}
_lock = threading.Lock()


def invalidate(user_id: int) -> None:
    with _lock:
        _cache.pop(user_id, None)


def store_refresh_token(user: User, refresh_token: str) -> None:
    user.google_refresh_token_encrypted = encrypt(refresh_token)
    invalidate(user.id)


def get_refresh_token(user: User) -> str:
    if not user.google_refresh_token_encrypted:
        raise GoogleReauthRequired("This account is not connected to Google Calendar yet.")
    return decrypt(user.google_refresh_token_encrypted)


def access_token_for(
    user: User, force_refresh: bool = False, *, client: httpx.Client | None = None
) -> str:
    now = datetime.now(UTC)
    if not force_refresh:
        with _lock:
            cached = _cache.get(user.id)
        if cached and cached.expires_at > now + timedelta(seconds=60):
            return cached.access_token

    bundle = refresh_access_token(get_refresh_token(user), client=client)
    with _lock:
        _cache[user.id] = _CachedToken(
            access_token=bundle.access_token,
            expires_at=now + timedelta(seconds=bundle.expires_in),
        )
    log.info("google_token_refreshed", extra={"user_id": user.id})
    return bundle.access_token


@contextmanager
def calendar_for(user: User, *, client: httpx.Client | None = None) -> Iterator[CalendarClient]:
    calendar = CalendarClient(
        token_provider=lambda force: access_token_for(user, force, client=client),
        calendar_id=user.google_calendar_id or "primary",
        client=client,
    )
    try:
        yield calendar
    finally:
        calendar.close()
