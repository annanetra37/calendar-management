"""Typed Google Calendar client (T-06, T-07, T-08).

Colour, formatting and idempotency policy live here so the rest of the app
never hand-rolls an event body.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

log = logging.getLogger(__name__)

API_ROOT = "https://www.googleapis.com/calendar/v3"

#: Google event colours. Verified against the standard event palette.
COLOR_CONFIRMED = "10"  # Basil — green
COLOR_SUGGESTED = "5"  # Banana — yellow

TENTATIVE_PREFIX = "[TENTATIVE] "
APP_TAG = "voice-calendar"

RETRYABLE_STATUS = frozenset({403, 429, 500, 502, 503, 504})
RETRYABLE_403_REASONS = frozenset(
    {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "backendError"}
)


class CalendarError(RuntimeError):
    """Any failure talking to the Calendar API."""

    def __init__(self, message: str, *, status: int | None = None, reason: str | None = None):
        super().__init__(message)
        self.status = status
        self.reason = reason


class CalendarNotFound(CalendarError):
    """The event is already gone (404/410). Usually benign."""


class CalendarAuthError(CalendarError):
    """401/403 that is not a rate limit — the token needs attention."""


class SyncTokenExpired(CalendarError):
    """410 on an incremental list: drop the token and do a full sync."""


@dataclass(slots=True)
class EventRef:
    event_id: str
    etag: str | None
    html_link: str | None = None
    status: str | None = None


def _sleep_with_jitter(attempt: int, base: float = 0.6, cap: float = 12.0) -> None:
    delay = min(cap, base * (2 ** (attempt - 1)))
    time.sleep(delay * (0.5 + random.random() / 2))


class CalendarClient:
    """Thin, retrying wrapper over the Calendar v3 REST API.

    ``token_provider`` returns a currently-valid access token; it is called
    again after a 401 so a mid-flight expiry heals itself.
    """

    def __init__(
        self,
        token_provider: Callable[[bool], str],
        calendar_id: str = "primary",
        *,
        client: httpx.Client | None = None,
        max_attempts: int = 5,
        sleeper: Callable[[int], None] = _sleep_with_jitter,
    ) -> None:
        self._token_provider = token_provider
        self.calendar_id = calendar_id
        self._client = client or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))
        self._owns_client = client is None
        self._max_attempts = max_attempts
        self._sleep = sleeper

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> CalendarClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- transport ------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response | None:
        url = f"{API_ROOT}{path}"
        force_refresh = False
        last_error: CalendarError | None = None

        for attempt in range(1, self._max_attempts + 1):
            token = self._token_provider(force_refresh)
            headers = {"Authorization": f"Bearer {token}", **kwargs.pop("headers", {})}
            try:
                response = self._client.request(method, url, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                last_error = CalendarError(f"Network error talking to Google: {exc}")
                log.warning("gcal_network_error", extra={"attempt": attempt, "path": path})
                if attempt < self._max_attempts:
                    self._sleep(attempt)
                    continue
                raise last_error from exc

            if response.status_code < 400:
                return response

            reason = _error_reason(response)
            log.warning(
                "gcal_error_response",
                extra={
                    "attempt": attempt,
                    "status": response.status_code,
                    "reason": reason,
                    "method": method,
                    "path": path,
                },
            )

            if response.status_code in (404, 410):
                if response.status_code == 410 and "syncToken" in str(kwargs.get("params", "")):
                    raise SyncTokenExpired("Sync token expired; full resync required.", status=410)
                raise CalendarNotFound(
                    "That calendar event no longer exists.", status=response.status_code
                )

            if response.status_code == 401 and not force_refresh:
                force_refresh = True
                continue

            retryable = response.status_code in RETRYABLE_STATUS and (
                response.status_code != 403 or reason in RETRYABLE_403_REASONS
            )
            if retryable and attempt < self._max_attempts:
                self._sleep(attempt)
                continue

            if response.status_code in (401, 403) and reason not in RETRYABLE_403_REASONS:
                raise CalendarAuthError(
                    "Google refused the request — the calendar authorisation may "
                    "need renewing.",
                    status=response.status_code,
                    reason=reason,
                )
            raise CalendarError(
                f"Google Calendar returned {response.status_code} ({reason or 'no reason'}).",
                status=response.status_code,
                reason=reason,
            )

        raise last_error or CalendarError("Google Calendar request failed.")

    # -- events ---------------------------------------------------------
    def create_event(self, body: dict[str, Any], *, send_updates: str = "none") -> EventRef:
        response = self._request(
            "POST",
            f"/calendars/{self.calendar_id}/events",
            params={"sendUpdates": send_updates, "supportsAttachments": "false"},
            json=body,
        )
        assert response is not None
        return _to_ref(response.json())

    def get_event(self, event_id: str) -> dict[str, Any]:
        response = self._request("GET", f"/calendars/{self.calendar_id}/events/{event_id}")
        assert response is not None
        return response.json()

    def update_event(
        self, event_id: str, body: dict[str, Any], *, send_updates: str = "none"
    ) -> EventRef:
        response = self._request(
            "PATCH",
            f"/calendars/{self.calendar_id}/events/{event_id}",
            params={"sendUpdates": send_updates},
            json=body,
        )
        assert response is not None
        return _to_ref(response.json())

    def patch_color(self, event_id: str, color_id: str) -> EventRef:
        return self.update_event(event_id, {"colorId": color_id})

    def delete_event(self, event_id: str, *, send_updates: str = "none") -> bool:
        """Delete an event. Returns False when it was already gone."""
        try:
            self._request(
                "DELETE",
                f"/calendars/{self.calendar_id}/events/{event_id}",
                params={"sendUpdates": send_updates},
            )
        except CalendarNotFound:
            return False
        return True

    def list_events(
        self,
        *,
        time_min: datetime | None = None,
        time_max: datetime | None = None,
        private_extended_property: str | None = None,
        sync_token: str | None = None,
        show_deleted: bool = False,
        max_results: int = 250,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Return (events, next_sync_token), following pagination."""
        params: dict[str, Any] = {"maxResults": max_results, "singleEvents": "true"}
        if sync_token:
            params["syncToken"] = sync_token
            params["showDeleted"] = "true"
        else:
            if time_min:
                params["timeMin"] = _rfc3339(time_min)
            if time_max:
                params["timeMax"] = _rfc3339(time_max)
            params["showDeleted"] = "true" if show_deleted else "false"
            params["orderBy"] = "startTime"
        if private_extended_property:
            params["privateExtendedProperty"] = private_extended_property

        items: list[dict[str, Any]] = []
        next_sync_token: str | None = None
        page_token: str | None = None
        while True:
            page_params = dict(params)
            if page_token:
                page_params["pageToken"] = page_token
            response = self._request(
                "GET", f"/calendars/{self.calendar_id}/events", params=page_params
            )
            assert response is not None
            payload = response.json()
            items.extend(payload.get("items", []))
            next_sync_token = payload.get("nextSyncToken") or next_sync_token
            page_token = payload.get("nextPageToken")
            if not page_token:
                break
        return items, next_sync_token

    def find_by_idempotency_key(self, key: str) -> dict[str, Any] | None:
        """T-08: has this exact write already landed?"""
        items, _ = self.list_events(
            private_extended_property=f"idempotency_key={key}", show_deleted=False
        )
        for item in items:
            if item.get("status") != "cancelled":
                return item
        return None

    # -- push channels (T-23) -------------------------------------------
    def watch(self, channel_id: str, address: str, token: str, ttl_seconds: int = 604800) -> dict:
        response = self._request(
            "POST",
            f"/calendars/{self.calendar_id}/events/watch",
            json={
                "id": channel_id,
                "type": "web_hook",
                "address": address,
                "token": token,
                "params": {"ttl": str(ttl_seconds)},
            },
        )
        assert response is not None
        return response.json()

    def stop_channel(self, channel_id: str, resource_id: str) -> bool:
        try:
            self._request(
                "POST", "/channels/stop", json={"id": channel_id, "resourceId": resource_id}
            )
        except (CalendarNotFound, CalendarError):
            return False
        return True


