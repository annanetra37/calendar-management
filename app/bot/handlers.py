"""Telegram update handling: the whole voice -> card -> calendar loop.

Nothing here writes to the calendar directly. A voice note produces a
confirmation card (T-15); only the ✅ button executes the plan.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import session_scope
from app.integrations.google_calendar import CalendarError
from app.integrations.google_oauth import GoogleReauthRequired
from app.integrations.telegram import TelegramClient, TelegramError
from app.logging_setup import correlation_scope, get_correlation_id
from app.models import (
    CommandStatus,
    Meeting,
    OAuthState,
    User,
    VoiceCommand,
    utcnow,
)
from app.nlu.extract import ExtractionError, extract_intent
from app.nlu.schema import ExtractedIntent
from app.nlu.transcribe import TranscriptionError, transcribe
from app.services import audio as audio_service
from app.services import cards
from app.services.google_account import calendar_for
from app.services.meetings import (
    MeetingOperationError,
    apply_plan,
    retry_failed_deletions,
)
from app.services.planning import (
    Plan,
    active_meetings,
    build_plan,
    open_meetings,
    summarise_for_prompt,
)
from app.services.users import (
    RateLimited,
    check_rate_limit,
    get_or_create_user,
    is_allowed,
)

log = logging.getLogger(__name__)

HELP_TEXT = """\
🎙 <b>Voice scheduling</b>

Send me a voice note. I never touch your calendar until you tap ✅.

<b>What I understand</b>
• “Board call, Tuesday the 15th, 3pm, fixed” → one green event
• “Suggest Evocabank meeting Tuesday 2pm, Wednesday 10, Thursday 4” → yellow placeholders
• “Evocabank is confirmed for Wednesday 10” → keeps one green, deletes the rest
• “Cancel the Evocabank meeting” / “Move the board call to Thursday 4pm”
• “What's still open?”

<b>Commands</b>
/pending — open proposals
/connect — link Google Calendar
/tz Europe/Vienna — interpret times in another zone while travelling
/tz — show the current zone
/cleanup — retry any placeholder deletions that failed
/help — this message
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def process_update(update: dict, telegram: TelegramClient | None = None) -> None:
    """Handle one Telegram update. Never raises — every failure is reported."""
    owns = telegram is None
    telegram = telegram or TelegramClient()
    correlation = update.get("_correlation_id")
    try:
        with correlation_scope(correlation):
            if "message" in update:
                _guarded(telegram, update["message"], handle_message)
            elif "callback_query" in update:
                _handle_callback_guarded(telegram, update["callback_query"])
            else:
                log.info("update_ignored", extra={"keys": ",".join(update.keys())})
    finally:
        if owns:
            telegram.close()


def _guarded(telegram: TelegramClient, message: dict, handler: Callable) -> None:
    chat_id = (message.get("chat") or {}).get("id")
    try:
        handler(telegram, message)
    except Exception as exc:
        log.exception("message_handler_failed")
        if chat_id:
            _safe_send(telegram, chat_id, f"⚠️ Something went wrong: {_friendly(exc)}")


def _handle_callback_guarded(telegram: TelegramClient, callback: dict) -> None:
    query_id = callback.get("id")
    chat_id = ((callback.get("message") or {}).get("chat") or {}).get("id")
    try:
        handle_callback(telegram, callback)
    except Exception as exc:
        log.exception("callback_handler_failed")
        if query_id:
            try:
                telegram.answer_callback_query(query_id, "Something went wrong.")
            except TelegramError:
                pass
        if chat_id:
            _safe_send(telegram, chat_id, f"⚠️ {_friendly(exc)}")


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

