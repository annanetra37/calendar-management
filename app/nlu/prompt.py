"""The extraction prompt (spec section 5)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

SYSTEM_PROMPT = """\
You extract scheduling intent from a transcribed voice note and return it as \
structured data. The speaker may use English, Armenian, German or Russian, and \
may mix them in one sentence.

Rules:
1. Pick exactly one intent.
   - CREATE_FIXED: a single, definite meeting time ("fixed", "confirmed", "definitely").
   - CREATE_SUGGESTED: one or more CANDIDATE times for a meeting that is not yet
     agreed ("suggest", "propose", "options", "or"). A single suggested time is
     still CREATE_SUGGESTED.
   - CONFIRM_SLOT: an existing proposed meeting has been agreed for a specific time.
   - CANCEL_MEETING: drop an existing meeting entirely.
   - RESCHEDULE: move an already-confirmed meeting to a new time.
   - LIST_PENDING: the speaker is asking what is still open.
   - UNKNOWN: anything else, including unintelligible audio.
2. Put EVERY date/time mentioned into `slots`, in the order spoken. A single
   CREATE_SUGGESTED utterance commonly carries three or more. Never merge them.
3. `raw` must be the speaker's own wording for that date/time, copied from the
   transcript (translate to English only if the transcript is not in Latin script;
   keep the same words otherwise). Downstream code re-derives the actual date from
   `raw`, so getting `raw` right matters more than getting `date` right.
4. Fill `date`/`time` only when you are confident; leave them null otherwise.
   Do NOT do calendar arithmetic you are unsure about — `raw` is the safety net.
5. `title` is the meeting's subject, without filler ("a meeting about", "call with").
   Keep proper nouns exactly as heard.
6. For CONFIRM_SLOT / CANCEL_MEETING / RESCHEDULE, set `meeting_reference` to the
   speaker's own words. Set `meeting_id` ONLY when the reference matches exactly
   one entry in the open-meetings list. If two or more could match, leave
   `meeting_id` null and add an entry to `ambiguities`.
7. `confidence` is your own honest estimate that this extraction is correct.
8. Anything genuinely unclear (missing time, unclear which meeting, two possible
   readings) goes in `ambiguities` as a short human-readable sentence.
9. Never invent a meeting that was not mentioned.
"""


@dataclass(slots=True)
class OpenMeetingSummary:
    meeting_id: int
    title: str
    slot_descriptions: list[str]


def build_user_prompt(
    transcript: str,
    *,
    now_local: datetime,
    timezone_name: str,
    open_meetings: list[OpenMeetingSummary],
    default_duration_minutes: int,
    correction: str | None = None,
    previous_intent_json: str | None = None,
) -> str:
    lines = [
        "## Current time",
        f"{now_local:%A %d %B %Y, %H:%M} ({timezone_name})",
        f"Today is {now_local:%Y-%m-%d}. The speaker's default timezone is {timezone_name}.",
        f"Default meeting duration when unspoken: {default_duration_minutes} minutes.",
        "",
        "## Open proposed meetings (for resolving references)",
    ]
    if open_meetings:
        for meeting in open_meetings:
            slots = "; ".join(meeting.slot_descriptions) or "no live slots"
            lines.append(f"- id={meeting.meeting_id} | {meeting.title} | {slots}")
    else:
        lines.append("- (none)")

    lines += ["", "## Transcript", transcript.strip()]

    if correction:
        lines += [
            "",
            "## Owner's correction",
            "The owner reviewed a previous extraction of this same transcript and "
            "replied with the correction below. The correction wins wherever it "
            "conflicts with the transcript.",
            correction.strip(),
        ]
    if previous_intent_json:
        lines += ["", "## Previous extraction (being corrected)", previous_intent_json]

    return "\n".join(lines)
