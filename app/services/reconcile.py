"""Drift detection and reconciliation (T-23, T-24).

Two paths reach the same code: Google push notifications (near-real-time) and
the daily cron sweep (the backstop that catches whatever push missed).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.integrations.google_calendar import (
    CalendarClient,
    CalendarError,
    SyncTokenExpired,
    is_app_event,
)
from app.models import Meeting, MeetingStatus, Slot, SlotState, SyncState, User, utcnow

log = logging.getLogger(__name__)

CHANNEL_TTL_SECONDS = 7 * 24 * 3600
CHANNEL_RENEW_WINDOW = timedelta(hours=48)
FULL_SWEEP_PAST = timedelta(days=1)
FULL_SWEEP_FUTURE = timedelta(days=180)


@dataclass(slots=True)
class ReconcileReport:
    slots_removed: int = 0
    slots_moved: int = 0
    meetings_expired: int = 0
    placeholders_expired: int = 0
    cleanup_cleared: int = 0
    cleanup_failing: int = 0
    notifications: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def merge(self, other: ReconcileReport) -> ReconcileReport:
        self.slots_removed += other.slots_removed
        self.slots_moved += other.slots_moved
        self.meetings_expired += other.meetings_expired
        self.placeholders_expired += other.placeholders_expired
        self.cleanup_cleared += other.cleanup_cleared
        self.cleanup_failing += other.cleanup_failing
        self.notifications += other.notifications
        self.errors += other.errors
        return self

    @property
    def is_quiet(self) -> bool:
        return not (
            self.slots_removed
            or self.slots_moved
            or self.meetings_expired
            or self.placeholders_expired
            or self.cleanup_cleared
            or self.errors
        )


def get_sync_state(session: Session, user: User) -> SyncState:
    state = session.get(SyncState, user.id)
    if state is None:
        state = SyncState(user_id=user.id)
        session.add(state)
        session.flush()
    return state


def user_for_channel(session: Session, channel_id: str) -> User | None:
    state = session.scalar(select(SyncState).where(SyncState.gcal_channel_id == channel_id))
    return session.get(User, state.user_id) if state else None


# ---------------------------------------------------------------------------
# Incremental / full reconciliation
# ---------------------------------------------------------------------------

def reconcile_user(
    session: Session, user: User, calendar: CalendarClient, *, force_full: bool = False
) -> ReconcileReport:
    report = ReconcileReport()
    state = get_sync_state(session, user)
    sync_token = None if force_full else state.gcal_sync_token

    try:
        events, next_token = _list(calendar, sync_token)
    except SyncTokenExpired:
        log.info("sync_token_expired_full_resync", extra={"user_id": user.id})
        state.gcal_sync_token = None
        events, next_token = _list(calendar, None)
        sync_token = None
    except CalendarError as exc:
        report.errors.append(f"Calendar list failed: {exc}")
        return report

    seen_event_ids: set[str] = set()

    for event in events:
        event_id = event.get("id")
        if not event_id:
            continue
        seen_event_ids.add(event_id)
        slot = session.scalar(
            select(Slot)
            .where(Slot.gcal_event_id == event_id)
            .options(selectinload(Slot.meeting))
        )
        if slot is None:
            continue
        if slot.meeting.user_id != user.id:
            continue

        if event.get("status") == "cancelled":
            if slot.state in (SlotState.suggested, SlotState.fixed):
                slot.state = SlotState.removed
                slot.removed_at = utcnow()
                report.slots_removed += 1
                log.info(
                    "slot_removed_externally",
                    extra={"slot_id": slot.id, "meeting_id": slot.meeting_id},
                )
            continue

        if not is_app_event(event):
            continue

        moved = _apply_time_change(slot, event)
        if moved:
            report.slots_moved += 1
        slot.gcal_etag = event.get("etag") or slot.gcal_etag

    # A full sweep can also detect events that vanished without a tombstone.
    if sync_token is None:
        report.merge(_detect_vanished(session, user, seen_event_ids))

    report.merge(_expire_emptied_meetings(session, user))

    state.gcal_sync_token = next_token or state.gcal_sync_token
    state.last_reconciled_at = utcnow()
    return report


def _list(calendar: CalendarClient, sync_token: str | None):
    if sync_token:
        return calendar.list_events(sync_token=sync_token)
    now = datetime.now(UTC)
    return calendar.list_events(
        time_min=now - FULL_SWEEP_PAST,
        time_max=now + FULL_SWEEP_FUTURE,
        show_deleted=True,
    )


def _apply_time_change(slot: Slot, event: dict) -> bool:
    start = _parse_event_time(event.get("start"))
    end = _parse_event_time(event.get("end"))
    if start is None or end is None:
        return False
    if abs((slot.start_utc - start).total_seconds()) < 60 and abs(
        (slot.end_utc - end).total_seconds()
    ) < 60:
        return False
    slot.start_utc = start
    slot.end_utc = end
    log.info("slot_moved_externally", extra={"slot_id": slot.id})
    return True


def _parse_event_time(payload: dict | None) -> datetime | None:
    if not payload:
        return None
    raw = payload.get("dateTime")
    if not raw:
        date_only = payload.get("date")
        if not date_only:
            return None
        return datetime.fromisoformat(date_only).replace(tzinfo=UTC)
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return value.astimezone(UTC)


def _detect_vanished(session: Session, user: User, seen: set[str]) -> ReconcileReport:
    report = ReconcileReport()
    stmt = (
        select(Slot)
        .join(Meeting, Slot.meeting_id == Meeting.id)
        .where(
            Meeting.user_id == user.id,
            Slot.state.in_([SlotState.suggested, SlotState.fixed]),
            Slot.gcal_event_id.is_not(None),
            Slot.start_utc > datetime.now(UTC) - FULL_SWEEP_PAST,
        )
    )
    for slot in session.scalars(stmt):
        if slot.gcal_event_id in seen:
            continue
        slot.state = SlotState.removed
        slot.removed_at = utcnow()
        report.slots_removed += 1
        log.info("slot_vanished", extra={"slot_id": slot.id})
    return report


def _expire_emptied_meetings(session: Session, user: User) -> ReconcileReport:
    """A proposed meeting whose last placeholder is gone is dead."""
    report = ReconcileReport()
    stmt = (
        select(Meeting)
        .where(Meeting.user_id == user.id, Meeting.status == MeetingStatus.proposed)
        .options(selectinload(Meeting.slots))
    )
    for meeting in session.scalars(stmt):
        if meeting.live_slots():
            continue
        if any(s.state is SlotState.failed for s in meeting.slots):
            continue  # cleanup still pending; not actually empty
        meeting.status = MeetingStatus.expired
        report.meetings_expired += 1
        report.notifications.append(
            f"🟡 “{meeting.title}” has no placeholders left — I marked it expired. "
            "Send a new voice note to propose times again."
        )
    return report


# ---------------------------------------------------------------------------
# Expiry sweep (T-24)
# ---------------------------------------------------------------------------

def expire_past_placeholders(
    session: Session, user: User, calendar: CalendarClient
) -> ReconcileReport:
    """Placeholders whose time has passed while still suggested."""
    report = ReconcileReport()
    now = datetime.now(UTC)
    stmt = (
        select(Meeting)
        .where(Meeting.user_id == user.id, Meeting.status == MeetingStatus.proposed)
        .options(selectinload(Meeting.slots))
    )
    for meeting in session.scalars(stmt):
        past = [
            s
            for s in meeting.slots
            if s.state is SlotState.suggested and s.start_utc < now
        ]
        if not past:
            continue
        for slot in past:
            if slot.gcal_event_id:
                try:
                    calendar.delete_event(slot.gcal_event_id, send_updates="none")
                except CalendarError as exc:
                    slot.state = SlotState.failed
                    report.errors.append(f"Could not delete a stale placeholder: {exc}")
                    continue
            slot.state = SlotState.removed
            slot.removed_at = utcnow()
            report.placeholders_expired += 1

        remaining = meeting.live_slots()
        if not remaining and not any(s.state is SlotState.failed for s in meeting.slots):
            meeting.status = MeetingStatus.expired
            report.meetings_expired += 1
            report.notifications.append(
                f"⌛ “{meeting.title}” expired — every proposed slot is now in the past, "
                "so I removed the placeholders."
            )
        elif remaining:
            report.notifications.append(
                f"⌛ Removed {len(past)} past placeholder(s) from “{meeting.title}”; "
                f"{len(remaining)} still open."
            )
    return report


# ---------------------------------------------------------------------------
# Push channels (T-23)
# ---------------------------------------------------------------------------

def ensure_watch_channel(
    session: Session, user: User, calendar: CalendarClient, *, force: bool = False
) -> bool:
    """Register or renew the Google push channel. Returns True if (re)registered."""
    settings = get_settings()
    state = get_sync_state(session, user)
    now = datetime.now(UTC)

    if (
        not force
        and state.gcal_channel_id
        and state.channel_expiry
        and _aware(state.channel_expiry) - now > CHANNEL_RENEW_WINDOW
    ):
        return False

    previous_id, previous_resource = state.gcal_channel_id, state.gcal_resource_id
    channel_id = f"vc-{user.id}-{uuid.uuid4().hex[:12]}"
    address = f"{settings.public_base_url}/webhooks/google"

    try:
        payload = calendar.watch(
            channel_id,
            address,
            settings.google_webhook_token or settings.telegram_webhook_secret,
            ttl_seconds=CHANNEL_TTL_SECONDS,
        )
    except CalendarError as exc:
        log.error("watch_channel_failed", extra={"user_id": user.id, "error": str(exc)})
        return False

    state.gcal_channel_id = channel_id
    state.gcal_resource_id = payload.get("resourceId")
    expiration = payload.get("expiration")
    state.channel_expiry = (
        datetime.fromtimestamp(int(expiration) / 1000, tz=UTC)
        if expiration
        else now + timedelta(seconds=CHANNEL_TTL_SECONDS)
    )
    log.info(
        "watch_channel_registered",
        extra={"user_id": user.id, "channel_id": channel_id, "expiry": str(state.channel_expiry)},
    )

    if previous_id and previous_resource:
        calendar.stop_channel(previous_id, previous_resource)
    return True


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)
