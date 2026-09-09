"""T-12: the extraction layer must never crash the handler."""

from __future__ import annotations

import json

import httpx
import pytest

from app.nlu.extract import ExtractionError, extract_intent, parse_model_output
from app.nlu.schema import INTENT_JSON_SCHEMA, Intent

GOOD = {
    "intent": "CREATE_SUGGESTED",
    "title": "Evocabank meeting",
    "slots": [
        {"raw": "Tuesday 2pm", "date": "2026-09-15", "time": "14:00"},
        {"raw": "Wednesday 10", "date": "2026-09-16", "time": "10:00"},
    ],
    "confidence": 0.9,
    "ambiguities": [],
}


def test_a_well_formed_payload_parses() -> None:
    intent = parse_model_output(GOOD)
    assert intent.intent is Intent.CREATE_SUGGESTED
    assert len(intent.slots) == 2
    assert intent.slots[0].raw == "Tuesday 2pm"


def test_a_json_string_parses() -> None:
    assert parse_model_output(json.dumps(GOOD)).intent is Intent.CREATE_SUGGESTED


def test_json_wrapped_in_prose_is_recovered() -> None:
    text = f"Sure! Here you go:\n```json\n{json.dumps(GOOD)}\n```\nHope that helps."
    assert parse_model_output(text).intent is Intent.CREATE_SUGGESTED


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "",
        "this is not json at all",
        "{ truncated: ",
        "[1, 2, 3]",
        123,
        {"intent": {"nested": "object"}},
    ],
)
def test_malformed_output_degrades_to_unknown_without_raising(payload) -> None:
    intent = parse_model_output(payload)
    assert intent.intent is Intent.UNKNOWN
    assert intent.confidence == 0.0
    assert intent.ambiguities, "the reason must be recorded for the owner"


def test_an_unrecognised_intent_name_becomes_unknown() -> None:
    assert parse_model_output({"intent": "BOOK_FLIGHT"}).intent is Intent.UNKNOWN


def test_out_of_range_values_are_clamped_or_dropped() -> None:
    intent = parse_model_output(
        {"intent": "CREATE_FIXED", "confidence": 7.5, "duration_minutes": 99999, "slots": []}
    )
    assert intent.confidence == 1.0
    assert intent.duration_minutes is None


def test_string_ambiguities_are_coerced_to_a_list() -> None:
    intent = parse_model_output({"intent": "CREATE_FIXED", "ambiguities": "just one"})
    assert intent.ambiguities == ["just one"]


def test_blank_date_and_time_become_none() -> None:
    intent = parse_model_output(
        {"intent": "CREATE_FIXED", "slots": [{"raw": "Tuesday", "date": "  ", "time": ""}]}
    )
    assert intent.slots[0].date is None and intent.slots[0].time is None


def test_extra_keys_from_the_model_are_ignored() -> None:
    payload = dict(GOOD, hallucinated_field="ignore me")
    assert parse_model_output(payload).intent is Intent.CREATE_SUGGESTED


def test_the_schema_advertises_every_intent() -> None:
    advertised = set(INTENT_JSON_SCHEMA["properties"]["intent"]["enum"])
    assert advertised == {i.value for i in Intent}
    for required in ("intent", "slots", "confidence", "ambiguities"):
        assert required in INTENT_JSON_SCHEMA["required"]


def test_anthropic_tool_use_output_is_read(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["tool_choice"]["name"] == "record_scheduling_intent"
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "text", "text": "thinking"},
                    {"type": "tool_use", "name": "record_scheduling_intent", "input": GOOD},
                ]
            },
        )

    from datetime import datetime

    intent = extract_intent(
        "suggest something",
        now_local=datetime(2026, 9, 14, 13, 0),
        timezone_name="Asia/Yerevan",
        open_meetings=[],
        default_duration_minutes=60,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert intent.intent is Intent.CREATE_SUGGESTED


def test_an_api_error_raises_extraction_error() -> None:
    from datetime import datetime

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(529, json={"error": "overloaded"})

    with pytest.raises(ExtractionError):
        extract_intent(
            "anything",
            now_local=datetime(2026, 9, 14, 13, 0),
            timezone_name="Asia/Yerevan",
            open_meetings=[],
            default_duration_minutes=60,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )


def test_the_prompt_carries_time_zone_and_open_meetings() -> None:
    from datetime import datetime

    from app.nlu.prompt import OpenMeetingSummary, build_user_prompt

    prompt = build_user_prompt(
        "Evocabank is confirmed for Wednesday",
        now_local=datetime(2026, 9, 14, 13, 0),
        timezone_name="Asia/Yerevan",
        open_meetings=[
            OpenMeetingSummary(7, "Evocabank meeting", ["Tue 15 Sep 14:00", "Wed 16 Sep 10:00"])
        ],
        default_duration_minutes=60,
    )
    assert "2026-09-14" in prompt
    assert "Asia/Yerevan" in prompt
    assert "id=7" in prompt
    assert "Wed 16 Sep 10:00" in prompt
    assert "Evocabank is confirmed for Wednesday" in prompt


def test_a_correction_is_passed_through_to_the_model() -> None:
    from datetime import datetime

    from app.nlu.prompt import build_user_prompt

    prompt = build_user_prompt(
        "meeting at four",
        now_local=datetime(2026, 9, 14, 13, 0),
        timezone_name="Asia/Yerevan",
        open_meetings=[],
        default_duration_minutes=60,
        correction="no, 4pm not 4am",
    )
    assert "no, 4pm not 4am" in prompt
    assert "correction wins" in prompt
