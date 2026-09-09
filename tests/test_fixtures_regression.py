"""T-25: run every fixture transcript through the deterministic layers.

The extraction output is supplied by the fixture (so this suite needs no API
key and no network); everything below it — time resolution, meeting matching,
plan construction — is exercised for real.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import Session

from app.models import MeetingStatus, SlotState, User
from app.services.planning import build_plan
from tests.conftest import make_meeting
from tests.fixtures.transcripts import FIXTURES, Fixture

NOW = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)  # Mon 14 Sep, 13:00 Yerevan
YEREVAN = ZoneInfo("Asia/Yerevan")


def _local(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=YEREVAN)


def _seed(session: Session, user: User, fixture: Fixture) -> None:
    confirmed = fixture.extra.get("confirmed", False)
    for title, times in fixture.seed_meetings:
        make_meeting(
            session,
            user,
            title,
            [_local(t).astimezone(UTC) for t in times],
            status=MeetingStatus.confirmed if confirmed else MeetingStatus.proposed,
            state=SlotState.fixed if confirmed else SlotState.suggested,
        )


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.id)
def test_fixture_produces_the_expected_plan(
    session: Session, user: User, fixture: Fixture
) -> None:
    _seed(session, user, fixture)

    plan = build_plan(session, user, fixture.intent, now_utc=NOW)

    assert plan.action == fixture.expected_action, (
        f"{fixture.id}: expected {fixture.expected_action}, got {plan.action} "
        f"(error={plan.error!r}, question={plan.question!r})"
    )

    if fixture.expected_slot_count:
        assert len(plan.slots) == fixture.expected_slot_count, fixture.id

    if fixture.expected_local_times:
        actual = [
            s.start_utc.astimezone(ZoneInfo(s.timezone_name)).strftime("%Y-%m-%d %H:%M")
            for s in plan.slots
        ]
        assert actual == fixture.expected_local_times, fixture.id

    if fixture.expected_title:
        assert plan.title == fixture.expected_title

    if "expected_siblings" in fixture.extra:
        assert plan.sibling_slot_count == fixture.extra["expected_siblings"], fixture.id

    if "expected_duration" in fixture.extra:
        assert plan.duration_minutes == fixture.extra["expected_duration"], fixture.id

    if "expected_timezone" in fixture.extra:
        assert plan.slots[0].timezone_name == fixture.extra["expected_timezone"], fixture.id


def test_every_language_is_covered() -> None:
    """D4: Armenian, English, German and Russian all have fixtures."""
    languages = {f.language for f in FIXTURES}
    assert {"en", "de", "ru", "hy"} <= languages


def test_fixture_set_covers_every_required_scenario() -> None:
    """The scenario list from T-25, asserted so coverage cannot silently regress."""
    actions = [f.expected_action for f in FIXTURES]
    for required in (
        "create_fixed",
        "create_suggested",
        "confirm",
        "cancel",
        "reschedule",
        "clarify",
        "reject",
        "list_pending",
    ):
        assert required in actions, f"no fixture covers {required}"
    assert len(FIXTURES) >= 30, "T-25 requires at least 30 transcripts"


def test_ambiguous_confirmations_never_guess(session: Session, user: User) -> None:
    """T-19 acceptance, isolated: two meetings containing 'call' must prompt."""
    fixture = next(f for f in FIXTURES if f.id == "en-confirm-ambiguous")
    _seed(session, user, fixture)
    plan = build_plan(session, user, fixture.intent, now_utc=NOW)
    assert plan.action == "clarify"
    assert plan.meeting_id is None
    assert len(plan.candidates) >= 2
    assert plan.question
