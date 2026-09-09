# Runbook

One page. Everything you need at 7am when the calendar looks wrong.

Assumes `railway link` is done and you can run `railway run <cmd>` against the
`api` service (which gives you `DATABASE_URL` and the rest of the environment).

---

## 1. Is it alive?

```bash
curl https://<domain>/healthz     # {"status":"ok",...} — the process is up
curl https://<domain>/readyz      # also checks Postgres
```

Logs are JSON, one object per line. Every voice command carries a
`correlation_id` that follows it from webhook to calendar write:

```bash
railway logs --service api | jq -c 'select(.correlation_id=="a1b2c3d4")'
railway logs --service api | jq -c 'select(.level=="ERROR")'
```

Useful message names: `telegram_update_received`, `transcribed`, `card_sent`,
`plan_applied`, `sibling_delete_failed`, `slot_removed_externally`,
`watch_channel_registered`, `reconcile_job_finished`.

---

## 2. Re-authorise Google

Symptom: the bot replies "Google access has expired", or logs show
`GoogleReauthRequired` / `invalid_grant`. Causes: the password was changed, the
app was removed at myaccount.google.com/permissions, or the OAuth client is
still in "Testing" mode (those refresh tokens expire after 7 days — publish the
consent screen).

1. Message the bot `/connect`.
2. Open the link, choose the right Google account, accept.
3. You should get "✅ Google Calendar connected".

The new refresh token overwrites the old one and the push channel is
re-registered. Nothing else needs restarting.

If the link errors with **"No refresh token issued"**: Google only returns one
on a fresh grant. Remove the app at
<https://myaccount.google.com/permissions>, then `/connect` again.

---

## 3. A meeting is stuck

### Yellow placeholders that should be gone

Usually a Calendar API failure during the collapse. Those slots are recorded as
`failed`, never dropped, so nothing is orphaned.

```
/cleanup      ← in Telegram: retries every failed deletion now
```

The nightly job retries them anyway. To see what is stuck:

```sql
SELECT s.id, s.gcal_event_id, m.title, m.status
FROM slots s JOIN meetings m ON m.id = s.meeting_id
WHERE s.state = 'failed';
```

### A meeting stuck in `proposed` that should be confirmed

```sql
-- What does the group look like?
SELECT m.id, m.title, m.status, s.id AS slot, s.state, s.start_utc, s.gcal_event_id
FROM meetings m JOIN slots s ON s.meeting_id = m.id
WHERE m.id = <meeting_id> ORDER BY s.start_utc;
```

Prefer fixing it through the bot ("Evocabank is confirmed for Wednesday 10") so
the calendar and database move together. Only if that is impossible:

```sql
BEGIN;
UPDATE slots SET state = 'fixed' WHERE id = <winning_slot_id>;
UPDATE slots SET state = 'removed', removed_at = now()
  WHERE meeting_id = <meeting_id> AND id <> <winning_slot_id> AND state IN ('suggested','fixed');
UPDATE meetings SET status = 'confirmed', confirmed_at = now() WHERE id = <meeting_id>;
COMMIT;
```

Then fix the colours in Google Calendar by hand, or run the reconcile job.

### Cards that will not go away

```
/cancel       ← discards every pending confirmation card
```

---

## 4. Purge orphaned calendar events

An "orphan" is an event this app created that no longer has a live database
row. The design makes these rare; the nightly job is the normal cure.

```bash
railway run python -m app.jobs.reconcile
```

To find them by hand, filter Google Calendar's search by
`extendedProperties.private.app = voice-calendar` — every event this app writes
carries that tag plus its `meeting_id`. Placeholders are also titled
`[TENTATIVE] …`, so in the Calendar UI you can search `[TENTATIVE]` and delete
what is left over. Deleting an event in the UI is safe: the next reconcile marks
the slot `removed`.

---

## 5. Rotate the bot token

1. @BotFather → `/revoke` → new token.
2. Set `TELEGRAM_BOT_TOKEN` in Railway (the service redeploys).
3. Re-register the webhook — a new token drops the old registration:

```bash
railway run python -m app.jobs.setup_webhook set
railway run python -m app.jobs.setup_webhook info
```

To rotate `TELEGRAM_WEBHOOK_SECRET`, change the variable and run `set` again;
until you do, every inbound update is refused with 403 (which is the safe
direction).

---

## 6. Rotate `FERNET_KEY`

**Rotating the key makes every stored refresh token unreadable.** With one user
that is fine — it just means re-authorising:

```bash
railway run python -m app.jobs.setup_webhook fernet   # new key
# set FERNET_KEY in Railway, then:
railway run python -c "
from app.db import session_scope
from app.models import User
with session_scope() as s:
    for u in s.query(User): u.google_refresh_token_encrypted = None
"
```

Then `/connect` in Telegram. With several users, decrypt-and-re-encrypt with
both keys in a migration script instead.

---

## 7. Google push notifications stopped

Channels last about 7 days and the nightly job renews anything expiring within
48 hours. Drift is still caught by the nightly full sweep, so this is not an
emergency.

```sql
SELECT user_id, gcal_channel_id, channel_expiry, last_reconciled_at FROM sync_state;
```

Force a new channel:

```bash
railway run python -c "
from app.db import session_scope
from app.models import User
from app.services.google_account import calendar_for
from app.services.reconcile import ensure_watch_channel
with session_scope() as s:
    for u in s.query(User):
        with calendar_for(u) as c:
            print(u.id, ensure_watch_channel(s, u, c, force=True))
"
```

The push address must be the custom domain, exactly as `PUBLIC_BASE_URL` — a
preview URL will register and then silently stop being reachable.

If reconciliation logs `sync_token_expired_full_resync`, that is normal: the
token aged out and the next pass was a full sweep.

---

## 8. Backups and restore

Enable automatic backups on the Railway Postgres plugin (daily, 7-day
retention is plenty for one user).

```bash
railway run pg_dump "$DATABASE_URL" -Fc -f backup-$(date +%F).dump   # manual
railway run pg_restore -d "$DATABASE_URL" --clean --if-exists backup.dump
```

Restoring the database does **not** restore the calendar. After a restore, run
`python -m app.jobs.reconcile` so the database catches up to whatever Google
actually holds.

---

## 9. Deploying

Migrations run as a pre-deploy step. If a deploy fails there, the old version
keeps serving — fix the migration and redeploy.

```bash
railway run alembic current
railway run alembic history
railway run alembic downgrade -1     # last resort; check the downgrade first
```

Always deploy to staging first, especially for anything touching the collapse.

---

## 10. Quick reference

| Symptom | First move |
|---|---|
| Bot silent | `/healthz`, then `setup_webhook info` |
| "Not on the allow-list" | add the id to `TELEGRAM_ALLOWED_USER_IDS` |
| "Google access has expired" | `/connect` (§2) |
| Yellow events left over | `/cleanup` (§3) |
| Calendar and bot disagree | `python -m app.jobs.reconcile` |
| Wrong time interpreted | check the zone on the card; `/tz <IANA>` |
| Transcription wrong | tap ✏️ and correct it in text |
| Too many commands refused | raise `COMMANDS_PER_HOUR` |
