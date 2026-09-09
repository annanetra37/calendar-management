"""T-26-style end-to-end: webhook in → card → button tap → calendar → collapse.

The only doubles are at the network edges (Telegram, Whisper, the extraction
model and the Calendar API). Everything in between — the database, planning,
matching, the collapse — is the real code path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.bot import handlers
from app.models import Base, CommandStatus, Meeting, MeetingStatus, SlotState, VoiceCommand
from app.nlu.schema import ExtractedIntent, Intent, SpokenSlot
from app.nlu.transcribe import Transcription
from tests.fakes import FakeCalendar

CHAT_ID = 4242
NOW = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)


class FakeTelegram:
    """Records every outbound Telegram call."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []
        self.answers: list[str] = []
        self._next_id = 100

    def send_message(self, chat_id, text, *, buttons=None, **kwargs) -> dict:
        self._next_id += 1
        self.messages.append(
            {
                "chat_id": chat_id,
                "text": text,
                "buttons": buttons,
                "message_id": self._next_id,
            }
        )
        return {"message_id": self._next_id}

    def edit_message(self, chat_id, message_id, text, *, buttons=None) -> dict:
        self.edits.append({"message_id": message_id, "text": text, "buttons": buttons})
        return {}

    def answer_callback_query(self, query_id, text="", show_alert=False) -> dict:
        self.answers.append(text)
        return {}

    def send_chat_action(self, chat_id, action="typing") -> None:
        pass

    def get_file(self, file_id):
        return {"file_path": "voice/file_1.oga"}

    def download_file(self, file_path, *, max_bytes):
        return b"OggS-fake-audio"

    def close(self) -> None:
        pass

    @property
    def last_card(self) -> dict:
        return self.messages[-1]

    @property
    def last_text(self) -> str:
        return (self.edits or self.messages)[-1]["text"]


@pytest.fixture()
def wired(monkeypatch, tmp_path):
    """Wire the handler module to an in-memory DB and fake edges."""
    engine = create_engine("sqlite+pysqlite:///file:e2e?mode=memory&cache=shared&uri=true")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    from contextlib import contextmanager

    @contextmanager
    def scope():
        db = maker()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    telegram = FakeTelegram()
    calendar = FakeCalendar()

    monkeypatch.setattr(handlers, "session_scope", scope)
    monkeypatch.setattr(handlers, "calendar_for", _fake_calendar_for(calendar))
    monkeypatch.setattr(
        handlers.audio_service, "persist", lambda note: str(tmp_path / note.filename)
    )
    monkeypatch.setattr(
        handlers, "transcribe", lambda data, filename="voice.ogg": Transcription("stub", "en")
    )
    monkeypatch.setattr(handlers, "_schedule_cleanup_retry", lambda *a, **k: None)

    # A connected Google account.
    with scope() as db:
        from app.services.users import get_or_create_user

        user = get_or_create_user(db, CHAT_ID)
        user.google_refresh_token_encrypted = "encrypted"
        user.google_email = "owner@example.com"

    yield telegram, calendar, scope
    Base.metadata.drop_all(engine)
    engine.dispose()


def _fake_calendar_for(calendar: FakeCalendar):
    from contextlib import contextmanager

    @contextmanager
    def factory(user, **kwargs):
        yield calendar

    return factory


def _voice_update(message_id: int) -> dict:
    return {
        "update_id": message_id,
        "message": {
            "message_id": message_id,
            "chat": {"id": CHAT_ID},
            "from": {"id": CHAT_ID},
            "voice": {"file_id": f"f{message_id}", "duration": 6, "mime_type": "audio/ogg"},
        },
    }


def _callback(command_id: int, action: str, card_message_id: int) -> dict:
    return {
        "callback_query": {
            "id": f"q{command_id}",
            "from": {"id": CHAT_ID},
            "data": f"cmd:{command_id}:{action}",
            "message": {"message_id": card_message_id, "chat": {"id": CHAT_ID}},
        }
    }


def _stub_extraction(monkeypatch, intent: ExtractedIntent) -> None:
    monkeypatch.setattr(handlers, "extract_intent", lambda *a, **k: intent)


def _freeze_now(monkeypatch) -> None:
    import app.services.planning as planning

    real = planning.datetime

    class FrozenDatetime(real):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr(planning, "datetime", FrozenDatetime)
    monkeypatch.setattr(handlers, "datetime", FrozenDatetime)


