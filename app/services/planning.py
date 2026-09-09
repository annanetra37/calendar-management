"""Turn an extracted intent into a concrete, reviewable plan (spec sections 5, 6).

A ``Plan`` is what the confirmation card renders and what the ✅ button
executes. Building a plan touches the database read-only and never the
Calendar API — nothing reaches the calendar before the owner taps Confirm.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Meeting, MeetingStatus, User
from app.nlu.prompt import OpenMeetingSummary
from app.nlu.schema import ExtractedIntent, Intent
from app.nlu.timeparse import TimeResolutionError, resolve_slot
from app.services.matching import normalize_title, resolve_meeting

log = logging.getLogger(__name__)

LOW_CONFIDENCE = 0.55

PlanAction = Literal[
    "create_suggested",
    "create_fixed",
    "confirm",
    "cancel",
    "reschedule",
    "list_pending",
    "clarify",
    "reject",
]


class PlannedSlot(BaseModel):
    start_utc: datetime
    end_utc: datetime
    timezone_name: str
    raw: str = ""
    existing_slot_id: int | None = None
    warnings: list[str] = Field(default_factory=list)

    def local_start(self) -> datetime:
        return self.start_utc.astimezone(ZoneInfo(self.timezone_name))

    def local_end(self) -> datetime:
        return self.end_utc.astimezone(ZoneInfo(self.timezone_name))


class CandidateMeeting(BaseModel):
    meeting_id: int
    title: str
    slot_count: int
    next_start_utc: datetime | None = None


class Plan(BaseModel):
    """Serialisable description of exactly what ✅ will do."""

    action: PlanAction
    title: str = ""
    meeting_id: int | None = None
    duration_minutes: int = 60
    slots: list[PlannedSlot] = Field(default_factory=list)
    attendees: list[str] = Field(default_factory=list)
    timezone_name: str = "UTC"
    warnings: list[str] = Field(default_factory=list)
    question: str | None = None
    candidates: list[CandidateMeeting] = Field(default_factory=list)
    sibling_slot_count: int = 0
    error: str | None = None

    @property
    def is_actionable(self) -> bool:
        return self.action not in ("clarify", "reject")

    @property
    def needs_calendar_write(self) -> bool:
        return self.action in (
            "create_suggested",
            "create_fixed",
            "confirm",
            "cancel",
            "reschedule",
        )


# ---------------------------------------------------------------------------
# Reading open meetings
# ---------------------------------------------------------------------------

def open_meetings(session: Session, user: User) -> list[Meeting]:
    stmt = (
        select(Meeting)
        .where(Meeting.user_id == user.id, Meeting.status == MeetingStatus.proposed)
        .options(selectinload(Meeting.slots))
        .order_by(Meeting.created_at.desc())
    )
    return list(session.scalars(stmt))


def active_meetings(session: Session, user: User) -> list[Meeting]:
    """Proposed *and* confirmed — reschedule and cancel apply to both."""
    stmt = (
        select(Meeting)
        .where(
            Meeting.user_id == user.id,
            Meeting.status.in_([MeetingStatus.proposed, MeetingStatus.confirmed]),
        )
        .options(selectinload(Meeting.slots))
        .order_by(Meeting.created_at.desc())
    )
    return list(session.scalars(stmt))


def summarise_for_prompt(meetings: list[Meeting], tz_name: str) -> list[OpenMeetingSummary]:
    zone = ZoneInfo(tz_name)
    summaries = []
    for meeting in meetings:
        descriptions = [
            f"{slot.start_utc.astimezone(zone):%a %d %b %H:%M}"
            for slot in meeting.live_slots()
        ]
        summaries.append(
            OpenMeetingSummary(
                meeting_id=meeting.id,
                title=meeting.title,
                slot_descriptions=descriptions,
            )
        )
    return summaries


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def build_plan(
    session: Session,
    user: User,
    intent: ExtractedIntent,
    *,
    now_utc: datetime | None = None,
) -> Plan:
    plan = _build_plan(session, user, intent, now_utc=now_utc)
    # Per-slot warnings (an assumed meridiem, a phrase that disagreed with the
    # model) must reach the card, not just the log.
    merged = list(plan.warnings)
    for slot in plan.slots:
        merged.extend(slot.warnings)
    plan.warnings = list(dict.fromkeys(merged))
    return plan


def _build_plan(
    session: Session,
    user: User,
    intent: ExtractedIntent,
    *,
    now_utc: datetime | None = None,
) -> Plan:
    now_utc = now_utc or datetime.now(UTC)
    tz_name = user.home_timezone
    duration = intent.duration_minutes or user.default_duration_minutes

    if intent.intent is Intent.UNKNOWN:
        return Plan(
            action="reject",
            timezone_name=tz_name,
            error=(
                intent.ambiguities[0]
                if intent.ambiguities
                else "I could not work out what you wanted me to do."
            ),
        )

    if intent.intent is Intent.LIST_PENDING:
        return Plan(action="list_pending", timezone_name=tz_name)

    if intent.intent in (Intent.CREATE_FIXED, Intent.CREATE_SUGGESTED):
        return _plan_create(intent, user, duration, now_utc, tz_name)

    if intent.intent is Intent.CONFIRM_SLOT:
        return _plan_confirm(session, user, intent, duration, now_utc, tz_name)

    if intent.intent is Intent.CANCEL_MEETING:
        return _plan_cancel(session, user, intent, now_utc, tz_name)

    if intent.intent is Intent.RESCHEDULE:
        return _plan_reschedule(session, user, intent, duration, now_utc, tz_name)

    return Plan(action="reject", timezone_name=tz_name, error="Unsupported request.")


def _resolve_all(
    intent: ExtractedIntent,
    *,
    now_utc: datetime,
    tz_name: str,
    duration: int,
) -> tuple[list[PlannedSlot], list[str]]:
    planned: list[PlannedSlot] = []
    problems: list[str] = []
    for spoken in intent.slots:
        try:
            resolved = resolve_slot(
                spoken,
                now_utc=now_utc,
                home_timezone=tz_name,
                duration_minutes=duration,
            )
        except TimeResolutionError as exc:
            problems.append(str(exc))
            continue
        planned.append(
            PlannedSlot(
                start_utc=resolved.start_utc,
                end_utc=resolved.end_utc,
                timezone_name=resolved.timezone_name,
                raw=spoken.raw,
                warnings=resolved.warnings,
            )
        )
    return _dedupe(planned), problems


def _dedupe(slots: list[PlannedSlot]) -> list[PlannedSlot]:
    seen: set[datetime] = set()
    unique: list[PlannedSlot] = []
    for slot in slots:
        if slot.start_utc in seen:
            continue
        seen.add(slot.start_utc)
        unique.append(slot)
    return unique


def _base_warnings(intent: ExtractedIntent) -> list[str]:
    warnings = list(intent.ambiguities)
    if intent.confidence and intent.confidence < LOW_CONFIDENCE:
        warnings.append(
            f"I am only {intent.confidence:.0%} sure I understood this — please check it."
        )
    return warnings


def _plan_create(
    intent: ExtractedIntent, user: User, duration: int, now_utc: datetime, tz_name: str
) -> Plan:
    slots, problems = _resolve_all(intent, now_utc=now_utc, tz_name=tz_name, duration=duration)
    warnings = _base_warnings(intent) + problems

    title = (intent.title or "").strip()
    if not title:
        return Plan(
            action="reject",
            timezone_name=tz_name,
            warnings=warnings,
            error="I did not catch what the meeting is about. Please say it again with a title.",
        )
    if not slots:
        return Plan(
            action="reject",
            title=title,
            timezone_name=tz_name,
            warnings=warnings,
            error=problems[0] if problems else "I did not catch a date and time.",
        )

    fixed = intent.intent is Intent.CREATE_FIXED
    if fixed and len(slots) > 1:
        warnings.append(
            "You gave several times for a fixed meeting — I kept the first and "
            "ignored the rest. Say “suggest” if you meant them as options."
        )
        slots = slots[:1]
    if intent.duration_minutes is None:
        warnings.append(f"Duration not stated — using {duration} minutes.")

    return Plan(
        action="create_fixed" if fixed else "create_suggested",
        title=title,
        duration_minutes=duration,
        slots=slots,
        attendees=intent.attendees if fixed else [],
        timezone_name=slots[0].timezone_name,
        warnings=warnings,
    )


def _plan_confirm(
    session: Session,
    user: User,
    intent: ExtractedIntent,
    duration: int,
    now_utc: datetime,
    tz_name: str,
) -> Plan:
    meetings = open_meetings(session, user)
    warnings = _base_warnings(intent)
    slots, problems = _resolve_all(intent, now_utc=now_utc, tz_name=tz_name, duration=duration)
    warnings += problems

    resolution = resolve_meeting(
        meetings,
        reference=intent.meeting_reference or intent.title,
        explicit_meeting_id=intent.meeting_id,
        spoken_starts=[s.start_utc for s in slots],
    )
    if not resolution.is_resolved:
        return _clarify(resolution, tz_name, warnings)

    meeting = resolution.meeting
    assert meeting is not None
    meeting_duration = meeting.duration_minutes or duration

    if resolution.matched_slot is not None:
        winner_slot = resolution.matched_slot
        winner = PlannedSlot(
            start_utc=winner_slot.start_utc,
            end_utc=winner_slot.end_utc,
            timezone_name=tz_name,
            existing_slot_id=winner_slot.id,
        )
    elif slots:
        # A brand-new time that was never proposed — explicitly supported (T-20).
        spoken = slots[0]
        winner = PlannedSlot(
            start_utc=spoken.start_utc,
            end_utc=spoken.start_utc.replace(microsecond=0)
            + (spoken.end_utc - spoken.start_utc),
            timezone_name=spoken.timezone_name,
            raw=spoken.raw,
            warnings=spoken.warnings,
        )
        matching = [
            s
            for s in meeting.live_slots()
            if abs(s.start_utc - winner.start_utc).total_seconds() < 60
        ]
        if matching:
            winner.existing_slot_id = matching[0].id
        else:
            warnings.append(
                "That time was not one of the proposed slots — I will create it "
                "and remove the placeholders."
            )
    else:
        live = meeting.live_slots()
        if len(live) == 1:
            winner = PlannedSlot(
                start_utc=live[0].start_utc,
                end_utc=live[0].end_utc,
                timezone_name=tz_name,
                existing_slot_id=live[0].id,
            )
        else:
            return Plan(
                action="clarify",
                timezone_name=tz_name,
                warnings=warnings,
                question=(
                    f"Which time is “{meeting.title}” confirmed for? "
                    "I have several placeholders open."
                ),
                candidates=[_candidate(meeting)],
            )

    siblings = [
        s
        for s in meeting.live_slots()
        if s.id != winner.existing_slot_id
    ]
    return Plan(
        action="confirm",
        title=meeting.title,
        meeting_id=meeting.id,
        duration_minutes=meeting_duration,
        slots=[winner],
        attendees=intent.attendees or list(meeting.attendees_json or []),
        timezone_name=winner.timezone_name,
        warnings=warnings,
        sibling_slot_count=len(siblings),
    )


def _plan_cancel(
    session: Session, user: User, intent: ExtractedIntent, now_utc: datetime, tz_name: str
) -> Plan:
    meetings = active_meetings(session, user)
    warnings = _base_warnings(intent)
    resolution = resolve_meeting(
        meetings,
        reference=intent.meeting_reference or intent.title,
        explicit_meeting_id=intent.meeting_id,
    )
    if not resolution.is_resolved:
        return _clarify(resolution, tz_name, warnings)
    meeting = resolution.meeting
    assert meeting is not None
    return Plan(
        action="cancel",
        title=meeting.title,
        meeting_id=meeting.id,
        timezone_name=tz_name,
        warnings=warnings,
        sibling_slot_count=len(meeting.live_slots()),
        slots=[
            PlannedSlot(
                start_utc=s.start_utc,
                end_utc=s.end_utc,
                timezone_name=tz_name,
                existing_slot_id=s.id,
            )
            for s in meeting.live_slots()
        ],
    )


def _plan_reschedule(
    session: Session,
    user: User,
    intent: ExtractedIntent,
    duration: int,
    now_utc: datetime,
    tz_name: str,
) -> Plan:
    meetings = active_meetings(session, user)
    warnings = _base_warnings(intent)
    slots, problems = _resolve_all(intent, now_utc=now_utc, tz_name=tz_name, duration=duration)
    warnings += problems

    resolution = resolve_meeting(
        meetings,
        reference=intent.meeting_reference or intent.title,
        explicit_meeting_id=intent.meeting_id,
    )
    if not resolution.is_resolved:
        return _clarify(resolution, tz_name, warnings)
    meeting = resolution.meeting
    assert meeting is not None

    if not slots:
        return Plan(
            action="reject",
            title=meeting.title,
            meeting_id=meeting.id,
            timezone_name=tz_name,
            warnings=warnings,
            error=f"I did not catch the new time for “{meeting.title}”.",
        )

    meeting_duration = meeting.duration_minutes or duration
    new_slot = slots[0]
    new_slot.end_utc = new_slot.start_utc + (new_slot.end_utc - new_slot.start_utc)
    return Plan(
        action="reschedule",
        title=meeting.title,
        meeting_id=meeting.id,
        duration_minutes=meeting_duration,
        slots=[new_slot],
        attendees=list(meeting.attendees_json or []),
        timezone_name=new_slot.timezone_name,
        warnings=warnings,
        sibling_slot_count=len(meeting.live_slots()),
    )


def _clarify(resolution, tz_name: str, warnings: list[str]) -> Plan:
    return Plan(
        action="clarify",
        timezone_name=tz_name,
        warnings=warnings,
        question=resolution.question or "Which meeting did you mean?",
        candidates=[_candidate(c.meeting) for c in resolution.candidates[:8]],
    )


def _candidate(meeting: Meeting) -> CandidateMeeting:
    live = meeting.live_slots()
    return CandidateMeeting(
        meeting_id=meeting.id,
        title=meeting.title,
        slot_count=len(live),
        next_start_utc=min((s.start_utc for s in live), default=None),
    )


def normalized(title: str) -> str:
    return normalize_title(title)


__all__ = [
    "CandidateMeeting",
    "Plan",
    "PlannedSlot",
    "active_meetings",
    "build_plan",
    "normalized",
    "open_meetings",
    "summarise_for_prompt",
]
