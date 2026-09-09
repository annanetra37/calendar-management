"""Regression fixtures for the extraction + resolution layers (T-25).

30+ transcripts across the four supported languages (D4), covering: single
fixed, multi-slot suggested, confirmation by title, confirmation by time only,
ambiguous confirmation, cancellation, past dates, missing duration and garbled
audio.

``expected`` describes what the *plan* layer must produce. Where the LLM is
involved, ``intent`` supplies the extraction output a competent model should
return, so the deterministic layers below it can be tested on their own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.nlu.schema import ExtractedIntent, Intent, SpokenSlot


@dataclass(slots=True)
class Fixture:
    id: str
    language: str
    transcript: str
    intent: ExtractedIntent
    expected_action: str
    expected_slot_count: int = 0
    expected_local_times: list[str] = field(default_factory=list)
    expected_title: str | None = None
    seed_meetings: list[tuple[str, list[str]]] = field(default_factory=list)
    notes: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def _slot(raw: str, date: str | None = None, time: str | None = None, tz: str | None = None) -> SpokenSlot:
    return SpokenSlot(raw=raw, date=date, time=time, timezone=tz)


#: "now" for every fixture: Monday 14 September 2026, 13:00 Asia/Yerevan.
NOW_LOCAL = "2026-09-14 13:00"

FIXTURES: list[Fixture] = [
    # ---------------- single fixed -------------------------------------
    Fixture(
        id="en-fixed-basic",
        language="en",
        transcript="Board call, Tuesday the 15th, 3pm, fixed.",
        intent=ExtractedIntent(
            intent=Intent.CREATE_FIXED,
            title="Board call",
            slots=[_slot("Tuesday the 15th, 3pm", "2026-09-15", "15:00")],
            confidence=0.95,
        ),
        expected_action="create_fixed",
        expected_slot_count=1,
        expected_local_times=["2026-09-15 15:00"],
        expected_title="Board call",
    ),
    Fixture(
        id="en-fixed-tomorrow",
        language="en",
        transcript="Fix a call with the auditors tomorrow at half past nine in the morning.",
        intent=ExtractedIntent(
            intent=Intent.CREATE_FIXED,
            title="Call with the auditors",
            slots=[_slot("tomorrow at half past nine in the morning", None, "09:30")],
            confidence=0.9,
        ),
        expected_action="create_fixed",
        expected_slot_count=1,
        expected_local_times=["2026-09-15 09:30"],
    ),
    Fixture(
        id="de-fixed-halb",
        language="de",
        transcript="Termin mit Herrn Weber, Donnerstag um halb drei, fix.",
        intent=ExtractedIntent(
            intent=Intent.CREATE_FIXED,
            title="Termin mit Herrn Weber",
            slots=[_slot("Donnerstag um halb drei", None, None)],
            confidence=0.88,
        ),
        expected_action="create_fixed",
        expected_slot_count=1,
        expected_local_times=["2026-09-17 14:30"],
        notes="German 'halb drei' is 14:30, not 15:30 — the classic trap.",
    ),
    Fixture(
        id="ru-fixed",
        language="ru",
        transcript="Поставь встречу с юристами в четверг в 11 утра, точно.",
        intent=ExtractedIntent(
            intent=Intent.CREATE_FIXED,
            title="Встреча с юристами",
            slots=[_slot("в четверг в 11 утра", None, "11:00")],
            confidence=0.9,
        ),
        expected_action="create_fixed",
        expected_slot_count=1,
        expected_local_times=["2026-09-17 11:00"],
    ),
    Fixture(
        id="hy-fixed",
        language="hy",
        transcript="Ֆիքսիր հանդիպումը Արամի հետ չորեքշաբթի ժամը 16:00։",
        intent=ExtractedIntent(
            intent=Intent.CREATE_FIXED,
            title="Հանդիպում Արամի հետ",
            slots=[_slot("չորեքշաբթի ժամը 16:00", None, "16:00")],
            confidence=0.85,
        ),
        expected_action="create_fixed",
        expected_slot_count=1,
        expected_local_times=["2026-09-16 16:00"],
    ),
    Fixture(
        id="en-fixed-iso-time",
        language="en",
        transcript="Fixed: quarterly review on the 21st at 09:00.",
        intent=ExtractedIntent(
            intent=Intent.CREATE_FIXED,
            title="Quarterly review",
            slots=[_slot("on the 21st at 09:00", "2026-09-21", "09:00")],
            confidence=0.93,
        ),
        expected_action="create_fixed",
        expected_slot_count=1,
        expected_local_times=["2026-09-21 09:00"],
    ),

    # ---------------- multi-slot suggested ------------------------------
    Fixture(
        id="en-suggested-three",
        language="en",
        transcript=(
            "Suggest Evocabank meeting Tuesday 2pm, Wednesday 10, Thursday 4."
        ),
        intent=ExtractedIntent(
            intent=Intent.CREATE_SUGGESTED,
            title="Evocabank meeting",
            slots=[
                _slot("Tuesday 2pm", "2026-09-15", "14:00"),
                _slot("Wednesday 10", "2026-09-16", "10:00"),
                _slot("Thursday 4", "2026-09-17", "16:00"),
            ],
            confidence=0.92,
        ),
        expected_action="create_suggested",
        expected_slot_count=3,
        expected_local_times=[
            "2026-09-15 14:00",
            "2026-09-16 10:00",
            "2026-09-17 16:00",
        ],
        expected_title="Evocabank meeting",
        notes="The canonical yellow-placeholder case.",
    ),
    Fixture(
        id="en-suggested-two",
        language="en",
        transcript="Propose the Ameria call for Wednesday at 11 or Friday at 3.",
        intent=ExtractedIntent(
            intent=Intent.CREATE_SUGGESTED,
            title="Ameria call",
            slots=[_slot("Wednesday at 11", "2026-09-16", "11:00"), _slot("Friday at 3")],
            confidence=0.9,
        ),
        expected_action="create_suggested",
        expected_slot_count=2,
        expected_local_times=["2026-09-16 11:00", "2026-09-18 15:00"],
    ),
    Fixture(
        id="en-suggested-single",
        language="en",
        transcript="Pencil in a coffee with Nare next Tuesday at 3.",
        intent=ExtractedIntent(
            intent=Intent.CREATE_SUGGESTED,
            title="Coffee with Nare",
            slots=[_slot("next Tuesday at 3")],
            confidence=0.86,
        ),
        expected_action="create_suggested",
        expected_slot_count=1,
        expected_local_times=["2026-09-22 15:00"],
        notes="'next Tuesday' on a Monday must be the FOLLOWING week (T-13).",
    ),
    Fixture(
        id="de-suggested-three",
        language="de",
        transcript=(
            "Schlage für das Investorengespräch Dienstag 14 Uhr, Mittwoch 10 Uhr "
            "und Donnerstag 16 Uhr vor."
        ),
        intent=ExtractedIntent(
            intent=Intent.CREATE_SUGGESTED,
            title="Investorengespräch",
            slots=[
                _slot("Dienstag 14 Uhr", "2026-09-15", "14:00"),
                _slot("Mittwoch 10 Uhr", "2026-09-16", "10:00"),
                _slot("Donnerstag 16 Uhr", "2026-09-17", "16:00"),
            ],
            confidence=0.9,
        ),
        expected_action="create_suggested",
        expected_slot_count=3,
        expected_local_times=[
            "2026-09-15 14:00",
            "2026-09-16 10:00",
            "2026-09-17 16:00",
        ],
    ),
    Fixture(
        id="ru-suggested-two",
        language="ru",
        transcript="Предложи созвон с Ардшинбанком во вторник в 15 или в среду в 10.",
        intent=ExtractedIntent(
            intent=Intent.CREATE_SUGGESTED,
            title="Созвон с Ардшинбанком",
            slots=[
                _slot("во вторник в 15", "2026-09-15", "15:00"),
                _slot("в среду в 10", "2026-09-16", "10:00"),
            ],
            confidence=0.87,
        ),
        expected_action="create_suggested",
        expected_slot_count=2,
        expected_local_times=["2026-09-15 15:00", "2026-09-16 10:00"],
    ),
    Fixture(
        id="hy-suggested-two",
        language="hy",
        transcript="Առաջարկիր Էվոկաբանկի հանդիպումը երեքշաբթի 14:00 կամ հինգշաբթի 16:00։",
        intent=ExtractedIntent(
            intent=Intent.CREATE_SUGGESTED,
            title="Էվոկաբանկի հանդիպում",
            slots=[
                _slot("երեքշաբթի 14:00", "2026-09-15", "14:00"),
                _slot("հինգշաբթի 16:00", "2026-09-17", "16:00"),
            ],
            confidence=0.84,
        ),
        expected_action="create_suggested",
        expected_slot_count=2,
        expected_local_times=["2026-09-15 14:00", "2026-09-17 16:00"],
    ),
    Fixture(
        id="en-suggested-duplicate-times",
        language="en",
        transcript="Suggest the Unibank sync Tuesday 2pm, Tuesday 2pm, Thursday 4.",
        intent=ExtractedIntent(
            intent=Intent.CREATE_SUGGESTED,
            title="Unibank sync",
            slots=[
                _slot("Tuesday 2pm", "2026-09-15", "14:00"),
                _slot("Tuesday 2pm", "2026-09-15", "14:00"),
                _slot("Thursday 4"),
            ],
            confidence=0.8,
        ),
        expected_action="create_suggested",
        expected_slot_count=2,
        notes="Repeated times must collapse to one slot.",
    ),

    # ---------------- confirmation by title -----------------------------
    Fixture(
        id="en-confirm-by-title",
        language="en",
        transcript="Evocabank is confirmed for Wednesday 10.",
        intent=ExtractedIntent(
            intent=Intent.CONFIRM_SLOT,
            meeting_reference="Evocabank",
            slots=[_slot("Wednesday 10", "2026-09-16", "10:00")],
            confidence=0.93,
        ),
        seed_meetings=[
            ("Evocabank meeting", ["2026-09-15 14:00", "2026-09-16 10:00", "2026-09-17 16:00"])
        ],
        expected_action="confirm",
        expected_slot_count=1,
        expected_local_times=["2026-09-16 10:00"],
        extra={"expected_siblings": 2},
    ),
    Fixture(
        id="en-confirm-fuzzy-title",
        language="en",
        transcript="The Evoca bank one is agreed for Thursday at four.",
        intent=ExtractedIntent(
            intent=Intent.CONFIRM_SLOT,
            meeting_reference="Evoca bank",
            slots=[_slot("Thursday at four")],
            confidence=0.8,
        ),
        seed_meetings=[
            ("Evocabank meeting", ["2026-09-15 14:00", "2026-09-17 16:00"]),
            ("Board call", ["2026-09-18 11:00"]),
        ],
        expected_action="confirm",
        expected_local_times=["2026-09-17 16:00"],
        notes="Transcription drift in a proper noun must still match.",
    ),
    Fixture(
        id="ru-confirm-by-title",
        language="ru",
        transcript="Ардшинбанк подтверждён на среду в 10.",
        intent=ExtractedIntent(
            intent=Intent.CONFIRM_SLOT,
            meeting_reference="Ардшинбанк",
            slots=[_slot("на среду в 10", "2026-09-16", "10:00")],
            confidence=0.9,
        ),
        seed_meetings=[("Созвон с Ардшинбанком", ["2026-09-15 15:00", "2026-09-16 10:00"])],
        expected_action="confirm",
        expected_local_times=["2026-09-16 10:00"],
    ),
    Fixture(
        id="de-confirm-by-title",
        language="de",
        transcript="Das Investorengespräch ist für Mittwoch 10 Uhr bestätigt.",
        intent=ExtractedIntent(
            intent=Intent.CONFIRM_SLOT,
            meeting_reference="Investorengespräch",
            slots=[_slot("Mittwoch 10 Uhr", "2026-09-16", "10:00")],
            confidence=0.91,
        ),
        seed_meetings=[("Investorengespräch", ["2026-09-15 14:00", "2026-09-16 10:00"])],
        expected_action="confirm",
        expected_local_times=["2026-09-16 10:00"],
    ),

    # ---------------- confirmation by time only -------------------------
    Fixture(
        id="en-confirm-time-only",
        language="en",
        transcript="Confirmed for Wednesday at 10.",
        intent=ExtractedIntent(
            intent=Intent.CONFIRM_SLOT,
            meeting_reference=None,
            slots=[_slot("Wednesday at 10", "2026-09-16", "10:00")],
            confidence=0.7,
        ),
        seed_meetings=[
            ("Evocabank meeting", ["2026-09-16 10:00"]),
            ("Board call", ["2026-09-18 11:00"]),
        ],
        expected_action="confirm",
        expected_local_times=["2026-09-16 10:00"],
        notes="Unique time match across all open meetings is decisive.",
    ),
    Fixture(
        id="en-confirm-new-time",
        language="en",
        transcript="Evocabank is confirmed, but for Friday at 9 instead.",
        intent=ExtractedIntent(
            intent=Intent.CONFIRM_SLOT,
            meeting_reference="Evocabank",
            slots=[_slot("Friday at 9", "2026-09-18", "09:00")],
            confidence=0.88,
        ),
        seed_meetings=[("Evocabank meeting", ["2026-09-15 14:00", "2026-09-16 10:00"])],
        expected_action="confirm",
        expected_local_times=["2026-09-18 09:00"],
        extra={"expected_siblings": 2},
        notes="A winning time that was never proposed must still work (T-20).",
    ),

    # ---------------- ambiguous confirmation ----------------------------
    Fixture(
        id="en-confirm-ambiguous",
        language="en",
        transcript="The call is confirmed.",
        intent=ExtractedIntent(
            intent=Intent.CONFIRM_SLOT,
            meeting_reference="the call",
            slots=[],
            confidence=0.4,
            ambiguities=["Two open meetings contain the word 'call'."],
        ),
        seed_meetings=[
            ("Board call", ["2026-09-16 11:00"]),
            ("Client call", ["2026-09-17 15:00"]),
        ],
        expected_action="clarify",
        notes="T-19 acceptance: must ask, never guess.",
    ),
    Fixture(
        id="en-confirm-ambiguous-time",
        language="en",
        transcript="That one is confirmed for Wednesday 10.",
        intent=ExtractedIntent(
            intent=Intent.CONFIRM_SLOT,
            meeting_reference=None,
            slots=[_slot("Wednesday 10", "2026-09-16", "10:00")],
            confidence=0.45,
        ),
        seed_meetings=[
            ("Evocabank meeting", ["2026-09-16 10:00"]),
            ("Ameria call", ["2026-09-16 10:00"]),
        ],
        expected_action="clarify",
        notes="Two meetings propose the same time — ambiguous, so ask.",
    ),
    Fixture(
        id="en-confirm-no-time-many-slots",
        language="en",
        transcript="Evocabank is confirmed.",
        intent=ExtractedIntent(
            intent=Intent.CONFIRM_SLOT,
            meeting_reference="Evocabank",
            slots=[],
            confidence=0.6,
        ),
        seed_meetings=[("Evocabank meeting", ["2026-09-15 14:00", "2026-09-16 10:00"])],
        expected_action="clarify",
        notes="Meeting is clear but which of its slots is not.",
    ),
    Fixture(
        id="en-confirm-no-time-one-slot",
        language="en",
        transcript="Evocabank is confirmed.",
        intent=ExtractedIntent(
            intent=Intent.CONFIRM_SLOT,
            meeting_reference="Evocabank",
            slots=[],
            confidence=0.7,
        ),
        seed_meetings=[("Evocabank meeting", ["2026-09-16 10:00"])],
        expected_action="confirm",
        expected_local_times=["2026-09-16 10:00"],
        notes="Only one slot open, so no ambiguity to resolve.",
    ),

    # ---------------- cancellation --------------------------------------
    Fixture(
        id="en-cancel",
        language="en",
        transcript="Cancel the Evocabank meeting entirely.",
        intent=ExtractedIntent(
            intent=Intent.CANCEL_MEETING,
            meeting_reference="Evocabank",
            confidence=0.94,
        ),
        seed_meetings=[("Evocabank meeting", ["2026-09-15 14:00", "2026-09-16 10:00"])],
        expected_action="cancel",
        extra={"expected_siblings": 2},
    ),
    Fixture(
        id="de-cancel",
        language="de",
        transcript="Sag das Investorengespräch komplett ab.",
        intent=ExtractedIntent(
            intent=Intent.CANCEL_MEETING,
            meeting_reference="Investorengespräch",
            confidence=0.9,
        ),
        seed_meetings=[("Investorengespräch", ["2026-09-16 10:00"])],
        expected_action="cancel",
    ),
    Fixture(
        id="hy-cancel",
        language="hy",
        transcript="Չեղարկիր Էվոկաբանկի հանդիպումը։",
        intent=ExtractedIntent(
            intent=Intent.CANCEL_MEETING,
            meeting_reference="Էվոկաբանկ",
            confidence=0.86,
        ),
        seed_meetings=[("Էվոկաբանկի հանդիպում", ["2026-09-15 14:00"])],
        expected_action="cancel",
    ),
    Fixture(
        id="en-cancel-ambiguous",
        language="en",
        transcript="Cancel the call.",
        intent=ExtractedIntent(
            intent=Intent.CANCEL_MEETING,
            meeting_reference="the call",
            confidence=0.4,
        ),
        seed_meetings=[
            ("Board call", ["2026-09-16 11:00"]),
            ("Client call", ["2026-09-17 15:00"]),
        ],
        expected_action="clarify",
    ),

    # ---------------- reschedule -----------------------------------------
    Fixture(
        id="en-reschedule",
        language="en",
        transcript="Move the board call to Thursday 4pm.",
        intent=ExtractedIntent(
            intent=Intent.RESCHEDULE,
            meeting_reference="board call",
            slots=[_slot("Thursday 4pm", "2026-09-17", "16:00")],
            confidence=0.92,
        ),
        seed_meetings=[("Board call", ["2026-09-16 11:00"])],
        expected_action="reschedule",
        expected_local_times=["2026-09-17 16:00"],
        extra={"confirmed": True},
    ),
    Fixture(
        id="ru-reschedule",
        language="ru",
        transcript="Перенеси встречу с юристами на пятницу в 12.",
        intent=ExtractedIntent(
            intent=Intent.RESCHEDULE,
            meeting_reference="встреча с юристами",
            slots=[_slot("на пятницу в 12", "2026-09-18", "12:00")],
            confidence=0.89,
        ),
        seed_meetings=[("Встреча с юристами", ["2026-09-17 11:00"])],
        expected_action="reschedule",
        expected_local_times=["2026-09-18 12:00"],
        extra={"confirmed": True},
    ),
    Fixture(
        id="en-reschedule-no-time",
        language="en",
        transcript="Move the board call.",
        intent=ExtractedIntent(
            intent=Intent.RESCHEDULE,
            meeting_reference="board call",
            slots=[],
            confidence=0.5,
        ),
        seed_meetings=[("Board call", ["2026-09-16 11:00"])],
        expected_action="reject",
        extra={"confirmed": True},
    ),

    # ---------------- past dates -----------------------------------------
    Fixture(
        id="en-past-date",
        language="en",
        transcript="Fix the audit call for yesterday at 10.",
        intent=ExtractedIntent(
            intent=Intent.CREATE_FIXED,
            title="Audit call",
            slots=[_slot("yesterday at 10", "2026-09-13", "10:00")],
            confidence=0.8,
        ),
        expected_action="reject",
        notes="A past date must be refused with a clarifying reply (T-13).",
    ),
    Fixture(
        id="en-past-time-today",
        language="en",
        transcript="Book the standup today at 9 in the morning, fixed.",
        intent=ExtractedIntent(
            intent=Intent.CREATE_FIXED,
            title="Standup",
            slots=[_slot("today at 9 in the morning", "2026-09-14", "09:00")],
            confidence=0.85,
        ),
        expected_action="reject",
        notes="09:00 Yerevan is already past at 13:00 local.",
    ),

    # ---------------- missing duration -----------------------------------
    Fixture(
        id="en-missing-duration",
        language="en",
        transcript="Fix lunch with Tigran on Friday at 1.",
        intent=ExtractedIntent(
            intent=Intent.CREATE_FIXED,
            title="Lunch with Tigran",
            slots=[_slot("Friday at 1", "2026-09-18", "13:00")],
            duration_minutes=None,
            confidence=0.9,
        ),
        expected_action="create_fixed",
        expected_local_times=["2026-09-18 13:00"],
        extra={"expected_duration": 60},
        notes="D5 default of 60 minutes, surfaced as a warning on the card.",
    ),
    Fixture(
        id="en-explicit-duration",
        language="en",
        transcript="Fix a 30 minute catch-up with Anna on Friday at 2.",
        intent=ExtractedIntent(
            intent=Intent.CREATE_FIXED,
            title="Catch-up with Anna",
            slots=[_slot("Friday at 2", "2026-09-18", "14:00")],
            duration_minutes=30,
            confidence=0.92,
        ),
        expected_action="create_fixed",
        expected_local_times=["2026-09-18 14:00"],
        extra={"expected_duration": 30},
    ),

    # ---------------- timezone while travelling ---------------------------
    Fixture(
        id="en-explicit-timezone",
        language="en",
        transcript="Fix the Raiffeisen call for Wednesday at 3pm Vienna time.",
        intent=ExtractedIntent(
            intent=Intent.CREATE_FIXED,
            title="Raiffeisen call",
            slots=[_slot("Wednesday at 3pm Vienna time", "2026-09-16", "15:00", "Europe/Vienna")],
            confidence=0.9,
        ),
        expected_action="create_fixed",
        expected_local_times=["2026-09-16 15:00"],
        extra={"expected_timezone": "Europe/Vienna"},
        notes="A named zone overrides the home zone (T-14).",
    ),

    # ---------------- garbled / unusable ----------------------------------
    Fixture(
        id="en-garbled",
        language="en",
        transcript="uh so yeah the thing about the... yeah",
        intent=ExtractedIntent(
            intent=Intent.UNKNOWN,
            confidence=0.1,
            ambiguities=["The transcript does not contain a scheduling request."],
        ),
        expected_action="reject",
    ),
    Fixture(
        id="en-no-title",
        language="en",
        transcript="Suggest Tuesday at 2 and Wednesday at 10.",
        intent=ExtractedIntent(
            intent=Intent.CREATE_SUGGESTED,
            title=None,
            slots=[_slot("Tuesday at 2", "2026-09-15", "14:00"), _slot("Wednesday at 10")],
            confidence=0.6,
            ambiguities=["No meeting title was mentioned."],
        ),
        expected_action="reject",
        notes="No title means nothing usable to name the event.",
    ),
    Fixture(
        id="en-no-datetime",
        language="en",
        transcript="Suggest a meeting with the bank sometime soon.",
        intent=ExtractedIntent(
            intent=Intent.CREATE_SUGGESTED,
            title="Meeting with the bank",
            slots=[_slot("sometime soon")],
            confidence=0.5,
            ambiguities=["No specific date or time was given."],
        ),
        expected_action="reject",
    ),

    # ---------------- list pending ----------------------------------------
    Fixture(
        id="en-list-pending",
        language="en",
        transcript="What's still open?",
        intent=ExtractedIntent(intent=Intent.LIST_PENDING, confidence=0.95),
        expected_action="list_pending",
    ),
    Fixture(
        id="ru-list-pending",
        language="ru",
        transcript="Что ещё не подтверждено?",
        intent=ExtractedIntent(intent=Intent.LIST_PENDING, confidence=0.9),
        expected_action="list_pending",
    ),
]

BY_ID = {fixture.id: fixture for fixture in FIXTURES}
