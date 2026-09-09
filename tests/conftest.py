from __future__ import annotations

import base64
import os
from datetime import UTC, datetime, timedelta

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://postgres@localhost/unused")
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("PUBLIC_BASE_URL", "https://scheduler.example.test")
os.environ.setdefault("FERNET_KEY", base64.urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-secret")
os.environ.setdefault(
    "GOOGLE_REDIRECT_URI", "https://scheduler.example.test/auth/google/callback"
)
os.environ.setdefault("GOOGLE_WEBHOOK_TOKEN", "goog-token")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:ABC")
os.environ.setdefault("TELEGRAM_WEBHOOK_SECRET", "tg-secret")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_IDS", "4242")
os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test")
os.environ.setdefault("DEFAULT_TIMEZONE", "Asia/Yerevan")

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, Meeting, MeetingStatus, Slot, SlotState, User
from app.services.matching import normalize_title
from tests.fakes import FakeCalendar

YEREVAN_MONDAY = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)  # Mon 13:00 Yerevan


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with maker() as db:
        yield db
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture()
def user(session: Session) -> User:
    record = User(
        telegram_user_id=4242,
        home_timezone="Asia/Yerevan",
        default_duration_minutes=60,
        google_calendar_id="primary",
        google_refresh_token_encrypted=None,
    )
    session.add(record)
    session.flush()
    return record


@pytest.fixture()
def calendar() -> FakeCalendar:
    return FakeCalendar()


@pytest.fixture()
def now_utc() -> datetime:
    return YEREVAN_MONDAY


def make_meeting(
    session: Session,
    user: User,
    title: str,
    starts: list[datetime],
    *,
    status: MeetingStatus = MeetingStatus.proposed,
    state: SlotState = SlotState.suggested,
    calendar: FakeCalendar | None = None,
) -> Meeting:
    meeting = Meeting(
        user_id=user.id,
        title=title,
        normalized_title=normalize_title(title),
        status=status,
        duration_minutes=60,
    )
    session.add(meeting)
    session.flush()
    for index, start in enumerate(starts):
        slot = Slot(
            meeting_id=meeting.id,
            start_utc=start,
            end_utc=start + timedelta(minutes=60),
            state=state,
            idempotency_key=f"seed-{meeting.id}-{index}",
        )
        if calendar is not None:
            event = calendar.seed_event(
                event_id=f"evt-{meeting.id}-{index}",
                meeting_id=meeting.id,
                start=start,
                end=start + timedelta(minutes=60),
                tentative=state is SlotState.suggested,
                title=title,
            )
            slot.gcal_event_id = event["id"]
            slot.gcal_etag = event["etag"]
        session.add(slot)
    session.flush()
    session.refresh(meeting)
    return meeting
