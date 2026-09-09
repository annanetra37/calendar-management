"""Rendering of the confirmation card and other bot replies (T-15, T-22, T-27).

The card is the trust boundary: nothing reaches the calendar until the owner
has read this and tapped ✅.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.integrations.telegram import InlineButton
from app.models import Meeting, MeetingStatus, SlotState
from app.services.planning import Plan

CONFIRM = "✅ Confirm"
EDIT = "✏️ Edit"
DISCARD = "❌ Discard"

ACTION_HEADERS: dict[str, str] = {
    "create_suggested": "🟡 SUGGESTED",
    "create_fixed": "🟢 FIXED",
    "confirm": "🟢 CONFIRM",
    "cancel": "🗑 CANCEL",
    "reschedule": "🔁 RESCHEDULE",
}


def render_card(plan: Plan) -> str:
    """The confirmation card body (HTML parse mode)."""
    title = html.escape(plan.title or "(untitled)")
    lines = [f"📋 <b>{title}</b>"]

    if plan.action == "create_suggested":
        count = len(plan.slots)
        lines.append(f"{ACTION_HEADERS[plan.action]} — {count} slot{'s' if count != 1 else ''}")
        lines += [f"   • {_slot_line(s)}" for s in plan.slots]
        lines.append("")
        lines.append("<i>Placeholders are private, shown as free, and invite nobody.</i>")

    elif plan.action == "create_fixed":
        lines.append(f"{ACTION_HEADERS[plan.action]}")
        lines += [f"   • {_slot_line(s)}" for s in plan.slots]
        if plan.attendees:
            lines.append(f"   👥 {html.escape(', '.join(plan.attendees))}")

    elif plan.action == "confirm":
        lines.append(f"{ACTION_HEADERS[plan.action]}")
        lines.append(f"   • {_slot_line(plan.slots[0])}")
        if plan.sibling_slot_count:
            lines.append(
                f"   🗑 removes {plan.sibling_slot_count} other placeholder"
                f"{'s' if plan.sibling_slot_count != 1 else ''}"
            )
        if plan.attendees:
            lines.append(f"   👥 invites {html.escape(', '.join(plan.attendees))}")

    elif plan.action == "cancel":
        lines.append(f"{ACTION_HEADERS[plan.action]}")
        lines.append(
            f"   🗑 removes {plan.sibling_slot_count} event"
            f"{'s' if plan.sibling_slot_count != 1 else ''}"
        )
        lines += [f"   • {_slot_line(s)}" for s in plan.slots[:6]]

    elif plan.action == "reschedule":
        lines.append(f"{ACTION_HEADERS[plan.action]}")
        lines.append(f"   → {_slot_line(plan.slots[0])}")
        if plan.attendees:
            lines.append(f"   👥 notifies {html.escape(', '.join(plan.attendees))}")

    if plan.duration_minutes and plan.action != "cancel":
        lines.append(f"   ⏱ {plan.duration_minutes} minutes")

    if plan.warnings:
        lines.append("")
        lines += [f"⚠️ {html.escape(w)}" for w in dict.fromkeys(plan.warnings)]

    return "\n".join(lines)


def card_buttons(command_id: int) -> list[list[InlineButton]]:
    return [
        [
            InlineButton(CONFIRM, f"cmd:{command_id}:confirm"),
            InlineButton(EDIT, f"cmd:{command_id}:edit"),
            InlineButton(DISCARD, f"cmd:{command_id}:discard"),
        ]
    ]


def clarification_buttons(command_id: int, plan: Plan) -> list[list[InlineButton]]:
    rows = [
        [InlineButton(f"{index}. {_short(c.title)}", f"pick:{command_id}:{c.meeting_id}")]
        for index, c in enumerate(plan.candidates, start=1)
    ]
    rows.append([InlineButton(DISCARD, f"cmd:{command_id}:discard")])
    return rows


def render_clarification(plan: Plan) -> str:
    lines = [f"❓ {html.escape(plan.question or 'Which meeting did you mean?')}", ""]
    for index, candidate in enumerate(plan.candidates, start=1):
        when = ""
        if candidate.next_start_utc:
            local = candidate.next_start_utc.astimezone(ZoneInfo(plan.timezone_name))
            when = f" — next {local:%a %d %b %H:%M}"
        lines.append(
            f"{index}. <b>{html.escape(candidate.title)}</b> "
            f"({candidate.slot_count} slot{'s' if candidate.slot_count != 1 else ''}){when}"
        )
    if not plan.candidates:
        lines.append("<i>Nothing open matches that.</i>")
    lines.append("")
    lines.append("<i>Tap one, or send another voice note.</i>")
    return "\n".join(lines)


def render_pending(meetings: list[Meeting], tz_name: str, now: datetime | None = None) -> str:
    """T-22 — the /pending list."""
    now = now or datetime.now(UTC)
    zone = ZoneInfo(tz_name)
    proposed = [m for m in meetings if m.status is MeetingStatus.proposed]
    if not proposed:
        return "✅ Nothing is waiting on a decision — no open proposals."

    lines = [f"🟡 <b>{len(proposed)} meeting{'s' if len(proposed) != 1 else ''} still open</b>", ""]
    for meeting in sorted(proposed, key=lambda m: m.created_at):
        age_hours = (now - _aware(meeting.created_at)).total_seconds() / 3600
        age = f"{age_hours / 24:.0f}d" if age_hours >= 48 else f"{age_hours:.0f}h"
        live = [s for s in meeting.slots if s.state is SlotState.suggested]
        lines.append(
            f"📋 <b>{html.escape(meeting.title)}</b> — "
            f"{len(live)} slot{'s' if len(live) != 1 else ''}, proposed {age} ago"
        )
        for slot in sorted(live, key=lambda s: s.start_utc):
            local = slot.start_utc.astimezone(zone)
            stale = " ⏰ past" if slot.start_utc < now else ""
            lines.append(f"   • {local:%a %d %b, %H:%M}{stale}")
        failed = [s for s in meeting.slots if s.state is SlotState.failed]
        if failed:
            lines.append(f"   ⚠️ {len(failed)} placeholder(s) pending cleanup")
        lines.append("")
    lines.append(f"<i>Times shown in {tz_name}.</i>")
    return "\n".join(lines).strip()


def _slot_line(slot) -> str:
    local_start = slot.start_utc.astimezone(ZoneInfo(slot.timezone_name))
    local_end = slot.end_utc.astimezone(ZoneInfo(slot.timezone_name))
    zone_label = slot.timezone_name.split("/")[-1].replace("_", " ")
    return (
        f"{local_start:%a %d %b}, {local_start:%H:%M}–{local_end:%H:%M} "
        f"({html.escape(zone_label)})"
    )


def _short(text: str, limit: int = 28) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)
