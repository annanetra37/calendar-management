"""Google OAuth 2.0 (T-05).

Only ``calendar.events`` is requested — never the full ``calendar`` scope.
The refresh token is encrypted with Fernet before it touches the database.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v3/userinfo"

SCOPES = (
    "https://www.googleapis.com/auth/calendar.events",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
)


class GoogleAuthError(RuntimeError):
    """OAuth handshake or token refresh failed."""


class GoogleReauthRequired(GoogleAuthError):
    """The refresh token is dead; the owner must authorise again."""


@dataclass(slots=True)
class TokenBundle:
    access_token: str
    refresh_token: str | None
    expires_in: int
    scope: str


def new_state() -> str:
    return secrets.token_urlsafe(32)


def authorization_url(state: str) -> str:
    settings = get_settings()
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        # offline + consent is what actually yields a refresh token on re-auth.
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"


def exchange_code(code: str, *, client: httpx.Client | None = None) -> TokenBundle:
    settings = get_settings()
    owns = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        response = client.post(
            TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.google_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        if response.status_code >= 400:
            raise GoogleAuthError(f"Token exchange failed ({response.status_code}).")
        payload = response.json()
        return TokenBundle(
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token"),
            expires_in=int(payload.get("expires_in", 3600)),
            scope=payload.get("scope", ""),
        )
    finally:
        if owns:
            client.close()


def refresh_access_token(refresh_token: str, *, client: httpx.Client | None = None) -> TokenBundle:
    settings = get_settings()
    owns = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        response = client.post(
            TOKEN_ENDPOINT,
            data={
                "refresh_token": refresh_token,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "grant_type": "refresh_token",
            },
        )
        if response.status_code >= 400:
            body = _safe_json(response)
            error = body.get("error", "")
            if response.status_code in (400, 401) and error in (
                "invalid_grant",
                "unauthorized_client",
                "invalid_client",
            ):
                raise GoogleReauthRequired(
                    "Google access has been revoked or expired. Please re-authorise."
                )
            raise GoogleAuthError(f"Token refresh failed ({response.status_code}: {error}).")
        payload = response.json()
        return TokenBundle(
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token"),
            expires_in=int(payload.get("expires_in", 3600)),
            scope=payload.get("scope", ""),
        )
    finally:
        if owns:
            client.close()


def fetch_email(access_token: str, *, client: httpx.Client | None = None) -> str | None:
    owns = client is None
    client = client or httpx.Client(timeout=15.0)
    try:
        response = client.get(
            USERINFO_ENDPOINT, headers={"Authorization": f"Bearer {access_token}"}
        )
        if response.status_code >= 400:
            return None
        return response.json().get("email")
    finally:
        if owns:
            client.close()


def revoke(token: str, *, client: httpx.Client | None = None) -> bool:
    owns = client is None
    client = client or httpx.Client(timeout=15.0)
    try:
        response = client.post(REVOKE_ENDPOINT, data={"token": token})
        return response.status_code < 400
    finally:
        if owns:
            client.close()


def _safe_json(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