def handle_message(telegram: TelegramClient, message: dict) -> None:
    chat_id = message["chat"]["id"]
    from_user = message.get("from") or {}
    telegram_user_id = from_user.get("id")

    if telegram_user_id is None:
        return
    if not is_allowed(telegram_user_id):
        log.warning("unauthorised_user", extra={"telegram_user_id": telegram_user_id})
        _safe_send(
            telegram,
            chat_id,
            "Sorry — this bot is private and your account is not on its allow-list.\n"
            "If you should have access, quote this id to the owner: "
            f"<code>{telegram_user_id}</code>",
        )
        return

    voice = message.get("voice") or message.get("audio")
    text = (message.get("text") or "").strip()

    if voice:
        handle_voice(telegram, message, telegram_user_id, voice)
    elif text.startswith("/"):
        handle_command(telegram, chat_id, telegram_user_id, text)
    elif text:
        handle_text(telegram, message, telegram_user_id, text)
    else:
        _safe_send(telegram, chat_id, "Send me a voice note, or /help.")


def handle_voice(
    telegram: TelegramClient, message: dict, telegram_user_id: int, voice: dict
) -> None:
    chat_id = message["chat"]["id"]
    message_id = message["message_id"]
    settings = get_settings()

    telegram.send_chat_action(chat_id, "typing")
    interim = _InterimNotice(telegram, chat_id, settings.slow_pipeline_notice_seconds)
    interim.start()
    started = time.monotonic()

    try:
        with session_scope() as session:
            user = get_or_create_user(session, telegram_user_id)
            try:
                check_rate_limit(session, user)
            except RateLimited as exc:
                _safe_send(telegram, chat_id, f"🚦 {exc}")
                return
            if not user.is_google_connected:
                _safe_send(telegram, chat_id, _connect_prompt(session, telegram_user_id))
                return
            user_id = user.id

        # Audio + model calls happen outside the DB transaction: they are slow
        # and must not hold a connection open.
        try:
            note = audio_service.download_voice(
                telegram, voice, user_id=user_id, message_id=message_id
            )
        except audio_service.AudioRejected as exc:
            _safe_send(telegram, chat_id, f"🎙 {exc}")
            return

        storage_ref = audio_service.persist(note)

        try:
            transcription = transcribe(note.data, filename=note.filename)
        except TranscriptionError as exc:
            with session_scope() as session:
                session.add(
                    VoiceCommand(
                        user_id=user_id,
                        telegram_message_id=message_id,
                        correlation_id=get_correlation_id(),
                        audio_storage_ref=storage_ref,
                        status=CommandStatus.failed,
                        error_text=str(exc),
                    )
                )
            _safe_send(telegram, chat_id, f"🎧 {exc}")
            return

        log.info(
            "transcribed",
            extra={
                "user_id": user_id,
                "language": transcription.language,
                "chars": len(transcription.text),
                "seconds": round(time.monotonic() - started, 2),
            },
        )
        if not get_settings().is_production or not get_settings().log_transcripts:
            log.debug("transcript", extra={"text": transcription.text})

        _plan_and_reply(
            telegram,
            chat_id=chat_id,
            user_id=user_id,
            telegram_message_id=message_id,
            transcript=transcription.text,
            language=transcription.language,
            storage_ref=storage_ref,
            started=started,
        )
    finally:
        interim.cancel()


def handle_text(
    telegram: TelegramClient, message: dict, telegram_user_id: int, text: str
) -> None:
    """A text reply: either a correction to a pending card, or a typed command."""
    chat_id = message["chat"]["id"]
    message_id = message["message_id"]
    started = time.monotonic()

    with session_scope() as session:
        user = get_or_create_user(session, telegram_user_id)
        if not user.is_google_connected:
            _safe_send(telegram, chat_id, _connect_prompt(session, telegram_user_id))
            return
        pending = _awaiting_edit(session, user)
        user_id = user.id
        if pending is not None:
            correction_of = pending.id
            original_transcript = pending.transcript or ""
            previous_intent = pending.parsed_intent_json
            pending.status = CommandStatus.rejected
            pending.awaiting_edit = False
        else:
            correction_of = None
            original_transcript = ""
            previous_intent = None

    if correction_of is not None:
        # T-16: re-run extraction with the original transcript plus the correction.
        _plan_and_reply(
            telegram,
            chat_id=chat_id,
            user_id=user_id,
            telegram_message_id=message_id,
            transcript=original_transcript,
            language=None,
            storage_ref=None,
            started=started,
            correction=text,
            previous_intent=previous_intent,
        )
        return

    _plan_and_reply(
        telegram,
        chat_id=chat_id,
        user_id=user_id,
        telegram_message_id=message_id,
        transcript=text,
        language=None,
        storage_ref=None,
        started=started,
    )


