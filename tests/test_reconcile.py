"""T-23 / T-24: drift detection, expiry and channel renewal."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.models import MeetingStatus, SlotState, User
from app.services.reconcile import (
    ensure_watch_channel,
    expire_past_placeholders,
    get_sync_state,
    reconcile_user,
    user_for_channel,
)
from tests.conftest import make_meeting
from tests.fakes import FakeCalendar

YEREVAN = ZoneInfo("Asia/Yerevan")


def _local(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=YEREVAN).astimezone(
        UTC
    )


def _future(days: int = 7, hour: int = 10) -> datetime:
    base = datetime.now(UTC) + timedelta(days=days)
    return base.replace(hour=hour, minute=0, second=0, microsecond=0)


def _past(days: int = 2, hour: int = 10) -> datetime:
    base = datetime.now(UTC) - timedelta(days=days)
    return base.replace(hour=hour, minute=0, second=0, microsecond=0)


def test_a_placeholder_deleted_in_google_marks_the_slot_removed(
    session: Session, user: User, calendar: FakeCalendar
) -> None:
    """T-23 acceptance."""
    meeting = make_meeting(
        session, user, "Evocabank sync", [_future(3), _future(4)], calendar=calendar
    )
    victim = f"evt-{meeting.id}-0"
    calendar.events[victim]["status"] = "cancelled"  # deleted in the calendar UI

    report = reconcile_user(session, user, calendar)

    assert report.slots_removed == 1
    session.flush()
    removed = [s for s in meeting.slots if s.state is SlotState.removed]
    assert len(removed) == 1
    assert removed[0].gcal_event_id == victim
    assert removed[0].removed_at is not None
    assert meeting.status is MeetingStatus.proposed, "one placeholder still stands"


def test_deleting_the_last_placeholder_expires_the_meeting(
    session: Session, user: User, calendar: FakeCalendar
) -> None:
    meeting = make_meeting(session, user, "Solo proposal", [_future(3)], calendar=calendar)
    calendar.events[f"evt-{meeting.id}-0"]["status"] = "cancelled"

    report = reconcile_user(session, user, calendar)

    assert report.slots_removed == 1
    assert report.meetings_expired == 1
    assert meeting.status is MeetingStatus.expired
    assert any("no placeholders left" in n for n in report.notifications)


def test_an_event_moved_in_google_updates_the_slot(
    session: Session, user: User, calendar: FakeCalendar
) -> None:
    meeting = make_meeting(session, user, "Board call", [_future(3)], calendar=calendar)
    event = calendar.events[f"evt-{meeting.id}-0"]
    moved_to = _future(3, hour=15)
    event["start"] = {"dateTime": moved_to.isoformat().replace("+00:00", "Z")}
    event["end"] = {"dateTime": (moved_to + timedelta(hours=1)).isoformat().replace("+00:00", "Z")}

    report = reconcile_user(session, user, calendar)

    assert report.slots_moved == 1
    assert meeting.slots[0].start_utc == moved_to


def test_reconciliation_ignores_events_this_app_did_not_create(
    session: Session, user: User, calendar: FakeCalendar
) -> None:
    calendar.events["someone-elses"] = {
        "id": "someone-elses",
        "status": "confirmed",
        "summary": "Dentist",
        "start": {"dateTime": _future(1).isoformat().replace("+00:00", "Z")},
        "end": {"dateTime": _future(1).isoformat().replace("+00:00", "Z")},
    }
    report = reconcile_user(session, user, calendar)
    assert report.slots_removed == 0 and report.slots_moved == 0


def test_the_sync_token_is_stored_for_the_next_incremental_pass(
    session: Session, user: User, calendar: FakeCalendar
) -> None:
    reconcile_user(session, user, calendar)
    assert get_sync_state(session, user).gcal_sync_token == "sync-token-1"
    assert get_sync_state(session, user).last_reconciled_at is not None


def test_past_placeholders_are_deleted_and_the_meeting_expires(
    session: Session, user: User, calendar: FakeCalendar
) -> None:
    """T-24 acceptance: yesterday's placeholder is gone this morning."""
    meeting = make_meeting(
        session, user, "Stale proposal", [_past(2), _past(1)], calendar=calendar
    )
    assert len(calendar.yellow()) == 2

    report = expire_past_placeholders(session, user, calendar)

    assert report.placeholders_expired == 2
    assert report.meetings_expired == 1
    assert not calendar.yellow()
    assert meeting.status is MeetingStatus.expired
    assert any("expired" in n for n in report.notifications)


def test_expiry_keeps_a_meeting_alive_when_future_slots_remain(
    session: Session, user: User, calendar: FakeCalendar
) -> None:
    meeting = make_meeting(
        session, user, "Half stale", [_past(1), _future(5)], calendar=calendar
    )
    report = expire_past_placeholders(session, user, calendar)

    assert report.placeholders_expired == 1
    assert report.meetings_expired == 0
    assert meeting.status is MeetingStatus.proposed
    assert len(calendar.yellow()) == 1


def test_expiry_leaves_confirmed_meetings_alone(
    session: Session, user: User, calendar: FakeCalendar
) -> None:
    meeting = make_meeting(
        session,
        user,
        "Already happened",
        [_past(1)],
        status=MeetingStatus.confirmed,
        state=SlotState.fixed,
        calendar=calendar,
    )
    report = expire_past_placeholders(session, user, calendar)
    assert report.placeholders_expired == 0
    assert meeting.status is MeetingStatus.confirmed
    assert len(calendar.green()) == 1


def test_watch_channel_is_registered_and_then_left_alone(
    session: Session, user: User, calendar: FakeCalendar
) -> None:
    assert ensure_watch_channel(session, user, calendar) is True
    state = get_sync_state(session, user)
    assert state.gcal_channel_id and state.gcal_resource_id
    assert user_for_channel(session, state.gcal_channel_id) is user

    # Far from expiry: renewing again would be pointless churn.
    state.channel_expiry = datetime.now(UTC) + timedelta(days=6)
    assert ensure_watch_channel(session, user, calendar) is False


def test_watch_channel_is_renewed_inside_the_48_hour_window(
    session: Session, user: User, calendar: FakeCalendar
) -> None:
    ensure_watch_channel(session, user, calendar)
    state = get_sync_state(session, user)
    first = state.gcal_channel_id
    state.channel_expiry = datetime.now(UTC) + timedelta(hours=12)

    assert ensure_watch_channel(session, user, calendar) is True
    assert state.gcal_channel_id != first


def test_a_meeting_with_a_pending_cleanup_is_not_expired(
    session: Session, user: User, calendar: FakeCalendar
) -> None:
    """A slot stuck in `failed` still has a live event; the group is not empty."""
    meeting = make_meeting(session, user, "Half cleaned", [_future(3)], calendar=calendar)
    meeting.slots[0].state = SlotState.failed
    session.flush()

    report = reconcile_user(session, user, calendar)
    assert report.meetings_expired == 0
    assert meeting.status is MeetingStatus.proposed
