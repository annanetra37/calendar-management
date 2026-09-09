"""Strict JSON schema for the intent-extraction layer (spec section 5)."""

from __future__ import annotations

import enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Intent(str, enum.Enum):
    CREATE_FIXED = "CREATE_FIXED"
    CREATE_SUGGESTED = "CREATE_SUGGESTED"
    CONFIRM_SLOT = "CONFIRM_SLOT"
    CANCEL_MEETING = "CANCEL_MEETING"
    RESCHEDULE = "RESCHEDULE"
    LIST_PENDING = "LIST_PENDING"
    UNKNOWN = "UNKNOWN"


class SpokenSlot(BaseModel):
    """One date/time as *spoken*. Resolution to UTC happens in app.nlu.timeparse."""

    model_config = ConfigDict(extra="ignore")

    date: str | None = Field(
        default=None,
        description="ISO date YYYY-MM-DD if the model could resolve it, else null",
    )
    time: str | None = Field(default=None, description="24h local time HH:MM, else null")
    raw: str = Field(default="", description="The exact spoken phrase, e.g. 'next Tuesday at 3'")
    timezone: str | None = Field(
        default=None,
        description="IANA zone only when the speaker named one, e.g. 'Europe/Vienna'",
    )

    @field_validator("date", "time", "timezone", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value


class ExtractedIntent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    intent: Intent = Intent.UNKNOWN
    title: str | None = None
    meeting_reference: str | None = Field(
        default=None,
        description="How the speaker referred to an existing meeting ('the Evocabank one')",
    )
    meeting_id: int | None = Field(
        default=None, description="Set only when the speaker's reference is unambiguous"
    )
    slots: list[SpokenSlot] = Field(default_factory=list)
    duration_minutes: int | None = None
    attendees: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    ambiguities: list[str] = Field(default_factory=list)
    notes: str | None = None

    @field_validator("intent", mode="before")
    @classmethod
    def _tolerant_intent(cls, value: Any) -> Any:
        if isinstance(value, str):
            candidate = value.strip().upper()
            if candidate in Intent.__members__:
                return candidate
            return Intent.UNKNOWN.value
        return value

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp(cls, value: Any) -> Any:
        try:
            return min(1.0, max(0.0, float(value)))
        except (TypeError, ValueError):
            return 0.0

    @field_validator("duration_minutes", mode="before")
    @classmethod
    def _sane_duration(cls, value: Any) -> Any:
        if value in (None, "", 0):
            return None
        try:
            minutes = int(value)
        except (TypeError, ValueError):
            return None
        return minutes if 5 <= minutes <= 24 * 60 else None

    @field_validator("attendees", "ambiguities", mode="before")
    @classmethod
    def _listify(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        return value


#: JSON Schema handed to the model as a tool definition / structured output spec.
INTENT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["intent", "slots", "confidence", "ambiguities"],
    "properties": {
        "intent": {
            "type": "string",
            "enum": [i.value for i in Intent],
            "description": "The single best-matching intent for the utterance.",
        },
        "title": {
            "type": ["string", "null"],
            "description": "Meeting title for CREATE_* intents. Omit filler words.",
        },
        "meeting_reference": {
            "type": ["string", "null"],
            "description": (
                "For CONFIRM_SLOT / CANCEL_MEETING / RESCHEDULE: the words the "
                "speaker used to point at an existing meeting."
            ),
        },
        "meeting_id": {
            "type": ["integer", "null"],
            "description": (
                "Id from the open-meetings list, ONLY when the reference matches "
                "exactly one of them. Never guess."
            ),
        },
        "slots": {
            "type": "array",
            "description": (
                "Every date/time the speaker mentioned, in spoken order. "
                "CREATE_SUGGESTED routinely carries several."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["raw"],
                "properties": {
                    "date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
                    "time": {"type": ["string", "null"], "description": "HH:MM 24-hour"},
                    "raw": {"type": "string", "description": "Exact spoken phrase"},
                    "timezone": {
                        "type": ["string", "null"],
                        "description": "IANA zone, only if the speaker named one",
                    },
                },
            },
        },
        "duration_minutes": {"type": ["integer", "null"]},
        "attendees": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "ambiguities": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Anything the transcript left genuinely unclear.",
        },
        "notes": {"type": ["string", "null"]},
    },
}
