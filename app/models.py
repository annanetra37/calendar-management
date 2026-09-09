"""SQLAlchemy models (spec section 4).

The central idea: a *meeting* is a logical group; a *slot* is one calendar
event belonging to that group. A proposed meeting has N slots; confirming
collapses N -> 1.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON, TypeDecorator


class Base(DeclarativeBase):
    pass


# JSONB on Postgres, plain JSON elsewhere (tests run on SQLite).
JsonType = JSON().with_variant(JSONB(), "postgresql")


class UtcDateTime(TypeDecorator):
    """A timestamp that is always stored and returned as aware UTC.

    Postgres ``timestamptz`` already round-trips a timezone, but backends that
    do not (SQLite, used by the tests) hand back naive values that then blow up
    on comparison. Normalising here means the rest of the codebase can assume
    every datetime it reads is aware.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        return value if value.tzinfo else value.replace(tzinfo=UTC)


def utcnow() -> datetime:
    return datetime.now(UTC)


class MeetingStatus(str, enum.Enum):
    proposed = "proposed"
    confirmed = "confirmed"
    cancelled = "cancelled"
    expired = "expired"


class SlotState(str, enum.Enum):
    suggested = "suggested"
    fixed = "fixed"
    removed = "removed"
    failed = "failed"


class CommandStatus(str, enum.Enum):
    pending_confirmation = "pending_confirmation"
    applied = "applied"
    rejected = "rejected"
    failed = "failed"


def _enum(py_enum: type[enum.Enum], name: str) -> Enum:
    return Enum(
        py_enum,
        name=name,
        native_enum=True,
        values_callable=lambda e: [m.value for m in e],
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    google_email: Mapped[str | None] = mapped_column(String(320))
    google_refresh_token_encrypted: Mapped[str | None] = mapped_column(Text)
    google_calendar_id: Mapped[str] = mapped_column(String(255), default="primary")
    home_timezone: Mapped[str] = mapped_column(String(64), default="Asia/Yerevan")
    default_duration_minutes: Mapped[int] = mapped_column(Integer, default=60)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)

    meetings: Mapped[list[Meeting]] = relationship(back_populates="user")
    sync_state: Mapped[SyncState | None] = relationship(
        back_populates="user", uselist=False
    )

    @property
    def is_google_connected(self) -> bool:
        return bool(self.google_refresh_token_encrypted)


class Meeting(Base):
    """The logical group. One meeting -> 1..N slots."""

    __tablename__ = "meetings"
    __table_args__ = (
        Index("ix_meetings_user_status", "user_id", "status"),
        Index("ix_meetings_user_normalized_title", "user_id", "normalized_title"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(500))
    normalized_title: Mapped[str] = mapped_column(String(500))
    status: Mapped[MeetingStatus] = mapped_column(
        _enum(MeetingStatus, "meeting_status"), default=MeetingStatus.proposed
    )
    duration_minutes: Mapped[int] = mapped_column(Integer, default=60)
    attendees_json: Mapped[list | None] = mapped_column(JsonType, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(UtcDateTime())

    user: Mapped[User] = relationship(back_populates="meetings")
    slots: Mapped[list[Slot]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan", order_by="Slot.start_utc"
    )

    def live_slots(self) -> list[Slot]:
        return [s for s in self.slots if s.state in (SlotState.suggested, SlotState.fixed)]


class Slot(Base):
    """One calendar event belonging to a meeting."""

    __tablename__ = "slots"
    __table_args__ = (
        Index("ix_slots_meeting_state", "meeting_id", "state"),
        Index("ix_slots_gcal_event_id", "gcal_event_id"),
        UniqueConstraint("idempotency_key", name="uq_slots_idempotency_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meeting_id: Mapped[int] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"))
    start_utc: Mapped[datetime] = mapped_column(UtcDateTime())
    end_utc: Mapped[datetime] = mapped_column(UtcDateTime())
    gcal_event_id: Mapped[str | None] = mapped_column(String(1024))
    gcal_etag: Mapped[str | None] = mapped_column(String(255))
    state: Mapped[SlotState] = mapped_column(
        _enum(SlotState, "slot_state"), default=SlotState.suggested
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    removed_at: Mapped[datetime | None] = mapped_column(UtcDateTime())

    meeting: Mapped[Meeting] = relationship(back_populates="slots")


class VoiceCommand(Base):
    """Full audit trail of everything the owner said and what it produced."""

    __tablename__ = "voice_commands"
    __table_args__ = (
        Index("ix_voice_commands_user_status", "user_id", "status"),
        UniqueConstraint(
            "user_id", "telegram_message_id", name="uq_voice_commands_user_message"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_card_message_id: Mapped[int | None] = mapped_column(BigInteger)
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    audio_storage_ref: Mapped[str | None] = mapped_column(String(1024))
    audio_purged: Mapped[bool] = mapped_column(Boolean, default=False)
    transcript: Mapped[str | None] = mapped_column(Text)
    language_detected: Mapped[str | None] = mapped_column(String(16))
    parsed_intent_json: Mapped[dict | None] = mapped_column(JsonType)
    plan_json: Mapped[dict | None] = mapped_column(JsonType)
    resolved_meeting_id: Mapped[int | None] = mapped_column(
        ForeignKey("meetings.id", ondelete="SET NULL")
    )
    status: Mapped[CommandStatus] = mapped_column(
        _enum(CommandStatus, "command_status"), default=CommandStatus.pending_confirmation
    )
    awaiting_edit: Mapped[bool] = mapped_column(Boolean, default=False)
    error_text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime())


class SyncState(Base):
    __tablename__ = "sync_state"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    gcal_sync_token: Mapped[str | None] = mapped_column(Text)
    gcal_channel_id: Mapped[str | None] = mapped_column(String(255), index=True)
    gcal_resource_id: Mapped[str | None] = mapped_column(String(255))
    channel_expiry: Mapped[datetime | None] = mapped_column(UtcDateTime())
    last_reconciled_at: Mapped[datetime | None] = mapped_column(UtcDateTime())

    user: Mapped[User] = relationship(back_populates="sync_state")


class OAuthState(Base):
    """Short-lived CSRF state for the Google OAuth handshake."""

    __tablename__ = "oauth_states"

    state: Mapped[str] = mapped_column(String(128), primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