SUGGEST = ExtractedIntent(
    intent=Intent.CREATE_SUGGESTED,
    title="Evocabank sync",
    slots=[
        SpokenSlot(raw="Tuesday 2pm", date="2026-09-15", time="14:00"),
        SpokenSlot(raw="Wednesday 10", date="2026-09-16", time="10:00"),
        SpokenSlot(raw="Thursday 4", date="2026-09-17", time="16:00"),
    ],
    confidence=0.92,
)

CONFIRM = ExtractedIntent(
    intent=Intent.CONFIRM_SLOT,
    meeting_reference="Evocabank",
    slots=[SpokenSlot(raw="Wednesday 10", date="2026-09-16", time="10:00")],
    confidence=0.9,
)


def test_the_full_loop_voice_to_placeholders_to_confirmation(wired, monkeypatch) -> None:
    telegram, calendar, scope = wired
    _freeze_now(monkeypatch)

    # --- 1. voice note proposing three slots -> a card, and NO calendar write
    _stub_extraction(monkeypatch, SUGGEST)
    handlers.process_update(_voice_update(1), telegram)

    card = telegram.last_card
    assert "SUGGESTED — 3 slots" in card["text"]
    assert "Tue 15 Sep, 14:00–15:00 (Yerevan)" in card["text"]
    assert card["buttons"], "the card must carry the confirm/edit/discard buttons"
    assert not calendar.events, "nothing may reach the calendar before ✅"

    with scope() as db:
        command = db.query(VoiceCommand).one()
        assert command.status is CommandStatus.pending_confirmation
        assert command.expires_at is not None
        command_id = command.id

    # --- 2. tap ✅ -> three yellow placeholders in one meeting
    handlers.process_update(_callback(command_id, "confirm", card["message_id"]), telegram)

    assert len(calendar.yellow()) == 3
    assert not calendar.green()
    assert "Created 3 placeholder slots" in telegram.edits[-1]["text"]

    with scope() as db:
        meeting = db.query(Meeting).one()
        assert meeting.status is MeetingStatus.proposed
        assert len(meeting.slots) == 3
        assert db.get(VoiceCommand, command_id).status is CommandStatus.applied

    # --- 3. a second voice note confirming one of them
    _stub_extraction(monkeypatch, CONFIRM)
    handlers.process_update(_voice_update(2), telegram)

    card2 = telegram.last_card
    assert "CONFIRM" in card2["text"]
    assert "removes 2 other placeholders" in card2["text"]
    assert len(calendar.yellow()) == 3, "still nothing written before ✅"

    with scope() as db:
        command2 = (
            db.query(VoiceCommand).filter(VoiceCommand.telegram_message_id == 2).one()
        )
        command2_id = command2.id

    # --- 4. tap ✅ -> collapse: exactly one green, zero yellow
    handlers.process_update(_callback(command2_id, "confirm", card2["message_id"]), telegram)

    assert len(calendar.green()) == 1
    assert len(calendar.yellow()) == 0
    green = calendar.green()[0]
    assert green["summary"] == "Evocabank sync"
    assert green["transparency"] == "opaque"
    assert "is confirmed for Wed 16 Sep, 10:00" in telegram.edits[-1]["text"]

    with scope() as db:
        meeting = db.query(Meeting).one()
        assert meeting.status is MeetingStatus.confirmed
        assert len([s for s in meeting.slots if s.state is SlotState.fixed]) == 1
        assert len([s for s in meeting.slots if s.state is SlotState.removed]) == 2


def test_discard_writes_nothing(wired, monkeypatch) -> None:
    telegram, calendar, scope = wired
    _freeze_now(monkeypatch)
    _stub_extraction(monkeypatch, SUGGEST)

    handlers.process_update(_voice_update(1), telegram)
    card = telegram.last_card
    with scope() as db:
        command_id = db.query(VoiceCommand).one().id

    handlers.process_update(_callback(command_id, "discard", card["message_id"]), telegram)

    assert not calendar.events
    assert "Discarded" in telegram.edits[-1]["text"]
    with scope() as db:
        assert db.get(VoiceCommand, command_id).status is CommandStatus.rejected