# ---------------------------------------------------------------------------
# Shared: extraction -> plan -> card
# ---------------------------------------------------------------------------

def _plan_and_reply(
    telegram: TelegramClient,
    *,
    chat_id: int,
    user_id: int,
    telegram_message_id: int,
    transcript: str,
    language: str | None,
    storage_ref: str | None,
    started: float,
    correction: str | None = None,
    previous_intent: dict | None = None,
) -> None:
    import json

    with session_scope() as session:
        user = session.get(User, user_id)
        assert user is not None
        tz_name = user.home_timezone
        default_duration = user.default_duration_minutes
        meetings = active_meetings(session, user)
        summaries = summarise_for_prompt(meetings, tz_name)

    now_utc = datetime.now(UTC)
    now_local = now_utc.astimezone(ZoneInfo(tz_name))

    try:
        intent = extract_intent(
            transcript,
            now_local=now_local,
            timezone_name=tz_name,
            open_meetings=summaries,
            default_duration_minutes=default_duration,
            correction=correction,
            previous_intent_json=json.dumps(previous_intent) if previous_intent else None,
        )
    except ExtractionError as exc:
        log.error("extraction_failed", extra={"error": str(exc)})
        with session_scope() as session:
            session.add(
                _new_command(
                    user_id, telegram_message_id, transcript, language, storage_ref,
                    status=CommandStatus.failed, error_text=str(exc),
                )
            )
        _safe_send(
            telegram,
            chat_id,
            "🧠 I could not reach the language model just now. "
            "Your voice note was saved — please send it again in a moment.",
        )
        return

    with session_scope() as session:
        user = session.get(User, user_id)
        assert user is not None
        plan = build_plan(session, user, intent, now_utc=now_utc)

        command = _new_command(
            user_id, telegram_message_id, transcript, language, storage_ref
        )
        command.parsed_intent_json = _intent_payload(intent, correction)
        command.plan_json = plan.model_dump(mode="json")
        command.expires_at = utcnow() + timedelta(
            minutes=get_settings().pending_card_ttl_minutes
        )
        if plan.meeting_id:
            command.resolved_meeting_id = plan.meeting_id

        if plan.action == "reject":
            command.status = CommandStatus.rejected
            command.error_text = plan.error
            session.add(command)
            _safe_send(telegram, chat_id, _rejection_text(plan, transcript))
            return

        if plan.action == "list_pending":
            command.status = CommandStatus.applied
            session.add(command)
            text = cards.render_pending(open_meetings(session, user), tz_name)
            _safe_send(telegram, chat_id, text)
            return

        session.add(command)
        session.flush()
        command_id = command.id

        if plan.action == "clarify":
            body = cards.render_clarification(plan)
            buttons = cards.clarification_buttons(command_id, plan)
        else:
            body = cards.render_card(plan)
            buttons = cards.card_buttons(command_id)

        elapsed = time.monotonic() - started
        footer = f"\n\n<i>Times in {tz_name} · {elapsed:.1f}s</i>"
        sent = _safe_send(telegram, chat_id, body + footer, buttons=buttons)
        if sent:
            command.telegram_card_message_id = sent.get("message_id")
        log.info(
            "card_sent",
            extra={"command_id": command_id, "action": plan.action, "seconds": round(elapsed, 2)},
        )


def _intent_payload(intent: ExtractedIntent, correction: str | None) -> dict:
    payload = intent.model_dump(mode="json")
    if correction:
        payload["_correction"] = correction
    return payload


def _new_command(
    user_id: int,
    telegram_message_id: int,
    transcript: str,
    language: str | None,
    storage_ref: str | None,
    *,
    status: CommandStatus = CommandStatus.pending_confirmation,
    error_text: str | None = None,
) -> VoiceCommand:
    return VoiceCommand(
        user_id=user_id,
        telegram_message_id=telegram_message_id,
        correlation_id=get_correlation_id(),
        audio_storage_ref=storage_ref,
        transcript=transcript,
        language_detected=language,
        status=status,
        error_text=error_text,
    )


