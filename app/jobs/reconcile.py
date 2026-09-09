"""Daily reconciliation and cleanup job (T-24, T-34).

Railway cron services run as one-shot containers, so this is a standalone
entrypoint: ``python -m app.jobs.reconcile``.

It does four things per user:
  1. full reconciliation sweep (catches anything push notifications missed)
  2. expires placeholders whose time has passed
  3. renews Google watch channels expiring within 48 hours
  4. purges audio blobs past the retention window

It also retries any placeholder deletion that previously failed, and expires
confirmation cards left unanswered (T-17).
"""

from __future__ import annotations

import logging
import sys

from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.integrations.google_oauth import GoogleReauthRequired
from app.integrations.telegram import TelegramClient, TelegramError
from app.logging_setup import configure_logging, correlation_scope
from app.models import CommandStatus, User, VoiceCommand, utcnow
from app.services import audio as audio_service
from app.services.google_account import calendar_for
from app.services.meetings import retry_failed_deletions
from app.services.reconcile import (
    ReconcileReport,
    ensure_watch_channel,
    expire_past_placeholders,
    reconcile_user,
)

log = logging.getLogger(__name__)


def expire_stale_cards(session) -> int:
    """T-17 — a card nobody answered within the TTL must not stay tappable."""
    ttl_cutoff = utcnow()
    stmt = select(VoiceCommand).where(
        VoiceCommand.status == CommandStatus.pending_confirmation,
        VoiceCommand.expires_at.is_not(None),
        VoiceCommand.expires_at < ttl_cutoff,
    )
    count = 0
    for command in session.scalars(stmt):
        command.status = CommandStatus.rejected
        command.error_text = "expired without an answer"
        command.awaiting_edit = False
        count += 1
    return count


def run_for_user(user_id: int) -> ReconcileReport:
    report = ReconcileReport()
    with session_scope() as session:
        user = session.get(User, user_id)
        if user is None or not user.is_google_connected:
            return report
        chat_id = user.telegram_user_id
        try:
            with calendar_for(user) as calendar:
                report.merge(reconcile_user(session, user, calendar, force_full=True))
                report.merge(expire_past_placeholders(session, user, calendar))
                cleared, failing = retry_failed_deletions(session, user, calendar)
                report.cleanup_cleared += cleared
                report.cleanup_failing += failing
                if ensure_watch_channel(session, user, calendar):
                    log.info("watch_channel_renewed", extra={"user_id": user_id})
        except GoogleReauthRequired:
            report.errors.append("Google access has expired — send /connect to re-authorise.")
        except Exception as exc:
            log.exception("reconcile_user_failed", extra={"user_id": user_id})
            report.errors.append(f"Reconciliation failed: {exc}")

    _notify(chat_id, report)
    return report


def _notify(chat_id: int, report: ReconcileReport) -> None:
    if report.is_quiet and not report.notifications:
        return
    lines = list(report.notifications)
    summary = []
    if report.placeholders_expired:
        summary.append(f"{report.placeholders_expired} past placeholder(s) removed")
    if report.slots_removed:
        summary.append(f"{report.slots_removed} slot(s) removed outside the bot")
    if report.slots_moved:
        summary.append(f"{report.slots_moved} slot(s) moved in the calendar")
    if report.cleanup_cleared:
        summary.append(f"{report.cleanup_cleared} leftover placeholder(s) cleaned up")
    if report.cleanup_failing:
        summary.append(f"⚠️ {report.cleanup_failing} deletion(s) still failing")
    if summary:
        lines.append("🧾 Overnight tidy-up: " + "; ".join(summary) + ".")
    lines += [f"⚠️ {error}" for error in report.errors]

    if not lines:
        return
    try:
        with TelegramClient() as telegram:
            telegram.send_message(chat_id, "\n\n".join(lines), disable_notification=True)
    except TelegramError:
        log.warning("cron_notification_failed", extra={"chat_id": chat_id})


def main() -> int:
    settings = get_settings()
    configure_logging(
        level=settings.log_level, sentry_dsn=settings.sentry_dsn, environment=settings.environment
    )
    with correlation_scope() as cid:
        log.info("reconcile_job_started", extra={"correlation_id": cid})

        with session_scope() as session:
            expired_cards = expire_stale_cards(session)
            purged = audio_service.purge_expired(session)
            user_ids = list(session.scalars(select(User.id)))

        total = ReconcileReport()
        for user_id in user_ids:
            total.merge(run_for_user(user_id))

        log.info(
            "reconcile_job_finished",
            extra={
                "users": len(user_ids),
                "expired_cards": expired_cards,
                "audio_purged": purged,
                "slots_removed": total.slots_removed,
                "slots_moved": total.slots_moved,
                "meetings_expired": total.meetings_expired,
                "placeholders_expired": total.placeholders_expired,
                "cleanup_cleared": total.cleanup_cleared,
                "cleanup_failing": total.cleanup_failing,
                "errors": len(total.errors),
            },
        )
        return 1 if total.errors else 0


if __name__ == "__main__":
    sys.exit(main())
