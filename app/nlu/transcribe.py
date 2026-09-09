"""Whisper transcription (T-11).

Language is auto-detected. One retry, then a specific owner-facing error —
never silent failure.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)


class TranscriptionError(RuntimeError):
    """Whisper could not produce a transcript."""


@dataclass(slots=True)
class Transcription:
    text: str
    language: str | None
    duration_seconds: float | None = None


def transcribe(
    audio: bytes, filename: str = "voice.ogg", *, client: httpx.Client | None = None
) -> Transcription:
    settings = get_settings()
    owns_client = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0))
    try:
        last_error: Exception | None = None
        for attempt in (1, 2):
            try:
                response = client.post(
                    f"{settings.openai_base_url}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                    files={"file": (filename, audio, "audio/ogg")},
                    data={
                        "model": settings.whisper_model,
                        "response_format": "verbose_json",
                        # A domain hint measurably improves proper-noun accuracy.
                        "prompt": (
                            "Business scheduling voice note. May contain company "
                            "names and weekday/time expressions."
                        ),
                    },
                )
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:
                last_error = exc
                log.warning("whisper_attempt_failed", extra={"attempt": attempt, "error": str(exc)})
                if attempt == 1:
                    time.sleep(1.5)
                continue

            text = (payload.get("text") or "").strip()
            if not text:
                last_error = TranscriptionError("Whisper returned an empty transcript.")
                log.warning("whisper_empty_transcript", extra={"attempt": attempt})
                if attempt == 1:
                    time.sleep(1.5)
                continue

            return Transcription(
                text=text,
                language=payload.get("language"),
                duration_seconds=payload.get("duration"),
            )

        raise TranscriptionError(
            "I could not transcribe that voice note. Please try again, "
            "ideally somewhere quieter."
        ) from last_error
    finally:
        if owns_client:
            client.close()