def _rejection_text(plan: Plan, transcript: str) -> str:
    import html

    lines = [f"❓ {html.escape(plan.error or 'I did not understand that.')}"]
    if transcript:
        lines += ["", f"<i>I heard:</i> “{html.escape(transcript[:400])}”"]
    for warning in dict.fromkeys(plan.warnings):
        lines.append(f"⚠️ {html.escape(warning)}")
    lines += ["", "Try again, or /help for examples."]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Callbacks (the ✅ / ✏️ / ❌ buttons)
# ---------------------------------------------------------------------------

def handle_callback(telegram: TelegramClient, callback: dict) -> None:
    data = callback.get("data") or ""
    query_id = callback["id"]
    message = callback.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    card_message_id = message.get("message_id")
    telegram_user_id = (callback.get("from") or {}).get("id")

    if not is_allowed(telegram_user_id):
        telegram.answer_callback_query(query_id, "Not authorised.")
        return

    parts = data.split(":")
    if len(parts) != 3:
        telegram.answer_callback_query(query_id, "Unrecognised button.")
        return
    kind, raw_id, argument = parts
    try:
        command_id = int(raw_id)
    except ValueError:
        telegram.answer_callback_query(query_id, "Unrecognised button.")
        return

    with correlation_scope():
        if kind == "cmd" and argument == "discard":
            _discard(telegram, query_id, chat_id, card_message_id, command_id)
        elif kind == "cmd" and argument == "edit":
            _start_edit(telegram, query_id, chat_id, card_message_id, command_id)
        elif kind == "cmd" and argument == "confirm":
            _execute(telegram, query_id, chat_id, card_message_id, command_id)
        elif kind == "pick":
            _pick_meeting(telegram, query_id, chat_id, card_message_id, command_id, int(argument))
        else:
            telegram.answer_callback_query(query_id, "Unrecognised button.")


def _load_pending(session: Session, command_id: int) -> VoiceCommand:
    command = session.get(VoiceCommand, command_id)
    if command is None:
        raise MeetingOperationError("That card is no longer available.")
    if command.status is not CommandStatus.pending_confirmation:
        raise MeetingOperationError(
            f"That card was already {command.status.value.replace('_', ' ')}."
        )
    if command.expires_at and _aware(command.expires_at) < utcnow():
        command.status = CommandStatus.rejected
        command.error_text = "expired"
        raise MeetingOperationError(
            "That card expired — please send the voice note again."
        )
    return command


def _discard(
    telegram: TelegramClient, query_id: str, chat_id: int, card_message_id: int, command_id: int
) -> None:
    with session_scope() as session:
        command = session.get(VoiceCommand, command_id)
        if command and command.status is CommandStatus.pending_confirmation:
            command.status = CommandStatus.rejected
            command.error_text = "discarded by owner"
            command.awaiting_edit = False
    telegram.answer_callback_query(query_id, "Discarded.")
    _edit_card(telegram, chat_id, card_message_id, "❌ Discarded — nothing was written.")


def _start_edit(
    telegram: TelegramClient, query_id: str, chat_id: int, card_message_id: int, command_id: int
) -> None:
    with session_scope() as session:
        try:
            command = _load_pending(session, command_id)
        except MeetingOperationError as exc:
            telegram.answer_callback_query(query_id, str(exc)[:180])
            _edit_card(telegram, chat_id, card_message_id, f"⌛ {exc}")
            return
        # Only one card may be awaiting a correction at a time.
        for other in session.scalars(
            select(VoiceCommand).where(
                VoiceCommand.user_id == command.user_id,
                VoiceCommand.awaiting_edit.is_(True),
            )
        ):
            other.awaiting_edit = False
        command.awaiting_edit = True
        heard = command.transcript or ""

    telegram.answer_callback_query(query_id, "Send your correction as a text message.")
    import html

    _edit_card(
        telegram,
        chat_id,
        card_message_id,
        "✏️ <b>Editing</b> — reply with a text correction, "
        "e.g. “no, 4pm not 4am”.\n\n"
        f"<i>I heard:</i> “{html.escape(heard[:400])}”",
    )


