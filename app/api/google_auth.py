"""Google OAuth routes (T-05)."""

from __future__ import annotations

import logging
from datetime import UTC, timedelta

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete

from app.db import session_scope
from app.integrations.google_oauth import (
    GoogleAuthError,
    authorization_url,
    exchange_code,
    fetch_email,
    new_state,
)
from app.integrations.telegram import TelegramClient, TelegramError
from app.models import OAuthState, utcnow
from app.services.google_account import calendar_for, invalidate, store_refresh_token
from app.services.reconcile import ensure_watch_channel
from app.services.users import get_or_create_user, is_allowed

log = logging.getLogger(__name__)
router = APIRouter(tags=["auth"])

STATE_TTL = timedelta(minutes=15)


@router.get("/auth/google/start")
def auth_start(telegram_user_id: int = Query(..., description="Telegram user id to link")):
    if not is_allowed(telegram_user_id):
        raise HTTPException(status_code=403, detail="not on the allow-list")
    state = new_state()
    with session_scope() as session:
        session.execute(delete(OAuthState).where(OAuthState.created_at < utcnow() - STATE_TTL))
        session.add(OAuthState(state=state, telegram_user_id=telegram_user_id))
    return RedirectResponse(authorization_url(state), status_code=302)


@router.get("/auth/google/callback", response_class=HTMLResponse)
def auth_callback(
    state: str = Query(default=""),
    code: str = Query(default=""),
    error: str = Query(default=""),
):
    if error:
        return _page("Authorisation cancelled", f"Google returned: {error}", ok=False)
    if not state or not code:
        return _page("Missing parameters", "The callback was incomplete.", ok=False)

    with session_scope() as session:
        record = session.get(OAuthState, state)
        if record is None:
            return _page(
                "Link expired",
                "That authorisation link was already used or is unknown. "
                "Send /connect to the bot for a fresh one.",
                ok=False,
            )
        if utcnow() - _aware(record.created_at) > STATE_TTL:
            session.delete(record)
            return _page("Link expired", "Send /connect to the bot for a fresh one.", ok=False)
        telegram_user_id = record.telegram_user_id
        session.delete(record)

    try:
        bundle = exchange_code(code)
    except GoogleAuthError as exc:
        log.error("oauth_exchange_failed", extra={"error": str(exc)})
        return _page("Could not complete authorisation", str(exc), ok=False)

    if not bundle.refresh_token:
        return _page(
            "No refresh token issued",
            "Google did not return a refresh token. Remove this app at "
            "myaccount.google.com/permissions and authorise again.",
            ok=False,
        )

    email = fetch_email(bundle.access_token)

    with session_scope() as session:
        user = get_or_create_user(session, telegram_user_id)
        store_refresh_token(user, bundle.refresh_token)
        user.google_email = email or user.google_email
        invalidate(user.id)
        user_id = user.id
        try:
            with calendar_for(user) as calendar:
                ensure_watch_channel(session, user, calendar, force=True)
        except Exception:
            log.exception("watch_channel_setup_failed", extra={"user_id": user_id})

    log.info("google_connected", extra={"user_id": user_id})
    try:
        with TelegramClient() as telegram:
            telegram.send_message(
                telegram_user_id,
                f"✅ Google Calendar connected{f' as {email}' if email else ''}.\n"
                "Send me a voice note whenever you like — /help for examples.",
            )
    except TelegramError:
        log.warning("connect_notification_failed")

    return _page(
        "Calendar connected",
        f"{email or 'Your account'} is linked. You can close this tab and go back to Telegram.",
        ok=True,
    )


def _page(title: str, body: str, *, ok: bool) -> HTMLResponse:
    colour = "#137333" if ok else "#b3261e"
    icon = "✅" if ok else "⚠️"
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title></head>
<body style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:0;
display:flex;min-height:100vh;align-items:center;justify-content:center;background:#fafafa">
<main style="max-width:32rem;padding:2rem;text-align:center">
<div style="font-size:3rem">{icon}</div>
<h1 style="color:{colour};font-size:1.4rem;margin:.5rem 0">{title}</h1>
<p style="color:#444;line-height:1.6">{body}</p>
</main></body></html>"""
    return HTMLResponse(html, status_code=200 if ok else 400)


def _aware(value):

    return value if value.tzinfo else value.replace(tzinfo=UTC)