def test_edit_produces_a_new_card_and_never_a_calendar_write(wired, monkeypatch) -> None:
    """T-16 acceptance."""
    telegram, calendar, scope = wired
    _freeze_now(monkeypatch)
    _stub_extraction(
        monkeypatch,
        ExtractedIntent(
            intent=Intent.CREATE_FIXED,
            title="Client call",
            slots=[SpokenSlot(raw="Friday at 4am", date="2026-09-18", time="04:00")],
            confidence=0.8,
        ),
    )
    handlers.process_update(_voice_update(1), telegram)
    card = telegram.last_card
    assert "04:00" in card["text"]

    with scope() as db:
        command_id = db.query(VoiceCommand).one().id

    handlers.process_update(_callback(command_id, "edit", card["message_id"]), telegram)
    assert "Editing" in telegram.edits[-1]["text"]

    corrections: list[str | None] = []

    def capture(transcript, *, correction=None, **kwargs):
        corrections.append(correction)
        return ExtractedIntent(
            intent=Intent.CREATE_FIXED,
            title="Client call",
            slots=[SpokenSlot(raw="Friday at 4pm", date="2026-09-18", time="16:00")],
            confidence=0.9,
        )

    monkeypatch.setattr(handlers, "extract_intent", capture)
    handlers.process_update(
        {
            "message": {
                "message_id": 2,
                "chat": {"id": CHAT_ID},
                "from": {"id": CHAT_ID},
                "text": "no, 4pm not 4am",
            }
        },
        telegram,
    )

    assert corrections == ["no, 4pm not 4am"], "the correction must reach the extractor"
    assert "16:00" in telegram.last_card["text"]
    assert telegram.last_card["buttons"], "a corrected command yields a new card"
    assert not calendar.events, "editing must never write to the calendar"

    with scope() as db:
        assert db.get(VoiceCommand, command_id).status is CommandStatus.rejected


def test_an_expired_card_cannot_be_tapped(wired, monkeypatch) -> None:
    """T-17 acceptance."""
    from datetime import timedelta

    telegram, calendar, scope = wired
    _freeze_now(monkeypatch)
    _stub_extraction(monkeypatch, SUGGEST)

    handlers.process_update(_voice_update(1), telegram)
    card = telegram.last_card
    with scope() as db:
        command = db.query(VoiceCommand).one()
        command.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        command_id = command.id

    handlers.process_update(_callback(command_id, "confirm", card["message_id"]), telegram)

    assert not calendar.events
    assert "expired" in telegram.edits[-1]["text"].lower()
    with scope() as db:
        assert db.get(VoiceCommand, command_id).status is CommandStatus.rejected


def test_tapping_confirm_twice_applies_the_plan_once(wired, monkeypatch) -> None:
    telegram, calendar, scope = wired
    _freeze_now(monkeypatch)
    _stub_extraction(monkeypatch, SUGGEST)

    handlers.process_update(_voice_update(1), telegram)
    card = telegram.last_card
    with scope() as db:
        command_id = db.query(VoiceCommand).one().id

    handlers.process_update(_callback(command_id, "confirm", card["message_id"]), telegram)
    handlers.process_update(_callback(command_id, "confirm", card["message_id"]), telegram)

    assert len(calendar.yellow()) == 3, "a double tap must not double-book"
    with scope() as db:
        assert db.query(Meeting).count() == 1


def test_an_unauthorised_user_is_refused_politely_and_nothing_runs(wired) -> None:
    telegram, calendar, scope = wired
    update = _voice_update(1)
    update["message"]["from"]["id"] = 9999
    update["message"]["chat"]["id"] = 9999

    handlers.process_update(update, telegram)

    assert "not on its allow-list" in telegram.messages[-1]["text"]
    assert not calendar.events
    with scope() as db:
        assert db.query(VoiceCommand).count() == 0


def test_a_transcription_failure_reports_and_records(wired, monkeypatch) -> None:
    """T-27: a specific, actionable message — never silence."""
    from app.nlu.transcribe import TranscriptionError

    telegram, calendar, scope = wired

    def boom(data, filename="voice.ogg"):
        raise TranscriptionError("I could not transcribe that voice note.")

    monkeypatch.setattr(handlers, "transcribe", boom)
    handlers.process_update(_voice_update(1), telegram)

    assert "could not transcribe" in telegram.messages[-1]["text"]
    assert not calendar.events
    with scope() as db:
        command = db.query(VoiceCommand).one()
        assert command.status is CommandStatus.failed
        assert command.error_text


def test_an_unparseable_command_is_rejected_with_the_transcript(wired, monkeypatch) -> None:
    telegram, calendar, scope = wired
    _freeze_now(monkeypatch)
    monkeypatch.setattr(
        handlers, "transcribe", lambda data, filename="v.ogg": Transcription("mumble", "en")
    )
    _stub_extraction(
        monkeypatch,
        ExtractedIntent(intent=Intent.UNKNOWN, confidence=0.1, ambiguities=["Not a request."]),
    )

    handlers.process_update(_voice_update(1), telegram)

    text = telegram.messages[-1]["text"]
    assert "Not a request." in text
    assert "mumble" in text, "the owner must see what was heard"
    assert not calendar.events