def _pick_meeting(
    telegram: TelegramClient,
    query_id: str,
    chat_id: int,
    card_message_id: int,
    command_id: int,
    meeting_id: int,
) -> None:
    """Owner answered the “which meeting?” question (T-19)."""
    with session_scope() as session:
        try:
            command = _load_pending(session, command_id)
        except MeetingOperationError as exc:
            telegram.answer_callback_query(query_id, str(exc)[:180])
            return
        user = session.get(User, command.user_id)
        assert user is not None
        meeting = session.get(Meeting, meeting_id)
        if meeting is None or meeting.user_id != user.id:
            telegram.answer_callback_query(query_id, "That meeting is gone.")
            return

        intent = ExtractedIntent.model_validate(command.parsed_intent_json or {})
        intent.meeting_id = meeting.id
        intent.meeting_reference = meeting.title
        plan = build_plan(session, user, intent)

        command.parsed_intent_json = intent.model_dump(mode="json")
        command.plan_json = plan.model_dump(mode="json")
        command.resolved_meeting_id = meeting.id

        if plan.action in ("clarify", "reject"):
            command.status = CommandStatus.rejected
            command.error_text = plan.error or plan.question
            telegram.answer_callback_query(query_id, "Still not clear.")
            _edit_card(
                telegram, chat_id, card_message_id, _rejection_text(plan, command.transcript or "")
            )
            return

        telegram.answer_callback_query(query_id, f"“{meeting.title}” it is.")
        _edit_card(
            telegram,
            chat_id,
            card_message_id,
            cards.render_card(plan),
            buttons=cards.card_buttons(command_id),
        )


def _execute(
    telegram: TelegramClient, query_id: str, chat_id: int, card_message_id: int, command_id: int
) -> None:
    """The ✅ path — the only place the calendar is written."""
    needs_retry_meeting: int | None = None
    user_id: int | None = None

    with session_scope() as session:
        try:
            command = _load_pending(session, command_id)
        except MeetingOperationError as exc:
            telegram.answer_callback_query(query_id, str(exc)[:180])
            _edit_card(telegram, chat_id, card_message_id, f"⌛ {exc}")
            return

        user = session.get(User, command.user_id)
        assert user is not None
        user_id = user.id
        plan = Plan.model_validate(command.plan_json or {})

        telegram.answer_callback_query(query_id, "Working on it…")

        try:
            with calendar_for(user) as calendar:
                result = apply_plan(session, user, plan, calendar, voice_command=command)
        except GoogleReauthRequired:
            command.status = CommandStatus.failed
            command.error_text = "google reauth required"
            _edit_card(
                telegram,
                chat_id,
                card_message_id,
                "🔑 Google access has expired. Nothing was written.\n"
                f"Re-authorise here: {_auth_link(session, user.telegram_user_id)}",
            )
            return
        except MeetingOperationError as exc:
            command.status = CommandStatus.failed
            command.error_text = str(exc)
            _edit_card(telegram, chat_id, card_message_id, f"⚠️ {exc}")
            return
        except CalendarError as exc:
            command.status = CommandStatus.failed
            command.error_text = str(exc)
            _edit_card(
                telegram,
                chat_id,
                card_message_id,
                f"📅 Google Calendar is not cooperating: {exc}\n"
                "Nothing partial was left behind — try again shortly.",
            )
            return

        command.status = CommandStatus.applied if result.ok else CommandStatus.failed
        command.error_text = None if result.ok else result.message
        if result.meeting_id:
            command.resolved_meeting_id = result.meeting_id
        if result.needs_cleanup_retry:
            needs_retry_meeting = result.meeting_id

        body = ("✅ " if result.ok else "⚠️ ") + result.message
        for warning in dict.fromkeys(result.warnings):
            body += f"\n⚠️ {warning}"
        _edit_card(telegram, chat_id, card_message_id, body)
        log.info(
            "plan_applied",
            extra={
                "command_id": command_id,
                "action": plan.action,
                "meeting_id": result.meeting_id,
                "partial": result.partial,
            },
        )

    if needs_retry_meeting and user_id is not None:
        _schedule_cleanup_retry(user_id, needs_retry_meeting, chat_id)


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

