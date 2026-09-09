"""T-01 / T-09 / T-23: HTTP surface, webhook authentication and OAuth."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(monkeypatch) -> TestClient:
    return TestClient(create_app(), raise_server_exceptions=False)


def test_healthz_is_200_and_touches_nothing(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_healthz_carries_a_correlation_id(client: TestClient) -> None:
    assert client.get("/healthz").headers.get("x-request-id")


def test_telegram_webhook_rejects_a_missing_secret(client: TestClient) -> None:
    response = client.post("/webhooks/telegram", json={"update_id": 1})
    assert response.status_code == 403


def test_telegram_webhook_rejects_a_wrong_secret(client: TestClient) -> None:
    response = client.post(
        "/webhooks/telegram",
        json={"update_id": 1},
        headers={"X-Telegram-Bot-Api-Secret-Token": "not-the-secret"},
    )
    assert response.status_code == 403


def test_telegram_webhook_accepts_the_right_secret(client: TestClient, monkeypatch) -> None:
    seen: list[dict] = []
    monkeypatch.setattr(
        "app.api.telegram_webhook.process_update", lambda update: seen.append(update)
    )
    response = client.post(
        "/webhooks/telegram",
        json={"update_id": 1, "message": {"message_id": 2}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
    )
    assert response.status_code == 200
    assert seen and seen[0]["update_id"] == 1
    assert seen[0]["_correlation_id"], "every update must get a correlation id"


def test_google_webhook_rejects_a_wrong_channel_token(client: TestClient, monkeypatch) -> None:
    called: list[str] = []
    monkeypatch.setattr("app.api.google_webhook.handle_push", lambda cid: called.append(cid))
    response = client.post(
        "/webhooks/google",
        headers={
            "X-Goog-Channel-ID": "chan-1",
            "X-Goog-Resource-State": "exists",
            "X-Goog-Channel-Token": "wrong",
        },
    )
    # 200 so Google does not retry and eventually kill the channel...
    assert response.status_code == 200
    # ...but nothing is processed.
    assert not called


def test_google_webhook_ignores_the_sync_handshake(client: TestClient, monkeypatch) -> None:
    called: list[str] = []
    monkeypatch.setattr("app.api.google_webhook.handle_push", lambda cid: called.append(cid))
    response = client.post(
        "/webhooks/google",
        headers={
            "X-Goog-Channel-ID": "chan-1",
            "X-Goog-Resource-State": "sync",
            "X-Goog-Channel-Token": "goog-token",
        },
    )
    assert response.status_code == 200
    assert not called


def test_google_webhook_schedules_reconciliation(client: TestClient, monkeypatch) -> None:
    called: list[str] = []
    monkeypatch.setattr("app.api.google_webhook.handle_push", lambda cid: called.append(cid))
    response = client.post(
        "/webhooks/google",
        headers={
            "X-Goog-Channel-ID": "chan-42",
            "X-Goog-Resource-State": "exists",
            "X-Goog-Channel-Token": "goog-token",
        },
    )
    assert response.status_code == 200
    assert called == ["chan-42"]


def test_oauth_start_refuses_users_outside_the_allowlist(client: TestClient) -> None:
    assert client.get("/auth/google/start?telegram_user_id=9999").status_code == 403


def test_oauth_start_redirects_with_the_minimum_scope(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr("app.api.google_auth.session_scope", _null_session_scope)
    response = client.get(
        "/auth/google/start?telegram_user_id=4242", follow_redirects=False
    )
    assert response.status_code == 302
    location = response.headers["location"]
    assert "accounts.google.com" in location
    assert "calendar.events" in location
    assert "auth%2Fcalendar&" not in location, "must not request the full calendar scope"
    assert "access_type=offline" in location
    assert "prompt=consent" in location


def test_oauth_callback_rejects_an_unknown_state(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr("app.api.google_auth.session_scope", _null_session_scope)
    response = client.get("/auth/google/callback?state=nope&code=abc")
    assert response.status_code == 400
    assert "expired" in response.text.lower()


class _NullSession:
    def add(self, *_a, **_k):
        pass

    def get(self, *_a, **_k):
        return None

    def execute(self, *_a, **_k):
        return None

    def delete(self, *_a, **_k):
        pass

    def flush(self):
        pass


def _null_session_scope():
    from contextlib import contextmanager

    @contextmanager
    def scope():
        yield _NullSession()

    return scope()
