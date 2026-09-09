"""Execution of a confirmed plan: the core meeting logic (T-18, T-20, T-21).

A *meeting* is the logical group; *slots* are its calendar events. Creating a
suggested meeting fans out to N yellow events; confirming collapses them to one
green event.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.integrations.google_calendar import (
    CalendarClient,
    CalendarError,
    CalendarNotFound,
    build_event_body,
    promotion_patch,
)
from app.models import Meeting, MeetingStatus, Slot, SlotState, User, VoiceCommand, utcnow
from app.services.matching import normalize_title
from app.services.planning import Plan

log = logging.getLogger(__name__)


class MeetingOperationError(RuntimeError):
    """The operation could not be started; nothing was changed."""


@dataclass(slots=True)
class ApplyResult:
    ok: bool
    message: str
    meeting_id: int | None = None
    partial: bool = False
    needs_cleanup_retry: bool = False
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def apply_plan(
    session: Session,
    user: User,
    plan: Plan,
    calendar: CalendarClient,
    *,
    voice_command: VoiceCommand | None = None,
) -> ApplyResult:
    if plan.action in ("create_suggested", "create_fixed"):
        return create_meeting(session, user, plan, calendar, voice_command=voice_command)
    if plan.action == "confirm":
        return collapse_to_confirmed(session, user, plan, calendar, voice_command=voice_command)
    if plan.action == "cancel":
        return cancel_meeting(session, user, plan, calendar)
    if plan.action == "reschedule":
        return reschedule_meeting(session, user, plan, calendar, voice_command=voice_command)
    raise MeetingOperationError(f"Plan action {plan.action!r} is not executable.")


# ---------------------------------------------------------------------------
# T-18 — creation and grouping
# ---------------------------------------------------------------------------

def create_meeting(
    session: Session,
    user: User,
    plan: Plan,
    calendar: CalendarClient,
    *,
    voice_command: VoiceCommand | None = None,
) -> ApplyResult:
    """One meeting row + N slot rows + N calendar events, in one transaction."""
    tentative = plan.action == "create_suggested"
    command_id = voice_command.id if voice_command else f"adhoc-{int(utcnow().timestamp())}"

    meeting = Meeting(
        user_id=user.id,
        title=plan.title,
        normalized_title=normalize_title(plan.title),
        status=MeetingStatus.proposed if tentative else MeetingStatus.confirmed,
        duration_minutes=plan.duration_minutes,
        attendees_json=plan.attendees or None,
        confirmed_at=None if tentative else utcnow(),
    )
    session.add(meeting)
    session.flush()  # assigns meeting.id without committing

    created: list[Slot] = []
    warnings: list[str] = []

    for index, planned in enumerate(plan.slots):
        key = f"{command_id}:{index}"
        slot = Slot(
            meeting_id=meeting.id,
            start_utc=planned.start_utc,
            end_utc=planned.end_utc,
            state=SlotState.suggested if tentative else SlotState.fixed,
            idempotency_key=key,
        )
        session.add(slot)
        session.flush()

        body = build_event_body(
            title=plan.title,
            start_utc=planned.start_utc,
            end_utc=planned.end_utc,
            timezone_name=planned.timezone_name,
            tentative=tentative,
            meeting_id=meeting.id,
            idempotency_key=key,
            attendees=plan.attendees if not tentative else None,
        )
        try:
            existing = calendar.find_by_idempotency_key(key)
            if existing:
                # T-08: this exact write already landed (retried webhook).
                slot.gcal_event_id = existing["id"]
                slot.gcal_etag = existing.get("etag")
                log.info("idempotent_create_skipped", extra={"idempotency_key": key})
            else:
                send_updates = "none"
                if not tentative and plan.attendees:
                    send_updates = "all"
                ref = calendar.create_event(body, send_updates=send_updates)
                slot.gcal_event_id = ref.event_id
                slot.gcal_etag = ref.etag
            created.append(slot)
        except CalendarError as exc:
            slot.state = SlotState.failed
            warnings.append(f"Could not create the slot at {_fmt(planned)}: {exc}")
            log.error("slot_create_failed", extra={"meeting_id": meeting.id, "error": str(exc)})

    if not created:
        # Nothing landed on the calendar: do not keep a phantom meeting.
        session.rollback()
        return ApplyResult(
            ok=False,
            message=(
                "I could not write anything to your calendar. "
                "Google refused every request — please try again shortly."
            ),
        )

    if voice_command is not None:
        voice_command.resolved_meeting_id = meeting.id

    kind = "placeholder" if tentative else "confirmed"
    plural = "s" if len(created) != 1 else ""
    return ApplyResult(
        ok=True,
        message=f"Created {len(created)} {kind} slot{plural} for “{plan.title}”.",
        meeting_id=meeting.id,
        partial=bool(warnings),
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# T-20 — the collapse operation
# ---------------------------------------------------------------------------

def collapse_to_confirmed(
    session: Session,
    user: User,
    plan: Plan,
    calendar: CalendarClient,
    *,
    voice_command: VoiceCommand | None = None,
) -> ApplyResult:
    """Promote one slot to green and remove every sibling placeholder.

    The winner may be an existing placeholder *or* a time that was never
    proposed — both are supported.

    Consistency contract: a placeholder whose calendar event could not be
    deleted keeps its database row, in state ``failed``, so it is retried and
    can never become an orphaned yellow event with no record. The database
    changes commit as one transaction; the caller commits.
    """
    meeting = session.get(Meeting, plan.meeting_id)
    if meeting is None or meeting.user_id != user.id:
        raise MeetingOperationError("That meeting no longer exists.")
    if meeting.status is MeetingStatus.cancelled:
        raise MeetingOperationError(f"“{meeting.title}” was already cancelled.")

    winner_plan = plan.slots[0]
    command_id = voice_command.id if voice_command else f"adhoc-{int(utcnow().timestamp())}"
    tz_name = winner_plan.timezone_name
    attendees = plan.attendees or list(meeting.attendees_json or [])

    winner: Slot | None = None
    if winner_plan.existing_slot_id is not None:
        winner = session.get(Slot, winner_plan.existing_slot_id)
        if winner is not None and winner.meeting_id != meeting.id:
            winner = None

    warnings: list[str] = []

    # --- step 2: promote the winner -----------------------------------
    if winner is not None and winner.gcal_event_id:
        try:
            ref = calendar.update_event(
                winner.gcal_event_id,
                promotion_patch(
                    title=meeting.title,
                    start_utc=winner_plan.start_utc,
                    end_utc=winner_plan.end_utc,
                    timezone_name=tz_name,
                    attendees=attendees or None,
                ),
                send_updates="all" if attendees else "none",
            )
            winner.gcal_etag = ref.etag
        except CalendarNotFound:
            # Deleted in the calendar UI between proposal and confirmation.
            winner.gcal_event_id = None
            warnings.append("The placeholder had already been deleted, so I created a fresh event.")
        except CalendarError as exc:
            raise MeetingOperationError(
                f"I could not promote the slot to confirmed: {exc} Nothing was changed."
            ) from exc

    if winner is None or not winner.gcal_event_id:
        key = f"{command_id}:confirm"
        if winner is None:
            winner = Slot(
                meeting_id=meeting.id,
                start_utc=winner_plan.start_utc,
                end_utc=winner_plan.end_utc,
                state=SlotState.suggested,
                idempotency_key=key,
            )
            session.add(winner)
            session.flush()
        else:
            winner.idempotency_key = key
        body = build_event_body(
            title=meeting.title,
            start_utc=winner_plan.start_utc,
            end_utc=winner_plan.end_utc,
            timezone_name=tz_name,
            tentative=False,
            meeting_id=meeting.id,
            idempotency_key=key,
            attendees=attendees or None,
        )
        try:
            existing = calendar.find_by_idempotency_key(key)
            ref = (
                None
                if existing
                else calendar.create_event(body, send_updates="all" if attendees else "none")
            )
            winner.gcal_event_id = existing["id"] if existing else ref.event_id  # type: ignore[union-attr]
            winner.gcal_etag = existing.get("etag") if existing else ref.etag  # type: ignore[union-attr]
        except CalendarError as exc:
            raise MeetingOperationError(
                f"I could not create the confirmed event: {exc} Nothing was changed."
            ) from exc

    winner.start_utc = winner_plan.start_utc
    winner.end_utc = winner_plan.end_utc
    winner.state = SlotState.fixed
    winner.removed_at = None

    # --- step 3/4: delete siblings ------------------------------------
    siblings = [
        s
        for s in meeting.slots
        if s.id != winner.id and s.state in (SlotState.suggested, SlotState.fixed)
    ]
    failed: list[Slot] = []
    removed = 0
    for sibling in siblings:
        if not sibling.gcal_event_id:
            sibling.state = SlotState.removed
            sibling.removed_at = utcnow()
            removed += 1
            continue
        try:
            calendar.delete_event(sibling.gcal_event_id, send_updates="none")
        except CalendarError as exc:
            # Keep the row so the event is never orphaned; retried by the job.
            sibling.state = SlotState.failed
            failed.append(sibling)
            log.error(
                "sibling_delete_failed",
                extra={"slot_id": sibling.id, "meeting_id": meeting.id, "error": str(exc)},
            )
            continue
        sibling.state = SlotState.removed
        sibling.removed_at = utcnow()
        removed += 1

    meeting.status = MeetingStatus.confirmed
    meeting.confirmed_at = utcnow()
    meeting.duration_minutes = int(
        (winner_plan.end_utc - winner_plan.start_utc).total_seconds() // 60
    )
    if attendees:
        meeting.attendees_json = attendees
    if voice_command is not None:
        voice_command.resolved_meeting_id = meeting.id

    local = winner_plan.start_utc.astimezone(ZoneInfo(tz_name))
    base = (
        f"“{meeting.title}” is confirmed for {local:%a %d %b, %H:%M} ({tz_name}). "
        f"{removed} placeholder{'s' if removed != 1 else ''} removed."
    )
    if failed:
        return ApplyResult(
            ok=True,
            message=(
                base
                + f"\n\n⚠️ {len(failed)} placeholder"
                + ("s" if len(failed) != 1 else "")
                + " could not be deleted from Google Calendar. They are still "
                "yellow in your calendar and I will keep retrying — I will tell "
                "you when they are gone."
            ),
            meeting_id=meeting.id,
            partial=True,
            needs_cleanup_retry=True,
            warnings=warnings,
        )
    return ApplyResult(
        ok=True, message=base, meeting_id=meeting.id, warnings=warnings
    )


# ---------------------------------------------------------------------------
# T-21 — cancel and reschedule
# ---------------------------------------------------------------------------

def cancel_meeting(
    session: Session, user: User, plan: Plan, calendar: CalendarClient
) -> ApplyResult:
    meeting = session.get(Meeting, plan.meeting_id)
    if meeting is None or meeting.user_id != user.id:
        raise MeetingOperationError("That meeting no longer exists.")

    had_attendees = bool(meeting.attendees_json)
    failed = 0
    removed = 0
    for slot in meeting.slots:
        if slot.state not in (SlotState.suggested, SlotState.fixed):
            continue
        if not slot.gcal_event_id:
            slot.state = SlotState.removed
            slot.removed_at = utcnow()
            removed += 1
            continue
        try:
            calendar.delete_event(
                slot.gcal_event_id,
                send_updates="all" if (had_attendees and slot.state is SlotState.fixed) else "none",
            )
        except CalendarError as exc:
            slot.state = SlotState.failed
            failed += 1
            log.error("cancel_delete_failed", extra={"slot_id": slot.id, "error": str(exc)})
            continue
        slot.state = SlotState.removed
        slot.removed_at = utcnow()
        removed += 1

    meeting.status = MeetingStatus.cancelled
    plural = "s" if removed != 1 else ""
    message = f"Cancelled “{meeting.title}” and removed {removed} event{plural}."
    if failed:
        message += (
            f"\n\n⚠️ {failed} event{'s' if failed != 1 else ''} could not be deleted. "
            "I will keep retrying."
        )
    return ApplyResult(
        ok=True,
        message=message,
        meeting_id=meeting.id,
        partial=bool(failed),
        needs_cleanup_retry=bool(failed),
    )


def reschedule_meeting(
    session: Session,
    user: User,
    plan: Plan,
    calendar: CalendarClient,
    *,
    voice_command: VoiceCommand | None = None,
) -> ApplyResult:
    meeting = session.get(Meeting, plan.meeting_id)
    if meeting is None or meeting.user_id != user.id:
        raise MeetingOperationError("That meeting no longer exists.")
    if meeting.status is MeetingStatus.cancelled:
        raise MeetingOperationError(f"“{meeting.title}” was cancelled; create it again instead.")

    new = plan.slots[0]
    attendees = list(meeting.attendees_json or [])
    tz_name = new.timezone_name

    if meeting.status is MeetingStatus.proposed:
        # Still a proposal: this is a confirmation at a new time.
        confirm_plan = plan.model_copy(update={"action": "confirm"})
        return collapse_to_confirmed(
            session, user, confirm_plan, calendar, voice_command=voice_command
        )

    live = [s for s in meeting.slots if s.state is SlotState.fixed]
    if not live:
        raise MeetingOperationError(
            f"“{meeting.title}” has no active event to move. Create it again instead."
        )

    target = live[0]
    if not target.gcal_event_id:
        raise MeetingOperationError(f"“{meeting.title}” has no calendar event to move.")

    try:
        ref = calendar.update_event(
            target.gcal_event_id,
            promotion_patch(
                title=meeting.title,
                start_utc=new.start_utc,
                end_utc=new.end_utc,
                timezone_name=tz_name,
                attendees=attendees or None,
            ),
            # Guests must be told the meeting moved.
            send_updates="all" if attendees else "none",
        )
        target.gcal_etag = ref.etag
    except CalendarNotFound as exc:
        raise MeetingOperationError(
            "That event is no longer in your calendar, so there is nothing to move."
        ) from exc
    except CalendarError as exc:
        raise MeetingOperationError(
            f"I could not move that event: {exc} Nothing was changed."
        ) from exc

    target.start_utc = new.start_utc
    target.end_utc = new.end_utc
    meeting.duration_minutes = int((new.end_utc - new.start_utc).total_seconds() // 60)

    local = new.start_utc.astimezone(ZoneInfo(tz_name))
    note = " Guests have been notified." if attendees else ""
    return ApplyResult(
        ok=True,
        message=f"Moved “{meeting.title}” to {local:%a %d %b, %H:%M} ({tz_name}).{note}",
        meeting_id=meeting.id,
    )


# ---------------------------------------------------------------------------
# Cleanup retry (used by the background retry and the daily cron)
# ---------------------------------------------------------------------------

def retry_failed_deletions(
    session: Session, user: User, calendar: CalendarClient, *, meeting_id: int | None = None
) -> tuple[int, int]:
    """Retry deleting slots left in ``failed``. Returns (cleared, still_failing)."""
    query = (
        session.query(Slot)
        .join(Meeting, Slot.meeting_id == Meeting.id)
        .filter(Meeting.user_id == user.id, Slot.state == SlotState.failed)
    )
    if meeting_id is not None:
        query = query.filter(Slot.meeting_id == meeting_id)

    cleared = failing = 0
    for slot in query.all():
        if not slot.gcal_event_id:
            slot.state = SlotState.removed
            slot.removed_at = utcnow()
            cleared += 1
            continue
        try:
            calendar.delete_event(slot.gcal_event_id, send_updates="none")
        except CalendarError as exc:
            failing += 1
            log.warning("cleanup_retry_failed", extra={"slot_id": slot.id, "error": str(exc)})
            continue
        slot.state = SlotState.removed
        slot.removed_at = utcnow()
        cleared += 1
    return cleared, failing


def _fmt(planned) -> str:
    local = planned.start_utc.astimezone(ZoneInfo(planned.timezone_name))
    return f"{local:%a %d %b %H:%M}"