def handle_command(
    telegram: TelegramClient, chat_id: int, telegram_user_id: int, text: str
) -> None:
    command, _, argument = text.partition(" ")
    command = command.split("@")[0].lower()
    argument = argument.strip()

    if command in ("/start", "/help"):
        with session_scope() as session:
            user = get_or_create_user(session, telegram_user_id)
            connected = user.is_google_connected
            link = None if connected else _auth_link(session, telegram_user_id)
        suffix = (
            f"\n\n✅ Google Calendar connected as <code>{chat_id}</code>."
            if connected
            else f"\n\n🔗 First, connect Google Calendar: {link}"
        )
        _safe_send(telegram, chat_id, HELP_TEXT + suffix)

    elif command == "/connect":
        with session_scope() as session:
            get_or_create_user(session, telegram_user_id)
            link = _auth_link(session, telegram_user_id)
        _safe_send(
            telegram,
            chat_id,
            f"🔗 Connect Google Calendar (events access only):\n{link}",
        )

    elif command == "/pending":
        with session_scope() as session:
            user = get_or_create_user(session, telegram_user_id)
            body = cards.render_pending(open_meetings(session, user), user.home_timezone)
        _safe_send(telegram, chat_id, body)

    elif command == "/tz":
        _handle_tz(telegram, chat_id, telegram_user_id, argument)

    elif command == "/cleanup":
        _handle_cleanup(telegram, chat_id, telegram_user_id)

    elif command == "/whoami":
        _safe_send(telegram, chat_id, f"Your Telegram id: <code>{telegram_user_id}</code>")

    elif command == "/cancel":
        with session_scope() as session:
            user = get_or_create_user(session, telegram_user_id)
            count = 0
            for pending in session.scalars(
                select(VoiceCommand).where(
                    VoiceCommand.user_id == user.id,
                    VoiceCommand.status == CommandStatus.pending_confirmation,
                )
            ):
                pending.status = CommandStatus.rejected
                pending.error_text = "cancelled by owner"
                pending.awaiting_edit = False
                count += 1
        _safe_send(telegram, chat_id, f"Cleared {count} pending card(s).")

    else:
        _safe_send(telegram, chat_id, "Unknown command. /help for the list.")


def _handle_tz(
    telegram: TelegramClient, chat_id: int, telegram_user_id: int, argument: str
) -> None:
    with session_scope() as session:
        user = get_or_create_user(session, telegram_user_id)
        if not argument:
            now = datetime.now(ZoneInfo(user.home_timezone))
            _safe_send(
                telegram,
                chat_id,
                f"🌍 Interpreting spoken times in <b>{user.home_timezone}</b> "
                f"(now {now:%H:%M}).\nChange it with <code>/tz Europe/Vienna</code>.",
            )
            return
        try:
            zone = ZoneInfo(argument)
        except (ZoneInfoNotFoundError, ValueError):
            _safe_send(
                telegram,
                chat_id,
                f"❓ <code>{argument}</code> is not an IANA timezone. "
                "Try <code>/tz Europe/Vienna</code> or <code>/tz Asia/Yerevan</code>.",
            )
            return
        previous = user.home_timezone
        user.home_timezone = argument
        now = datetime.now(zone)
    _safe_send(
        telegram,
        chat_id,
        f"🌍 Spoken times are now read as <b>{argument}</b> (was {previous}). "
        f"Local time there is {now:%H:%M}.\n"
        "<i>Existing events are unchanged — they were already stored in UTC.</i>",
    )