def test_ambiguity_asks_and_the_answer_resolves_it(wired, monkeypatch) -> None:
    """T-19 end to end: ask, then act on the tapped answer."""
    telegram, calendar, scope = wired
    _freeze_now(monkeypatch)

    with scope() as db:
        from zoneinfo import ZoneInfo

        from tests.conftest import make_meeting

        user = db.query(handlers.User).one()
        yerevan = ZoneInfo("Asia/Yerevan")
        make_meeting(
            db,
            user,
            "Board call",
            [datetime(2026, 9, 16, 11, 0, tzinfo=yerevan).astimezone(UTC)],
            calendar=calendar,
        )
        make_meeting(
            db,
            user,
            "Client call",
            [datetime(2026, 9, 17, 15, 0, tzinfo=yerevan).astimezone(UTC)],
            calendar=calendar,
        )

    _stub_extraction(
        monkeypatch,
        ExtractedIntent(
            intent=Intent.CONFIRM_SLOT, meeting_reference="the call", confidence=0.4
        ),
    )
    handlers.process_update(_voice_update(1), telegram)

    card = telegram.last_card
    assert "Board call" in card["text"] and "Client call" in card["text"]
    assert len(calendar.green()) == 0

    with scope() as db:
        command_id = db.query(VoiceCommand).one().id
        chosen = db.query(Meeting).filter(Meeting.title == "Client call").one().id

    handlers.process_update(
        {
            "callback_query": {
                "id": "q1",
                "from": {"id": CHAT_ID},
                "data": f"pick:{command_id}:{chosen}",
                "message": {"message_id": card["message_id"], "chat": {"id": CHAT_ID}},
            }
        },
        telegram,
    )

    assert "Client call" in telegram.edits[-1]["text"]
    assert telegram.edits[-1]["buttons"], "picking a meeting yields a confirmable card"

    handlers.process_update(_callback(command_id, "confirm", card["message_id"]), telegram)
    with scope() as db:
        client = db.query(Meeting).filter(Meeting.title == "Client call").one()
        board = db.query(Meeting).filter(Meeting.title == "Board call").one()
        assert client.status is MeetingStatus.confirmed
        assert board.status is MeetingStatus.proposed, "the other meeting is untouched"


def test_pending_command_lists_open_proposals(wired, monkeypatch) -> None:
    telegram, calendar, scope = wired
    with scope() as db:
        from tests.conftest import make_meeting

        user = db.query(handlers.User).one()
        make_meeting(
            db, user, "Evocabank sync", [datetime(2026, 9, 16, 6, 0, tzinfo=UTC)]
        )

    handlers.process_update(
        {
            "message": {
                "message_id": 9,
                "chat": {"id": CHAT_ID},
                "from": {"id": CHAT_ID},
                "text": "/pending",
            }
        },
        telegram,
    )
    assert "Evocabank sync" in telegram.messages[-1]["text"]


def test_tz_command_switches_the_interpretation_zone(wired, monkeypatch) -> None:
    """T-14: /tz while travelling."""
    telegram, calendar, scope = wired

    handlers.process_update(
        {
            "message": {
                "message_id": 9,
                "chat": {"id": CHAT_ID},
                "from": {"id": CHAT_ID},
                "text": "/tz Europe/Vienna",
            }
        },
        telegram,
    )
    assert "Europe/Vienna" in telegram.messages[-1]["text"]
    with scope() as db:
        assert db.query(handlers.User).one().home_timezone == "Europe/Vienna"

    handlers.process_update(
        {
            "message": {
                "message_id": 10,
                "chat": {"id": CHAT_ID},
                "from": {"id": CHAT_ID},
                "text": "/tz Mars/Olympus",
            }
        },
        telegram,
    )
    assert "not an IANA timezone" in telegram.messages[-1]["text"]
    with scope() as db:
        assert db.query(handlers.User).one().home_timezone == "Europe/Vienna"


def test_an_unconnected_account_is_sent_an_auth_link(wired, monkeypatch) -> None:
    telegram, calendar, scope = wired
    with scope() as db:
        db.query(handlers.User).one().google_refresh_token_encrypted = None

    handlers.process_update(_voice_update(1), telegram)

    assert "not connected" in telegram.messages[-1]["text"]
    assert "accounts.google.com" in telegram.messages[-1]["text"]
    assert not calendar.events
