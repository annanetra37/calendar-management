"""In-memory doubles used across the unit tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.integrations.google_calendar import (
    APP_TAG,
    TENTATIVE_PREFIX,
    CalendarError,
    CalendarNotFound,
    EventRef,
)


class FakeCalendar:
    """A stand-in for ``CalendarClient`` that records everything it is asked to do."""

    def __init__(self) -> None:
        self.events: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self._counter = 0
        #: event_id -> number of remaining forced failures
        self.fail_delete: dict[str, int] = {}
        self.fail_create = 0
        self.fail_update = 0

    # -- helpers used by tests -----------------------------------------
    def seed_event(
        self,
        *,
        event_id: str,
        meeting_id: int,
        start: datetime,
        end: datetime,
        tentative: bool,
        title: str,
    ) -> dict[str, Any]:
        event = {
            "id": event_id,
            "etag": f'"{event_id}-1"',
            "status": "confirmed",
            "summary": (TENTATIVE_PREFIX if tentative else "") + title,
            "colorId": "5" if tentative else "10",
            "transparency": "transparent" if tentative else "opaque",
            "start": {"dateTime": _iso(start)},
            "end": {"dateTime": _iso(end)},
            "extendedProperties": {
                "private": {
                    "app": APP_TAG,
                    "meeting_id": str(meeting_id),
                    "kind": "suggested" if tentative else "confirmed",
                    "idempotency_key": f"seed-{event_id}",
                }
            },
        }
        self.events[event_id] = event
        return event

    @property
    def live(self) -> list[dict[str, Any]]:
        return [e for e in self.events.values() if e.get("status") != "cancelled"]

    def yellow(self) -> list[dict[str, Any]]:
        return [e for e in self.live if e.get("colorId") == "5"]

    def green(self) -> list[dict[str, Any]]:
        return [e for e in self.live if e.get("colorId") == "10"]

    # -- CalendarClient surface ----------------------------------------
    def create_event(self, body: dict, *, send_updates: str = "none") -> EventRef:
        if self.fail_create:
            self.fail_create -= 1
            raise CalendarError("simulated create failure", status=503)
        self._counter += 1
        event_id = f"created-{self._counter}"
        event = dict(body)
        event["id"] = event_id
        event["etag"] = f'"{event_id}-1"'
        event["status"] = "confirmed"
        self.events[event_id] = event
        self.calls.append(("create", event_id))
        return EventRef(event_id=event_id, etag=event["etag"])

    def get_event(self, event_id: str) -> dict:
        if event_id not in self.events:
            raise CalendarNotFound("gone", status=404)
        return self.events[event_id]

    def update_event(self, event_id: str, body: dict, *, send_updates: str = "none") -> EventRef:
        if self.fail_update:
            self.fail_update -= 1
            raise CalendarError("simulated update failure", status=503)
        if event_id not in self.events or self.events[event_id].get("status") == "cancelled":
            raise CalendarNotFound("gone", status=404)
        self.events[event_id].update(body)
        self.events[event_id]["etag"] = f'"{event_id}-2"'
        self.calls.append(("update", event_id))
        return EventRef(event_id=event_id, etag=self.events[event_id]["etag"])

    def patch_color(self, event_id: str, color_id: str) -> EventRef:
        return self.update_event(event_id, {"colorId": color_id})

    def delete_event(self, event_id: str, *, send_updates: str = "none") -> bool:
        remaining = self.fail_delete.get(event_id, 0)
        if remaining:
            self.fail_delete[event_id] = remaining - 1
            raise CalendarError("simulated delete failure", status=503)
        if event_id not in self.events:
            return False
        self.events[event_id]["status"] = "cancelled"
        self.calls.append(("delete", event_id))
        return True

    def list_events(
        self,
        *,
        time_min=None,
        time_max=None,
        private_extended_property: str | None = None,
        sync_token: str | None = None,
        show_deleted: bool = False,
        max_results: int = 250,
    ):
        items = list(self.events.values())
        if private_extended_property:
            key, _, value = private_extended_property.partition("=")
            items = [
                e
                for e in items
                if ((e.get("extendedProperties") or {}).get("private") or {}).get(key) == value
            ]
        if not show_deleted:
            items = [e for e in items if e.get("status") != "cancelled"]
        return items, "sync-token-1"

    def find_by_idempotency_key(self, key: str) -> dict | None:
        items, _ = self.list_events(private_extended_property=f"idempotency_key={key}")
        return items[0] if items else None

    def watch(self, channel_id: str, address: str, token: str, ttl_seconds: int = 604800) -> dict:
        return {"resourceId": f"res-{channel_id}", "expiration": "1789000000000"}

    def stop_channel(self, channel_id: str, resource_id: str) -> bool:
        return True

    def close(self) -> None:
        pass


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
