"""T-15 / T-17 / T-22 / T-27 / T-28: cards, allow-list, limits and crypto."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import Session

from app.crypto import TokenDecryptionError, decrypt, encrypt, generate_key
from app.models import CommandStatus, MeetingStatus, SlotState, User, VoiceCommand
from app.nlu.schema import ExtractedIntent, Intent, SpokenSlot
from app.services.cards import (
    card_buttons,
    clarification_buttons,
    render_card,
    render_clarification,
    render_pending,
)
from app.services.planning import build_plan, open_meetings
from app.services.users import RateLimited, check_rate_limit, get_or_create_user, is_allowed
from tests.conftest import make_meeting

NOW = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)
YEREVAN = ZoneInfo("Asia/Yerevan")


def _local(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=YEREVAN).astimezone(
        UTC
    )


# --- T-15: the confirmation card --------------------------------------------

def test_the_suggested_card_lists_every_slot_with_its_timezone(
    session: Session, user: User
) -> None:
    intent = ExtractedIntent(
        intent=Intent.CREATE_SUGGESTED,
        title="Evocabank sync",
        slots=[
            SpokenSlot(raw="Tuesday 2pm", date="2026-09-15", time="14:00"),
            SpokenSlot(raw="Wednesday 10", date="2026-09-16", time="10:00"),
            SpokenSlot(raw="Thursday 4", date="2026-09-17", time="16:00"),
        ],
        confidence=0.9,
    )
    card = render_card(build_plan(session, user, intent, now_utc=NOW))

    assert "Evocabank sync" in card
    assert "SUGGESTED — 3 slots" in card
    assert "Tue 15 Sep, 14:00–15:00 (Yerevan)" in card
    assert "Wed 16 Sep, 10:00–11:00 (Yerevan)" in card
    assert "Thu 17 Sep, 16:00–17:00 (Yerevan)" in card


def test_the_card_always_names_the_timezone_it_interpreted(
    session: Session, user: User
) -> None:
    """T-14 acceptance."""
    intent = ExtractedIntent(
        intent=Intent.CREATE_FIXED,
        title="Raiffeisen call",
        slots=[
            SpokenSlot(
                raw="Wednesday 3pm Vienna time",
                date="2026-09-16",
                time="15:00",
                timezone="Europe/Vienna",
            )
        ],
        confidence=0.9,
    )
    card = render_card(build_plan(session, user, intent, now_utc=NOW))
    assert "(Vienna)" in card


def test_the_confirm_card_says_how_many_placeholders_it_will_remove(
    session: Session, user: User
) -> None:
    make_meeting(
        session,
        user,
        "Evocabank sync",
        [_local("2026-09-15 14:00"), _local("2026-09-16 10:00"), _local("2026-09-17 16:00")],
    )
    intent = ExtractedIntent(
        intent=Intent.CONFIRM_SLOT,
        meeting_reference="Evocabank",
        slots=[SpokenSlot(raw="Wednesday 10", date="2026-09-16", time="10:00")],
        confidence=0.9,
    )
    card = render_card(build_plan(session, user, intent, now_utc=NOW))
    assert "removes 2 other placeholders" in card


def test_warnings_appear_on_the_card(session: Session, user: User) -> None:
    intent = ExtractedIntent(
        intent=Intent.CREATE_FIXED,
        title="Standup",
        slots=[SpokenSlot(raw="Friday at 4")],
        confidence=0.3,
    )
    card = render_card(build_plan(session, user, intent, now_utc=NOW))
    assert "⚠️" in card
    assert "business-hours reading" in card
    assert "30% sure" in card


def test_card_content_is_html_escaped(session: Session, user: User) -> None:
    intent = ExtractedIntent(
        intent=Intent.CREATE_FIXED,
        title="<script>alert(1)</script>",
        slots=[SpokenSlot(raw="Friday at 2pm", date="2026-09-18", time="14:00")],
        confidence=0.9,
    )
    card = render_card(build_plan(session, user, intent, now_utc=NOW))
    assert "<script>" not in card
    assert "&lt;script&gt;" in card


def test_the_card_offers_confirm_edit_and_discard() -> None:
    rows = card_buttons(42)
    labels = [b.text for b in rows[0]]
    assert labels == ["✅ Confirm", "✏️ Edit", "❌ Discard"]
    assert [b.callback_data for b in rows[0]] == [
        "cmd:42:confirm",
        "cmd:42:edit",
        "cmd:42:discard",
    ]


# --- T-19: the clarification card -------------------------------------------

def test_ambiguity_renders_a_numbered_list_with_one_button_each(
    session: Session, user: User
) -> None:
    make_meeting(session, user, "Board call", [_local("2026-09-16 11:00")])
    make_meeting(session, user, "Client call", [_local("2026-09-17 15:00")])
    intent = ExtractedIntent(
        intent=Intent.CONFIRM_SLOT, meeting_reference="the call", confidence=0.4
    )
    plan = build_plan(session, user, intent, now_utc=NOW)
    assert plan.action == "clarify"

    text = render_clarification(plan)
    assert "1." in text and "2." in text
    assert "Board call" in text and "Client call" in text

    rows = clarification_buttons(9, plan)
    assert len(rows) == len(plan.candidates) + 1  # + discard
    assert rows[0][0].callback_data.startswith("pick:9:")


# --- T-22: /pending ---------------------------------------------------------

def test_pending_lists_open_proposals_with_ages(session: Session, user: User) -> None:
    make_meeting(
        session,
        user,
        "Evocabank sync",
        [_local("2026-09-16 10:00"), _local("2026-09-17 16:00")],
    )
    make_meeting(
        session,
        user,
        "Signed and sealed",
        [_local("2026-09-18 09:00")],
        status=MeetingStatus.confirmed,
        state=SlotState.fixed,
    )
    text = render_pending(open_meetings(session, user), "Asia/Yerevan", now=NOW)
    assert "Evocabank sync" in text
    assert "Signed and sealed" not in text, "confirmed meetings are not pending"
    assert "2 slots" in text
    assert "Asia/Yerevan" in text


def test_pending_is_friendly_when_nothing_is_open(session: Session, user: User) -> None:
    assert "Nothing is waiting" in render_pending([], "Asia/Yerevan", now=NOW)


def test_pending_flags_past_placeholders(session: Session, user: User) -> None:
    make_meeting(session, user, "Stale", [_local("2026-09-01 10:00")])
    text = render_pending(open_meetings(session, user), "Asia/Yerevan", now=NOW)
    assert "past" in text


# --- T-09 / T-28: allow-list and rate limits --------------------------------

def test_the_allowlist_permits_only_configured_ids() -> None:
    assert is_allowed(4242) is True
    assert is_allowed(9999) is False


def test_an_empty_allowlist_fails_closed(monkeypatch) -> None:
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(type(settings), "allowed_telegram_ids", property(lambda self: frozenset()))
    assert is_allowed(4242) is False, "an empty allow-list must mean nobody, not everybody"


def test_rate_limit_trips_after_the_hourly_cap(session: Session, user: User) -> None:
    from app.config import get_settings

    cap = get_settings().commands_per_hour
    for index in range(cap):
        session.add(
            VoiceCommand(user_id=user.id, telegram_message_id=index, transcript="x")
        )
    session.flush()
    with pytest.raises(RateLimited):
        check_rate_limit(session, user)


def test_old_commands_do_not_count_towards_the_limit(session: Session, user: User) -> None:
    from app.config import get_settings

    cap = get_settings().commands_per_hour
    stale = datetime.now(UTC) - timedelta(hours=3)
    for index in range(cap + 5):
        session.add(
            VoiceCommand(
                user_id=user.id, telegram_message_id=index, transcript="x", created_at=stale
            )
        )
    session.flush()
    check_rate_limit(session, user)  # must not raise


def test_get_or_create_user_is_idempotent(session: Session) -> None:
    first = get_or_create_user(session, 777)
    second = get_or_create_user(session, 777)
    assert first.id == second.id
    assert first.home_timezone == "Asia/Yerevan"
    assert first.default_duration_minutes == 60


# --- Security: token encryption ---------------------------------------------

def test_refresh_tokens_round_trip_through_fernet() -> None:
    secret = "1//0gAbCdEfGh-a-google-refresh-token"
    ciphertext = encrypt(secret)
    assert secret not in ciphertext
    assert decrypt(ciphertext) == secret


def test_a_token_from_another_key_cannot_be_read(monkeypatch) -> None:
    from cryptography.fernet import Fernet

    foreign = Fernet(generate_key().encode()).encrypt(b"secret").decode()
    with pytest.raises(TokenDecryptionError):
        decrypt(foreign)


# --- T-17: card expiry -------------------------------------------------------

def test_the_cron_expires_cards_left_unanswered(session: Session, user: User) -> None:
    from app.jobs.reconcile import expire_stale_cards

    fresh = VoiceCommand(
        user_id=user.id,
        telegram_message_id=1,
        transcript="x",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    stale = VoiceCommand(
        user_id=user.id,
        telegram_message_id=2,
        transcript="x",
        expires_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    session.add_all([fresh, stale])
    session.flush()

    assert expire_stale_cards(session) == 1
    assert fresh.status is CommandStatus.pending_confirmation
    assert stale.status is CommandStatus.rejected
    assert stale.error_text == "expired without an answer"
