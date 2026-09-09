"""Intent extraction (T-12).

The model is called with a strict JSON schema (Anthropic tool use, or OpenAI
structured output). Malformed model output is caught and turned into an
``UNKNOWN`` intent — it never crashes the handler.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

import httpx
from pydantic import ValidationError

from app.config import get_settings
from app.nlu.prompt import SYSTEM_PROMPT, OpenMeetingSummary, build_user_prompt
from app.nlu.schema import INTENT_JSON_SCHEMA, ExtractedIntent, Intent

log = logging.getLogger(__name__)

TOOL_NAME = "record_scheduling_intent"


class ExtractionError(RuntimeError):
    """The extraction provider was unreachable or refused the request."""


def extract_intent(
    transcript: str,
    *,
    now_local: datetime,
    timezone_name: str,
    open_meetings: list[OpenMeetingSummary],
    default_duration_minutes: int,
    correction: str | None = None,
    previous_intent_json: str | None = None,
    client: httpx.Client | None = None,
) -> ExtractedIntent:
    settings = get_settings()
    user_prompt = build_user_prompt(
        transcript,
        now_local=now_local,
        timezone_name=timezone_name,
        open_meetings=open_meetings,
        default_duration_minutes=default_duration_minutes,
        correction=correction,
        previous_intent_json=previous_intent_json,
    )

    owns_client = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(45.0, connect=10.0))
    try:
        if settings.extraction_provider == "anthropic":
            raw = _call_anthropic(client, settings, user_prompt)
        else:
            raw = _call_openai(client, settings, user_prompt)
    finally:
        if owns_client:
            client.close()

    return parse_model_output(raw)


def parse_model_output(raw: str | dict | None) -> ExtractedIntent:
    """Turn whatever the model produced into a valid ``ExtractedIntent``.

    Never raises: a model that returns prose, truncated JSON or the wrong shape
    yields an ``UNKNOWN`` intent with the problem recorded in ``ambiguities``.
    """
    if raw is None:
        return _unknown("The extraction model returned nothing.")

    payload = raw
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            extracted = _first_json_object(payload)
            if extracted is None:
                log.warning("extraction_non_json_output")
                return _unknown("The extraction model did not return JSON.")
            payload = extracted

    if not isinstance(payload, dict):
        return _unknown("The extraction model returned an unexpected shape.")

    try:
        return ExtractedIntent.model_validate(payload)
    except ValidationError as exc:
        log.warning("extraction_schema_violation", extra={"errors": exc.error_count()})
        return _unknown("The extraction model returned data I could not read.")


def _unknown(reason: str) -> ExtractedIntent:
    return ExtractedIntent(intent=Intent.UNKNOWN, confidence=0.0, ambiguities=[reason])


def _first_json_object(text: str) -> dict | None:
    """Recover a JSON object embedded in prose or a fenced code block."""
    start = text.find("{")
    while start != -1:
        depth, in_string, escaped = 0, False, False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        candidate = json.loads(text[start : index + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(candidate, dict):
                        return candidate
                    break
        start = text.find("{", start + 1)
    return None


def _call_anthropic(client: httpx.Client, settings, user_prompt: str) -> str | dict | None:
    if not settings.anthropic_api_key:
        raise ExtractionError("ANTHROPIC_API_KEY is not set but EXTRACTION_PROVIDER=anthropic.")
    response = client.post(
        f"{settings.anthropic_base_url}/v1/messages",
        headers={
            "x-api-key": settings.anthropic_api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": settings.extraction_model,
            "max_tokens": 2000,
            "system": SYSTEM_PROMPT,
            "tools": [
                {
                    "name": TOOL_NAME,
                    "description": "Record the scheduling intent extracted from the transcript.",
                    "input_schema": INTENT_JSON_SCHEMA,
                }
            ],
            "tool_choice": {"type": "tool", "name": TOOL_NAME},
            "messages": [{"role": "user", "content": user_prompt}],
        },
    )
    if response.status_code >= 400:
        raise ExtractionError(f"Anthropic API returned {response.status_code}.")
    for block in response.json().get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == TOOL_NAME:
            return block.get("input")
    return None


def _call_openai(client: httpx.Client, settings, user_prompt: str) -> str | dict | None:
    schema = dict(INTENT_JSON_SCHEMA)
    response = client.post(
        f"{settings.openai_base_url}/chat/completions",
        headers={
            "Authorization": f"Bearer {settings.openai_api_key}",
            "content-type": "application/json",
        },
        json={
            "model": settings.openai_extraction_model,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": TOOL_NAME,
                    "strict": False,
                    "schema": schema,
                },
            },
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        },
    )
    if response.status_code >= 400:
        raise ExtractionError(f"OpenAI API returned {response.status_code}.")
    choices = response.json().get("choices") or []
    if not choices:
        return None
    return choices[0].get("message", {}).get("content")
