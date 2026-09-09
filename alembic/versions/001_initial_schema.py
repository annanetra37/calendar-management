"""Initial schema: users, meetings, slots, voice_commands, sync_state, oauth_states.

Revision ID: 001_initial
Revises:
Create Date: 2026-09-09
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# create_type=False: the types are created once, explicitly, at the top of
# upgrade(). Without it every table referencing an enum tries to create it again.
meeting_status = postgresql.ENUM(
    "proposed", "confirmed", "cancelled", "expired",
    name="meeting_status", create_type=False,
)
slot_state = postgresql.ENUM(
    "suggested", "fixed", "removed", "failed", name="slot_state", create_type=False
)
command_status = postgresql.ENUM(
    "pending_confirmation", "applied", "rejected", "failed",
    name="command_status", create_type=False,
)

JSONB = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    bind = op.get_bind()
    meeting_status.create(bind, checkfirst=True)
    slot_state.create(bind, checkfirst=True)
    command_status.create(bind, checkfirst=True)

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("google_email", sa.String(length=320)),
        sa.Column("google_refresh_token_encrypted", sa.Text()),
        sa.Column(
            "google_calendar_id", sa.String(length=255), server_default="primary", nullable=False
        ),
        sa.Column(
            "home_timezone", sa.String(length=64), server_default="Asia/Yerevan", nullable=False
        ),
        sa.Column(
            "default_duration_minutes", sa.Integer(), server_default="60", nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )
    op.create_index("ix_users_telegram_user_id", "users", ["telegram_user_id"], unique=True)

    op.create_table(
        "meetings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("normalized_title", sa.String(length=500), nullable=False),
        sa.Column("status", meeting_status, nullable=False, server_default="proposed"),
        sa.Column("duration_minutes", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("attendees_json", JSONB),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_meetings_user_status", "meetings", ["user_id", "status"])
    op.create_index(
        "ix_meetings_user_normalized_title", "meetings", ["user_id", "normalized_title"]
    )

    op.create_table(
        "slots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("meeting_id", sa.Integer(), nullable=False),
        sa.Column("start_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("gcal_event_id", sa.String(length=1024)),
        sa.Column("gcal_etag", sa.String(length=255)),
        sa.Column("state", slot_state, nullable=False, server_default="suggested"),
        sa.Column("idempotency_key", sa.String(length=128)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("removed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["meeting_id"], ["meetings.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("idempotency_key", name="uq_slots_idempotency_key"),
    )
    op.create_index("ix_slots_meeting_state", "slots", ["meeting_id", "state"])
    op.create_index("ix_slots_gcal_event_id", "slots", ["gcal_event_id"])

    op.create_table(
        "voice_commands",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger()),
        sa.Column("telegram_card_message_id", sa.BigInteger()),
        sa.Column("correlation_id", sa.String(length=64)),
        sa.Column("audio_storage_ref", sa.String(length=1024)),
        sa.Column("audio_purged", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("transcript", sa.Text()),
        sa.Column("language_detected", sa.String(length=16)),
        sa.Column("parsed_intent_json", JSONB),
        sa.Column("plan_json", JSONB),
        sa.Column("resolved_meeting_id", sa.Integer()),
        sa.Column(
            "status", command_status, nullable=False, server_default="pending_confirmation"
        ),
        sa.Column("awaiting_edit", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error_text", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["resolved_meeting_id"], ["meetings.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "user_id", "telegram_message_id", name="uq_voice_commands_user_message"
        ),
    )
    op.create_index("ix_voice_commands_user_status", "voice_commands", ["user_id", "status"])

    op.create_table(
        "sync_state",
        sa.Column("user_id", sa.Integer(), primary_key=True),
        sa.Column("gcal_sync_token", sa.Text()),
        sa.Column("gcal_channel_id", sa.String(length=255)),
        sa.Column("gcal_resource_id", sa.String(length=255)),
        sa.Column("channel_expiry", sa.DateTime(timezone=True)),
        sa.Column("last_reconciled_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_sync_state_gcal_channel_id", "sync_state", ["gcal_channel_id"])

    op.create_table(
        "oauth_states",
        sa.Column("state", sa.String(length=128), primary_key=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("oauth_states")
    op.drop_index("ix_sync_state_gcal_channel_id", table_name="sync_state")
    op.drop_table("sync_state")
    op.drop_index("ix_voice_commands_user_status", table_name="voice_commands")
    op.drop_table("voice_commands")
    op.drop_index("ix_slots_gcal_event_id", table_name="slots")
    op.drop_index("ix_slots_meeting_state", table_name="slots")
    op.drop_table("slots")
    op.drop_index("ix_meetings_user_normalized_title", table_name="meetings")
    op.drop_index("ix_meetings_user_status", table_name="meetings")
    op.drop_table("meetings")
    op.drop_index("ix_users_telegram_user_id", table_name="users")
    op.drop_table("users")

    bind = op.get_bind()
    command_status.drop(bind, checkfirst=True)
    slot_state.drop(bind, checkfirst=True)
    meeting_status.drop(bind, checkfirst=True)