# ---------------------------------------------------------------------------
# Event bodies (T-07 / T-08)
# ---------------------------------------------------------------------------

def build_event_body(
    *,
    title: str,
    start_utc: datetime,
    end_utc: datetime,
    timezone_name: str,
    tentative: bool,
    meeting_id: int | str,
    idempotency_key: str,
    attendees: list[str] | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Build a create/update body for a placeholder or a confirmed event.

    Placeholders are yellow, private, transparent (so they never block other
    bookings), prefixed, and never carry guests (decision D3).
    """
    body: dict[str, Any] = {
        "summary": (TENTATIVE_PREFIX if tentative else "") + title,
        "start": {"dateTime": _rfc3339(start_utc), "timeZone": timezone_name},
        "end": {"dateTime": _rfc3339(end_utc), "timeZone": timezone_name},
        "colorId": COLOR_SUGGESTED if tentative else COLOR_CONFIRMED,
        "transparency": "transparent" if tentative else "opaque",
        "visibility": "private" if tentative else "default",
        "reminders": {"useDefault": not tentative},
        "extendedProperties": {
            "private": {
                "app": APP_TAG,
                "meeting_id": str(meeting_id),
                "idempotency_key": idempotency_key,
                "kind": "suggested" if tentative else "confirmed",
            }
        },
    }
    if description:
        body["description"] = description
    elif tentative:
        body["description"] = (
            "Tentative placeholder proposed by the scheduling bot. "
            "It disappears automatically once this meeting is confirmed."
        )
    if attendees and not tentative:
        body["attendees"] = [{"email": email} for email in attendees]
    return body


def promotion_patch(
    *,
    title: str,
    start_utc: datetime,
    end_utc: datetime,
    timezone_name: str,
    attendees: list[str] | None = None,
) -> dict[str, Any]:
    """T-20 step 2: turn a yellow placeholder into the green confirmed event."""
    body: dict[str, Any] = {
        "summary": title,
        "start": {"dateTime": _rfc3339(start_utc), "timeZone": timezone_name},
        "end": {"dateTime": _rfc3339(end_utc), "timeZone": timezone_name},
        "colorId": COLOR_CONFIRMED,
        "transparency": "opaque",
        "visibility": "default",
        "reminders": {"useDefault": True},
        "description": "",
        "extendedProperties": {"private": {"kind": "confirmed"}},
    }
    if attendees:
        body["attendees"] = [{"email": email} for email in attendees]
    return body


def is_app_event(event: dict[str, Any]) -> bool:
    private = (event.get("extendedProperties") or {}).get("private") or {}
    return private.get("app") == APP_TAG


def event_meeting_id(event: dict[str, Any]) -> int | None:
    private = (event.get("extendedProperties") or {}).get("private") or {}
    raw = private.get("meeting_id")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def iter_private_props(events: Iterator[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for event in events:
        yield (event.get("extendedProperties") or {}).get("private") or {}


def _to_ref(payload: dict[str, Any]) -> EventRef:
    return EventRef(
        event_id=payload["id"],
        etag=payload.get("etag"),
        html_link=payload.get("htmlLink"),
        status=payload.get("status"),
    )


def _error_reason(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    error = payload.get("error")
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        errors = error.get("errors") or []
        if errors and isinstance(errors[0], dict):
            return errors[0].get("reason")
        return error.get("status")
    return None


def _rfc3339(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