def _handle_cleanup(telegram: TelegramClient, chat_id: int, telegram_user_id: int) -> None:
    with session_scope() as session:
        user = get_or_create_user(session, telegram_user_id)
        if not user.is_google_connected:
            _safe_send(telegram, chat_id, _connect_prompt(session, telegram_user_id))
            return
        with calendar_for(user) as calendar:
            cleared, failing = retry_failed_deletions(session, user, calendar)
    if cleared == 0 and failing == 0:
        _safe_send(telegram, chat_id, "✅ Nothing to clean up.")
    else:
        message = f"🧹 Removed {cleared} leftover placeholder(s)."
        if failing:
            message += f"\n⚠️ {failing} still failing — I will retry again tonight."
        _safe_send(telegram, chat_id, message)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _InterimNotice:
    """Send a 'still working' message if the pipeline exceeds the latency budget."""

    def __init__(self, telegram: TelegramClient, chat_id: int, after_seconds: float) -> None:
        self._timer = threading.Timer(after_seconds, self._fire)
        self._timer.daemon = True
        self._telegram = telegram
        self._chat_id = chat_id

    def start(self) -> None:
        self._timer.start()

    def cancel(self) -> None:
        self._timer.cancel()

    def _fire(self) -> None:
        try:
            self._telegram.send_message(
                self._chat_id, "⏳ Still processing that one…", disable_notification=True
            )
        except Exception:
            # A courtesy message: never let it become the failure itself.
            log.debug("interim_notice_failed", exc_info=True)


def _auth_link(session: Session, telegram_user_id: int) -> str:
    from app.integrations.google_oauth import authorization_url, new_state

    state = new_state()
    session.add(OAuthState(state=state, telegram_user_id=telegram_user_id))
    return authorization_url(state)


def _connect_prompt(session: Session, telegram_user_id: int) -> str:
    return (
        "🔗 Your Google Calendar is not connected yet.\n"
        f"Authorise here (events access only): {_auth_link(session, telegram_user_id)}"
    )


def _safe_send(telegram: TelegramClient, chat_id: int, text: str, **kwargs) -> dict:
    try:
        return telegram.send_message(chat_id, text, **kwargs)
    except TelegramError as exc:
        log.error("telegram_send_failed", extra={"error": str(exc)})
        return {}


def _edit_card(
    telegram: TelegramClient, chat_id: int | None, message_id: int | None, text: str, **kwargs
) -> None:
    if chat_id is None:
        return
    try:
        if message_id is not None:
            telegram.edit_message(chat_id, message_id, text, **kwargs)
        else:
            telegram.send_message(chat_id, text, **kwargs)
    except TelegramError as exc:
        log.warning("card_edit_failed", extra={"error": str(exc)})
        _safe_send(telegram, chat_id, text)


def _awaiting_edit(session: Session, user: User) -> VoiceCommand | None:
    return session.scalar(
        select(VoiceCommand)
        .where(
            VoiceCommand.user_id == user.id,
            VoiceCommand.awaiting_edit.is_(True),
            VoiceCommand.status == CommandStatus.pending_confirmation,
        )
        .order_by(VoiceCommand.created_at.desc())
        .limit(1)
    )


def _schedule_cleanup_retry(user_id: int, meeting_id: int, chat_id: int) -> None:
    """Retry a failed placeholder deletion shortly, off the request path."""

    def worker() -> None:
        time.sleep(20)
        with correlation_scope():
            try:
                with session_scope() as session:
                    user = session.get(User, user_id)
                    if user is None:
                        return
                    with calendar_for(user) as calendar:
                        cleared, failing = retry_failed_deletions(
                            session, user, calendar, meeting_id=meeting_id
                        )
                if cleared and not failing:
                    with TelegramClient() as telegram:
                        _safe_send(
                            telegram,
                            chat_id,
                            f"🧹 The {cleared} leftover placeholder(s) are gone now — "
                            "your calendar is consistent.",
                        )
            except Exception:
                log.exception("cleanup_retry_worker_failed")

    thread = threading.Thread(target=worker, name=f"cleanup-{meeting_id}", daemon=True)
    thread.start()


def _friendly(exc: Exception) -> str:
    if isinstance(exc, GoogleReauthRequired):
        return "Google access expired — send /connect to re-authorise."
    if isinstance(exc, CalendarError):
        return f"Google Calendar said: {exc}"
    if isinstance(
        exc, TranscriptionError | ExtractionError | MeetingOperationError
    ):
        return str(exc)
    return "an unexpected error. It has been logged."


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


__all__ = ["handle_callback", "handle_message", "process_update"]
