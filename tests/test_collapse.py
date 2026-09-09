"""T-18 / T-20 / T-21: grouping, the collapse operation, cancel and reschedule.

This is the heart of the product: N yellow placeholders must become exactly one
green event, and a failure part-way through must never leave an orphaned yellow
event with no database record.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import Session

from app.integrations.google_calendar import (
    COLOR_CONFIRMED,
    COLOR_SUGGESTED,
    TENTATIVE_PREFIX,
)
from app.models import Meeting, MeetingStatus, SlotState, User, VoiceCommand
from app.nlu.schema import ExtractedIntent, Intent, SpokenSlot
from app.services.meetings import (
    MeetingOperationError,
    apply_plan,
    retry_failed_deletions,
)
from app.services.planning import build_plan
from tests.conftest import make_meeting
from tests.fakes import FakeCalendar

NOW = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)
YEREVAN = ZoneInfo("Asia/Yerevan")


def _local(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=YEREVAN).astimezone(
        UTC
    )


def _reload(session: Session, instance):
    """Flush pending changes, then re-read from the database.

    ``Session.refresh`` does not autoflush, so refreshing without flushing
    first would silently discard the very changes under test.
    """
    session.flush()
    session.expire(instance)
    session.refresh(instance)
    return instance


def _command(session: Session, user: User, message_id: int = 1) -> VoiceCommand:
    command = VoiceCommand(user_id=user.id, telegram_message_id=message_id, transcript="x")
    session.add(command)
    session.flush()
    return command


def _suggest_plan(session: Session, user: User, title: str, raws: list[tuple[str, str, str]]):
    intent = ExtractedIntent(
        intent=Intent.CREATE_SUGGESTED,
        title=title,
        slots=[SpokenSlot(raw=raw, date=date, time=time) for raw, date, time in raws],
        confidence=0.9,
    )
    return build_plan(session, user, intent, now_utc=NOW)


# ---------------------------------------------------------------------------
# T-18 — creation and grouping
# ---------------------------------------------------------------------------

def test_three_spoken_slots_make_one_meeting_and_three_yellow_events(
    session: Session, user: User, calendar: FakeCalendar
) -> None:
    plan = _suggest_plan(
        session,
        user,
        "Evocabank sync",
        [
            ("Tuesday 2pm", "2026-09-15", "14:00"),
            ("Wednesday 10", "2026-09-16", "10:00"),
            ("Thursday 4", "2026-09-17", "16:00"),
        ],
    )
    command = _command(session, user)

    result = apply_plan(session, user, plan, calendar, voice_command=command)

    assert result.ok
    meeting = session.get(Meeting, result.meeting_id)
    assert meeting is not None
    assert meeting.status is MeetingStatus.proposed
    assert len(meeting.slots) == 3
    assert all(s.state is SlotState.suggested for s in meeting.slots)
    assert all(s.gcal_event_id for s in meeting.slots)

    assert len(calendar.yellow()) == 3
    assert not calendar.green()
    for event in calendar.yellow():
        assert event["summary"].startswith(TENTATIVE_PREFIX)
        assert event["transparency"] == "transparent"
        assert event["visibility"] == "private"
        assert "attendees" not in event  # D3: placeholders never invite anyone


def test_fixed_creation_is_green_opaque_and_unprefixed(
    session: Session, user: User, calendar: FakeCalendar
) -> None:
    intent = ExtractedIntent(
        intent=Intent.CREATE_FIXED,
        title="Board call",
        slots=[SpokenSlot(raw="Tuesday 3pm", date="2026-09-15", time="15:00")],
        confidence=0.95,
    )
    plan = build_plan(session, user, intent, now_utc=NOW)
    result = apply_plan(session, user, plan, calendar, voice_command=_command(session, user))

    assert result.ok
    assert len(calendar.green()) == 1
    event = calendar.green()[0]
    assert event["summary"] == "Board call"
    assert event["transparency"] == "opaque"
    assert not event["summary"].startswith(TENTATIVE_PREFIX)


def test_replaying_the_same_command_creates_no_duplicates(
    session: Session, user: User, calendar: FakeCalendar
) -> None:
    """T-08 acceptance: replaying the same command yields exactly one event.

    This is the crash-recovery case — the calendar write landed but the
    database transaction never committed, so the command is retried.
    """
    plan = _suggest_plan(
        session, user, "Ameria call", [("Tuesday 2pm", "2026-09-15", "14:00")]
    )
    command = _command(session, user)
    command_id = command.id

    apply_plan(session, user, plan, calendar, voice_command=command)
    session.flush()
    assert len(calendar.live) == 1

    # The transaction is lost; the calendar event survives.
    session.rollback()
    assert len(calendar.live) == 1

    command = _command(session, user)
    assert command.id == command_id, "the retry must reuse the same command id"
    plan2 = _suggest_plan(
        session, user, "Ameria call", [("Tuesday 2pm", "2026-09-15", "14:00")]
    )
    apply_plan(session, user, plan2, calendar, voice_command=command)
    session.flush()

    assert len(calendar.live) == 1, "a replay created a duplicate event"
    slots = session.query(Meeting).one().slots
    assert len(slots) == 1
    assert slots[0].gcal_event_id == calendar.live[0]["id"]


def test_creation_reports_failure_when_every_write_fails(
    session: Session, user: User, calendar: FakeCalendar
) -> None:
    calendar.fail_create = 5
    plan = _suggest_plan(
        session, user, "Doomed call", [("Tuesday 2pm", "2026-09-15", "14:00")]
    )
    result = apply_plan(session, user, plan, calendar, voice_command=_command(session, user))
    assert not result.ok
    assert not calendar.live


# ---------------------------------------------------------------------------
# T-20 — the collapse
# ---------------------------------------------------------------------------

def _seed_three(session: Session, user: User, calendar: FakeCalendar):
    return make_meeting(
        session,
        user,
        "Evocabank sync",
        [_local("2026-09-15 14:00"), _local("2026-09-16 10:00"), _local("2026-09-17 16:00")],
        calendar=calendar,
    )


def _confirm_plan(session: Session, user: User, reference: str, raw: str, date: str, time: str):
    intent = ExtractedIntent(
        intent=Intent.CONFIRM_SLOT,
        meeting_reference=reference,
        slots=[SpokenSlot(raw=raw, date=date, time=time)],
        confidence=0.9,
    )
    return build_plan(session, user, intent, now_utc=NOW)


def test_confirming_one_of_three_leaves_one_green_and_zero_yellow(
    session: Session, user: User, calendar: FakeCalendar
) -> None:
    """The T-20 acceptance criterion."""
    meeting = _seed_three(session, user, calendar)
    assert len(calendar.yellow()) == 3

    plan = _confirm_plan(session, user, "Evocabank", "Wednesday 10", "2026-09-16", "10:00")
    assert plan.action == "confirm"
    assert plan.sibling_slot_count == 2

    result = apply_plan(session, user, plan, calendar, voice_command=_command(session, user))

    assert result.ok and not result.partial
    assert len(calendar.green()) == 1
    assert len(calendar.yellow()) == 0

    green = calendar.green()[0]
    assert green["summary"] == "Evocabank sync"
    assert not green["summary"].startswith(TENTATIVE_PREFIX)
    assert green["transparency"] == "opaque"
    assert green["colorId"] == COLOR_CONFIRMED

    _reload(session, meeting)
    assert meeting.status is MeetingStatus.confirmed
    assert meeting.confirmed_at is not None
    fixed = [s for s in meeting.slots if s.state is SlotState.fixed]
    removed = [s for s in meeting.slots if s.state is SlotState.removed]
    assert len(fixed) == 1 and len(removed) == 2
    assert fixed[0].start_utc == _local("2026-09-16 10:00")


def test_confirming_a_time_that_was_never_proposed(
    session: Session, user: User, calendar: FakeCalendar
) -> None:
    """T-20 step 1 explicitly supports a brand-new time."""
    meeting = _seed_three(session, user, calendar)

    plan = _confirm_plan(session, user, "Evocabank", "Friday at 9", "2026-09-18", "09:00")
    assert plan.action == "confirm"
    assert plan.slots[0].existing_slot_id is None
    assert plan.sibling_slot_count == 3

    result = apply_plan(session, user, plan, calendar, voice_command=_command(session, user))

    assert result.ok
    assert len(calendar.green()) == 1
    assert len(calendar.yellow()) == 0
    assert calendar.green()[0]["start"]["dateTime"].startswith("2026-09-18T05:00")

    _reload(session, meeting)
    assert meeting.status is MeetingStatus.confirmed
    assert len([s for s in meeting.slots if s.state is SlotState.fixed]) == 1
    assert len([s for s in meeting.slots if s.state is SlotState.removed]) == 3


def test_a_failed_sibling_deletion_is_reported_and_never_orphaned(
    session: Session, user: User, calendar: FakeCalendar
) -> None:
    """T-20 step 5: partial state must be visible, recorded and retried."""
    meeting = _seed_three(session, user, calendar)
    doomed = f"evt-{meeting.id}-2"
    calendar.fail_delete[doomed] = 99  # never succeeds

    plan = _confirm_plan(session, user, "Evocabank", "Wednesday 10", "2026-09-16", "10:00")
    result = apply_plan(session, user, plan, calendar, voice_command=_command(session, user))

    assert result.ok and result.partial and result.needs_cleanup_retry
    assert "could not be deleted" in result.message
    assert len(calendar.green()) == 1
    assert len(calendar.yellow()) == 1, "the undeletable placeholder is still there"

    _reload(session, meeting)
    stuck = [s for s in meeting.slots if s.state is SlotState.failed]
    assert len(stuck) == 1, "the surviving event must keep a database row"
    assert stuck[0].gcal_event_id == doomed

    # Every live calendar event still has a row pointing at it — no orphans.
    live_ids = {e["id"] for e in calendar.live}
    known_ids = {s.gcal_event_id for s in meeting.slots if s.gcal_event_id}
    assert live_ids <= known_ids


def test_the_retry_job_clears_a_previously_failed_deletion(
    session: Session, user: User, calendar: FakeCalendar
) -> None:
    meeting = _seed_three(session, user, calendar)
    doomed = f"evt-{meeting.id}-2"
    calendar.fail_delete[doomed] = 1  # fails once, then succeeds

    plan = _confirm_plan(session, user, "Evocabank", "Wednesday 10", "2026-09-16", "10:00")
    result = apply_plan(session, user, plan, calendar, voice_command=_command(session, user))
    assert result.partial

    cleared, failing = retry_failed_deletions(session, user, calendar)
    assert (cleared, failing) == (1, 0)
    assert len(calendar.yellow()) == 0
    assert len(calendar.green()) == 1
    _reload(session, meeting)
    assert not [s for s in meeting.slots if s.state is SlotState.failed]


def test_promotion_failure_changes_nothing(
    session: Session, user: User, calendar: FakeCalendar
) -> None:
    """If step 2 fails, no sibling is deleted and the meeting stays proposed."""
    meeting = _seed_three(session, user, calendar)
    calendar.fail_update = 99

    plan = _confirm_plan(session, user, "Evocabank", "Wednesday 10", "2026-09-16", "10:00")
    with pytest.raises(MeetingOperationError, match="Nothing was changed"):
        apply_plan(session, user, plan, calendar, voice_command=_command(session, user))

    assert len(calendar.yellow()) == 3
    assert not calendar.green()
    _reload(session, meeting)
    assert meeting.status is MeetingStatus.proposed


def test_confirming_when_the_placeholder_was_deleted_by_hand(
    session: Session, user: User, calendar: FakeCalendar
) -> None:
    meeting = _seed_three(session, user, calendar)
    # The owner deleted the winning placeholder in the Google Calendar UI.
    del calendar.events[f"evt-{meeting.id}-1"]

    plan = _confirm_plan(session, user, "Evocabank", "Wednesday 10", "2026-09-16", "10:00")
    result = apply_plan(session, user, plan, calendar, voice_command=_command(session, user))

    assert result.ok
    assert len(calendar.green()) == 1
    assert len(calendar.yellow()) == 0
    assert any("already been deleted" in w for w in result.warnings)


def test_confirmed_meeting_keeps_the_original_duration(
    session: Session, user: User, calendar: FakeCalendar
) -> None:
    meeting = _seed_three(session, user, calendar)
    plan = _confirm_plan(session, user, "Evocabank", "Wednesday 10", "2026-09-16", "10:00")
    apply_plan(session, user, plan, calendar, voice_command=_command(session, user))
    _reload(session, meeting)
    assert meeting.duration_minutes == 60


# ---------------------------------------------------------------------------
# T-21 — cancel and reschedule
# ---------------------------------------------------------------------------

def test_cancel_removes_every_event_in_the_group(
    session: Session, user: User, calendar: FakeCalendar
) -> None:
    meeting = _seed_three(session, user, calendar)
    intent = ExtractedIntent(
        intent=Intent.CANCEL_MEETING, meeting_reference="Evocabank", confidence=0.95
    )
    plan = build_plan(session, user, intent, now_utc=NOW)
    assert plan.action == "cancel"

    result = apply_plan(session, user, plan, calendar, voice_command=_command(session, user))

    assert result.ok
    assert not calendar.live
    _reload(session, meeting)
    assert meeting.status is MeetingStatus.cancelled
    assert all(s.state is SlotState.removed for s in meeting.slots)


def test_reschedule_moves_a_confirmed_event(
    session: Session, user: User, calendar: FakeCalendar
) -> None:
    meeting = make_meeting(
        session,
        user,
        "Board call",
        [_local("2026-09-16 11:00")],
        status=MeetingStatus.confirmed,
        state=SlotState.fixed,
        calendar=calendar,
    )
    intent = ExtractedIntent(
        intent=Intent.RESCHEDULE,
        meeting_reference="board call",
        slots=[SpokenSlot(raw="Thursday 4pm", date="2026-09-17", time="16:00")],
        confidence=0.92,
    )
    plan = build_plan(session, user, intent, now_utc=NOW)
    assert plan.action == "reschedule"

    result = apply_plan(session, user, plan, calendar, voice_command=_command(session, user))

    assert result.ok
    _reload(session, meeting)
    fixed = [s for s in meeting.slots if s.state is SlotState.fixed]
    assert len(fixed) == 1
    assert fixed[0].start_utc == _local("2026-09-17 16:00")
    assert len(calendar.green()) == 1


def test_rescheduling_a_still_proposed_meeting_confirms_it_instead(
    session: Session, user: User, calendar: FakeCalendar
) -> None:
    meeting = _seed_three(session, user, calendar)
    intent = ExtractedIntent(
        intent=Intent.RESCHEDULE,
        meeting_reference="Evocabank",
        slots=[SpokenSlot(raw="Friday at 9", date="2026-09-18", time="09:00")],
        confidence=0.9,
    )
    plan = build_plan(session, user, intent, now_utc=NOW)
    result = apply_plan(session, user, plan, calendar, voice_command=_command(session, user))

    assert result.ok
    _reload(session, meeting)
    assert meeting.status is MeetingStatus.confirmed
    assert len(calendar.green()) == 1
    assert len(calendar.yellow()) == 0


def test_reschedule_notifies_guests_when_there_are_any(
    session: Session, user: User, calendar: FakeCalendar
) -> None:
    meeting = make_meeting(
        session,
        user,
        "Client review",
        [_local("2026-09-16 11:00")],
        status=MeetingStatus.confirmed,
        state=SlotState.fixed,
        calendar=calendar,
    )
    meeting.attendees_json = ["client@example.com"]
    session.flush()

    intent = ExtractedIntent(
        intent=Intent.RESCHEDULE,
        meeting_reference="Client review",
        slots=[SpokenSlot(raw="Thursday 4pm", date="2026-09-17", time="16:00")],
        confidence=0.9,
    )
    plan = build_plan(session, user, intent, now_utc=NOW)
    result = apply_plan(session, user, plan, calendar, voice_command=_command(session, user))
    assert result.ok
    assert "Guests have been notified" in result.message


def test_colors_are_the_documented_ids() -> None:
    assert COLOR_CONFIRMED == "10"  # Basil, green
    assert COLOR_SUGGESTED == "5"  # Banana, yellow
