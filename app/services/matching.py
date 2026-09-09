"""Meeting resolution for confirmations, cancellations and reschedules (T-19).

Order of evidence:
  1. an explicit meeting id the extraction was confident about
  2. fuzzy title match against open meetings
  3. an exact time match against exactly one slot of exactly one meeting

If more than one meeting survives, we ask. We never guess.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.models import Meeting, Slot, SlotState

#: Words that carry no identifying signal in a meeting title.
STOPWORDS = frozenset(
    {
        "meeting", "call", "sync", "session", "chat", "the", "a", "an", "with",
        "and", "for", "to", "about", "our", "my", "at", "on",
        "besprechung", "termin", "gespräch", "gesprach", "anruf",
        "встреча", "звонок", "созвон", "совещание",
        "հանդիպում", "զանգ",
    }
)

TITLE_MATCH_THRESHOLD = 0.62
TIME_MATCH_TOLERANCE = timedelta(minutes=1)


def normalize_title(title: str) -> str:
    """Case-, accent- and punctuation-insensitive form used for matching."""
    decomposed = unicodedata.normalize("NFKD", title or "")
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    lowered = stripped.casefold()
    cleaned = re.sub(r"[^\w\s]+", " ", lowered, flags=re.UNICODE)
    return re.sub(r"\s+", " ", cleaned).strip()


def _tokens(text: str) -> list[str]:
    return [t for t in normalize_title(text).split() if t and t not in STOPWORDS]


def title_similarity(reference: str, candidate: str) -> float:
    """0..1 similarity, biased towards distinctive tokens (company names)."""
    ref_tokens, cand_tokens = _tokens(reference), _tokens(candidate)
    if not ref_tokens or not cand_tokens:
        return 0.0

    ref_set, cand_set = set(ref_tokens), set(cand_tokens)
    shared = ref_set & cand_set
    overlap = len(shared) / len(ref_set | cand_set) if shared else 0.0

    # Catch transcription drift ("Evocabank" vs "Evoca bank").
    best_partial = 0.0
    for ref in ref_tokens:
        for cand in cand_tokens:
            if ref == cand:
                best_partial = max(best_partial, 1.0)
            elif len(ref) >= 4 and len(cand) >= 4 and (ref in cand or cand in ref):
                best_partial = max(best_partial, 0.9)
            else:
                best_partial = max(best_partial, _ratio(ref, cand))

    joined_ref, joined_cand = "".join(ref_tokens), "".join(cand_tokens)
    joined = _ratio(joined_ref, joined_cand)
    return max(overlap, best_partial * 0.95, joined)


def _ratio(a: str, b: str) -> float:
    from difflib import SequenceMatcher

    return SequenceMatcher(None, a, b).ratio()


@dataclass(slots=True)
class MeetingCandidate:
    meeting: Meeting
    score: float
    matched_slot: Slot | None = None
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Resolution:
    """Either a single confident match, or the shortlist to ask about."""

    meeting: Meeting | None
    matched_slot: Slot | None
    candidates: list[MeetingCandidate]
    question: str | None = None

    @property
    def is_resolved(self) -> bool:
        return self.meeting is not None


def resolve_meeting(
    meetings: list[Meeting],
    *,
    reference: str | None,
    explicit_meeting_id: int | None = None,
    spoken_starts: list[datetime] | None = None,
) -> Resolution:
    if not meetings:
        return Resolution(None, None, [], question="You have no open meetings right now.")

    by_id = {m.id: m for m in meetings}
    if explicit_meeting_id is not None and explicit_meeting_id in by_id:
        meeting = by_id[explicit_meeting_id]
        return Resolution(meeting, _slot_at(meeting, spoken_starts), [])

    spoken_starts = spoken_starts or []
    candidates: list[MeetingCandidate] = []

    for meeting in meetings:
        score = 0.0
        reasons: list[str] = []
        if reference:
            score = title_similarity(reference, meeting.title)
            if score >= TITLE_MATCH_THRESHOLD:
                reasons.append(f"title matches “{meeting.title}”")
        matched_slot = _slot_at(meeting, spoken_starts)
        if matched_slot is not None:
            score = max(score, 0.55) + 0.35
            reasons.append("one of its proposed slots is exactly that time")
        if score > 0:
            candidates.append(MeetingCandidate(meeting, min(score, 1.0), matched_slot, reasons))

    candidates.sort(key=lambda c: c.score, reverse=True)
    strong = [c for c in candidates if c.score >= TITLE_MATCH_THRESHOLD]

    if not strong:
        # No title signal at all: a unique time match is still decisive.
        timed = [c for c in candidates if c.matched_slot is not None]
        if len(timed) == 1:
            return Resolution(timed[0].meeting, timed[0].matched_slot, candidates)
        if not candidates:
            return Resolution(
                None,
                None,
                [MeetingCandidate(m, 0.0) for m in meetings],
                question="I could not tell which meeting you meant.",
            )
        return Resolution(
            None, None, candidates, question="I could not tell which meeting you meant."
        )

    if len(strong) == 1:
        return Resolution(strong[0].meeting, strong[0].matched_slot, candidates)

    # Several plausible titles — a unique exact time match breaks the tie.
    timed = [c for c in strong if c.matched_slot is not None]
    if len(timed) == 1:
        return Resolution(timed[0].meeting, timed[0].matched_slot, candidates)

    # A clear winner by margin is still allowed; a close call is not.
    if strong[0].score - strong[1].score >= 0.25:
        return Resolution(strong[0].meeting, strong[0].matched_slot, candidates)

    return Resolution(
        None,
        None,
        strong,
        question="More than one open meeting matches that. Which one did you mean?",
    )


def _slot_at(meeting: Meeting, spoken_starts: list[datetime] | None) -> Slot | None:
    if not spoken_starts:
        return None
    for slot in meeting.slots:
        if slot.state not in (SlotState.suggested, SlotState.fixed):
            continue
        for start in spoken_starts:
            if abs(slot.start_utc - start) <= TIME_MATCH_TOLERANCE:
                return slot
    return None
