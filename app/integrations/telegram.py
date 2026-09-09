"""Telegram Bot API client (T-09, T-10, T-15).

Webhook mode only — the bot never polls, so a Railway instance is not burned
holding a long poll open.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    pass


@dataclass(slots=True)
class InlineButton:
    text: str
    callback_data: str


class TelegramClient:
    def __init__(self, token: str | None = None, *, client: httpx.Client | None = None) -> None:
        settings = get_settings()
        self._token = token or settings.telegram_bot_token
        self._client = client or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> TelegramClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def _base(self) -> str:
        return f"https://api.telegram.org/bot{self._token}"

    def _call(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in (1, 2, 3):
            try:
                response = self._client.post(f"{self._base}/{method}", json=payload or {})
                body = response.json()
            except Exception as exc:
                last = exc
                time.sleep(0.5 * attempt)
                continue
            if body.get("ok"):
                return body.get("result", {})
            description = body.get("description", "unknown error")
            if response.status_code == 429:
                retry_after = int((body.get("parameters") or {}).get("retry_after", 1))
                time.sleep(min(retry_after, 10))
                last = TelegramError(description)
                continue
            raise TelegramError(f"{method} failed: {description}")
        raise TelegramError(f"{method} failed after retries: {last}")

    # -- messaging ------------------------------------------------------
    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        buttons: Iterable[Iterable[InlineButton]] | None = None,
        reply_to_message_id: int | None = None,
        disable_notification: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": disable_notification,
        }
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
            payload["allow_sending_without_reply"] = True
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": _keyboard(buttons)}
        return self._call("sendMessage", payload)

    def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        buttons: Iterable[Iterable[InlineButton]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        payload["reply_markup"] = (
            {"inline_keyboard": _keyboard(buttons)} if buttons else {"inline_keyboard": []}
        )
        try:
            return self._call("editMessageText", payload)
        except TelegramError as exc:
            # Editing to identical content is not an error worth propagating.
            if "message is not modified" in str(exc):
                return {}
            raise

    def answer_callback_query(
        self, callback_query_id: str, text: str = "", show_alert: bool = False
    ) -> dict[str, Any]:
        return self._call(
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id, "text": text[:200], "show_alert": show_alert},
        )

    def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        try:
            self._call("sendChatAction", {"chat_id": chat_id, "action": action})
        except TelegramError:
            pass

    # -- files ----------------------------------------------------------
    def get_file(self, file_id: str) -> dict[str, Any]:
        return self._call("getFile", {"file_id": file_id})

    def download_file(self, file_path: str, *, max_bytes: int) -> bytes:
        url = f"https://api.telegram.org/file/bot{self._token}/{file_path}"
        chunks: list[bytes] = []
        total = 0
        with self._client.stream("GET", url) as response:
            if response.status_code >= 400:
                raise TelegramError(f"File download failed ({response.status_code}).")
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise TelegramError("That voice note is too large to process.")
                chunks.append(chunk)
        return b"".join(chunks)

    # -- setup ----------------------------------------------------------
    def set_webhook(self, url: str, secret_token: str) -> dict[str, Any]:
        return self._call(
            "setWebhook",
            {
                "url": url,
                "secret_token": secret_token,
                "allowed_updates": ["message", "callback_query"],
                "drop_pending_updates": True,
            },
        )

    def delete_webhook(self) -> dict[str, Any]:
        return self._call("deleteWebhook", {"drop_pending_updates": False})

    def get_me(self) -> dict[str, Any]:
        return self._call("getMe")

    def set_my_commands(self, commands: list[tuple[str, str]]) -> dict[str, Any]:
        return self._call(
            "setMyCommands",
            {"commands": [{"command": name, "description": desc} for name, desc in commands]},
        )


def _keyboard(buttons: Iterable[Iterable[InlineButton]]) -> list[list[dict[str, str]]]:
    return [
        [{"text": b.text, "callback_data": b.callback_data} for b in row] for row in buttons
    ]
