"""Voice-note ingest and retention (T-10).

Audio is capped in size and duration, stored for a configurable window so a
mis-transcription can be investigated, and purged by the daily job.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.integrations.telegram import TelegramClient, TelegramError
from app.models import VoiceCommand, utcnow

log = logging.getLogger(__name__)

MIME_EXTENSIONS = {
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/wav": ".wav",
    "audio/webm": ".webm",
    "video/mp4": ".mp4",
}


class AudioRejected(ValueError):
    """The voice note breaches a size or duration cap."""


@dataclass(slots=True)
class VoiceNote:
    data: bytes
    filename: str
    mime_type: str
    duration_seconds: int
    storage_ref: str | None = None


def download_voice(
    telegram: TelegramClient, voice: dict, *, user_id: int, message_id: int
) -> VoiceNote:
    settings = get_settings()
    duration = int(voice.get("duration") or 0)
    size = int(voice.get("file_size") or 0)

    if duration > settings.max_audio_seconds:
        raise AudioRejected(
            f"That note is {duration}s long; I only handle up to "
            f"{settings.max_audio_seconds}s. Please send a shorter one."
        )
    if size and size > settings.max_audio_bytes:
        raise AudioRejected("That voice note is too large for me to process.")

    try:
        file_info = telegram.get_file(voice["file_id"])
        data = telegram.download_file(file_info["file_path"], max_bytes=settings.max_audio_bytes)
    except TelegramError as exc:
        raise AudioRejected(f"I could not download that voice note: {exc}") from exc

    mime = voice.get("mime_type") or "audio/ogg"
    extension = MIME_EXTENSIONS.get(
        mime, Path(file_info.get("file_path", "a.ogg")).suffix or ".ogg"
    )
    return VoiceNote(
        data=data,
        filename=f"voice-{user_id}-{message_id}{extension}",
        mime_type=mime,
        duration_seconds=duration,
    )


def persist(note: VoiceNote) -> str | None:
    """Store the blob for the retention window. Never fatal if it fails."""
    settings = get_settings()
    if settings.audio_retention_days <= 0:
        return None
    try:
        directory = Path(settings.audio_storage_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / note.filename
        path.write_bytes(note.data)
        note.storage_ref = str(path)
        return str(path)
    except OSError as exc:
        log.warning("audio_persist_failed", extra={"error": str(exc)})
        return None


def purge_expired(session: Session) -> int:
    """Delete audio blobs past the retention window (T-24)."""
    settings = get_settings()
    if settings.audio_retention_days <= 0:
        return 0
    cutoff = utcnow() - timedelta(days=settings.audio_retention_days)
    stmt = select(VoiceCommand).where(
        VoiceCommand.created_at < cutoff,
        VoiceCommand.audio_purged.is_(False),
        VoiceCommand.audio_storage_ref.is_not(None),
    )
    purged = 0
    for command in session.scalars(stmt):
        try:
            os.remove(command.audio_storage_ref)  # type: ignore[arg-type]
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("audio_purge_failed", extra={"command_id": command.id, "error": str(exc)})
            continue
        command.audio_purged = True
        command.audio_storage_ref = None
        purged += 1
    return purged
