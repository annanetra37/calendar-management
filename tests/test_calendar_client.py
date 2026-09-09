"""T-06 / T-07 / T-08: the calendar wrapper's retry, error and formatting paths."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from app.integrations.google_calendar import (
    COLOR_CONFIRMED,
    COLOR_SUGGESTED,
    TENTATIVE_PREFIX,
    CalendarAuthError,
    CalendarClient,
    CalendarError,
    CalendarNotFound,
    build_event_body,
    is_app_event,
    promotion_patch,
)

START = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)
END = datetime(2026, 9, 15, 11, 0, tzinfo=UTC)


def _client(handler, **kwargs) -> CalendarClient:
    transport = httpx.MockTransport(handler)
    return CalendarClient(
        token_provider=lambda force: "refreshed" if force else "token",
        client=httpx.Client(transport=transport),
        sleeper=lambda attempt: None,  # no real sleeping in tests
        **kwargs,
    )


def test_rate_limit_403_is_retried_then_succeeds() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(
                403,
                json={"error": {"errors": [{"reason": "rateLimitExceeded"}], "code": 403}},
            )
        return httpx.Response(200, json={"id": "evt-1", "etag": '"e1"'})

    ref = _client(handler).create_event({"summary": "x"})
    assert ref.event_id == "evt-1"
    assert attempts["n"] == 3


def test_5xx_is_retried() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 2:
            return httpx.Response(503, json={"error": {"errors": [{"reason": "backendError"}]}})
        return httpx.Response(200, json={"id": "evt-2", "etag": '"e2"'})

    assert _client(handler).create_event({}).event_id == "evt-2"
    assert attempts["n"] == 2


def test_retries_are_bounded_and_then_raise() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"errors": [{"reason": "backendError"}]}})

    with pytest.raises(CalendarError):
        _client(handler, max_attempts=3).create_event({})


def test_401_triggers_exactly_one_forced_token_refresh() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        if len(seen) == 1:
            return httpx.Response(401, json={"error": {"errors": [{"reason": "authError"}]}})
        return httpx.Response(200, json={"id": "evt-3", "etag": '"e3"'})

    assert _client(handler).create_event({}).event_id == "evt-3"
    assert seen == ["Bearer token", "Bearer refreshed"]


def test_permanent_403_becomes_an_auth_error_without_retrying() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(
            403, json={"error": {"errors": [{"reason": "insufficientPermissions"}]}}
        )

    with pytest.raises(CalendarAuthError):
        _client(handler).create_event({})
    assert attempts["n"] == 1, "a permission error must not be retried"


def test_delete_of_a_missing_event_is_not_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"errors": [{"reason": "notFound"}]}})

    assert _client(handler).delete_event("gone") is False


def test_get_of_a_missing_event_raises_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(410, json={"error": {"errors": [{"reason": "deleted"}]}})

    with pytest.raises(CalendarNotFound):
        _client(handler).get_event("gone")


def test_list_events_follows_pagination() -> None:
    pages = [
        httpx.Response(200, json={"items": [{"id": "a"}], "nextPageToken": "p2"}),
        httpx.Response(200, json={"items": [{"id": "b"}], "nextSyncToken": "sync-9"}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return pages.pop(0)

    events, token = _client(handler).list_events(time_min=START, time_max=END)
    assert [e["id"] for e in events] == ["a", "b"]
    assert token == "sync-9"


def test_network_errors_are_retried_then_surfaced() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ConnectError("boom")

    with pytest.raises(CalendarError, match="Network error"):
        _client(handler, max_attempts=3).create_event({})
    assert attempts["n"] == 3


def test_idempotency_lookup_filters_by_private_property() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(200, json={"items": [{"id": "found", "status": "confirmed"}]})

    found = _client(handler).find_by_idempotency_key("cmd-7:0")
    assert found is not None and found["id"] == "found"
    assert captured["privateExtendedProperty"] == "idempotency_key=cmd-7:0"


# --- T-07: event formatting -------------------------------------------------

def test_placeholder_body_is_yellow_private_transparent_and_prefixed() -> None:
    body = build_event_body(
        title="Evocabank sync",
        start_utc=START,
        end_utc=END,
        timezone_name="Asia/Yerevan",
        tentative=True,
        meeting_id=7,
        idempotency_key="cmd-1:0",
        attendees=["someone@example.com"],
    )
    assert body["summary"] == f"{TENTATIVE_PREFIX}Evocabank sync"
    assert body["colorId"] == COLOR_SUGGESTED
    assert body["transparency"] == "transparent"
    assert body["visibility"] == "private"
    assert "attendees" not in body, "D3: placeholders must never invite guests"
    assert body["extendedProperties"]["private"]["idempotency_key"] == "cmd-1:0"
    assert body["extendedProperties"]["private"]["meeting_id"] == "7"
    assert is_app_event(body)


def test_confirmed_body_is_green_opaque_and_unprefixed() -> None:
    body = build_event_body(
        title="Evocabank sync",
        start_utc=START,
        end_utc=END,
        timezone_name="Asia/Yerevan",
        tentative=False,
        meeting_id=7,
        idempotency_key="cmd-1:0",
        attendees=["client@example.com"],
    )
    assert body["summary"] == "Evocabank sync"
    assert body["colorId"] == COLOR_CONFIRMED
    assert body["transparency"] == "opaque"
    assert body["visibility"] == "default"
    assert body["attendees"] == [{"email": "client@example.com"}]


def test_promotion_patch_strips_the_prefix_and_goes_green() -> None:
    patch = promotion_patch(
        title="Evocabank sync",
        start_utc=START,
        end_utc=END,
        timezone_name="Asia/Yerevan",
    )
    assert patch["summary"] == "Evocabank sync"
    assert not patch["summary"].startswith(TENTATIVE_PREFIX)
    assert patch["colorId"] == COLOR_CONFIRMED
    assert patch["transparency"] == "opaque"
    assert patch["visibility"] == "default"


def test_times_are_serialised_as_utc_rfc3339() -> None:
    body = build_event_body(
        title="x",
        start_utc=START,
        end_utc=END,
        timezone_name="Asia/Yerevan",
        tentative=False,
        meeting_id=1,
        idempotency_key="k",
    )
    assert body["start"]["dateTime"] == "2026-09-15T10:00:00Z"
    assert body["start"]["timeZone"] == "Asia/Yerevan"
