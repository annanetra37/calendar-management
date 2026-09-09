# Voice-to-Calendar Scheduling

Send a voice note; get the slot in Google Calendar.

**Fixed** meetings become **green** events. **Suggested** meetings become
**yellow** placeholders — several per meeting. When one is agreed, a second
voice note confirms it: every sibling placeholder disappears and exactly one
green event remains.

The idea the code is built around: **a meeting is a logical group, not a
calendar event.** One `meetings` row owns 1..N `slots` rows, each of which is
one calendar event. Confirming collapses N → 1.

```
🎙 "Suggest Evocabank meeting Tuesday 2pm, Wednesday 10, Thursday 4"
   → 1 meeting, 3 yellow placeholders

🎙 "Evocabank is confirmed for Wednesday 10"
   → 1 green event, 0 yellow
```

---

## How it works

```
Voice note (Telegram)
        │
        ▼
POST /webhooks/telegram ──► FastAPI (Railway)
        │
        ├─ 1. download audio (.ogg/opus), size- and duration-capped
        ├─ 2. transcribe                → Whisper (auto language detection)
        ├─ 3. extract intent            → Claude / GPT, strict JSON schema
        ├─ 4. re-derive dates and times → deterministic parser, not the LLM
        ├─ 5. reply with a CONFIRMATION CARD (✅ / ✏️ / ❌)
        │       └─ only ✅ writes to Google Calendar
        └─ 6. persist to Postgres (meetings, slots, voice_commands)

POST /webhooks/google  ──► incremental sync: drift from the calendar UI
Railway Cron           ──► nightly reconcile, expiry, channel renewal, purge
```

**Nothing reaches the calendar before you have read a card and tapped ✅.**
That single rule is what makes the thing trustworthy enough to use daily.

### Stack

| Layer | Choice |
|---|---|
| Runtime | Python 3.12, FastAPI, Uvicorn |
| Database | PostgreSQL, SQLAlchemy 2, Alembic |
| Bot | Telegram Bot API, **webhook mode** (polling would burn an instance) |
| Speech | OpenAI Whisper (`whisper-1`) |
| Intent | Claude (default) or GPT, tool-use / structured output |
| Calendar | Google Calendar REST v3 over `httpx` |
| Queue | FastAPI `BackgroundTasks` — **no Redis** |

Two deliberate deviations from the original spec, both to make the code
testable and the deployment smaller:

- **Google APIs are called over `httpx`, not `google-api-python-client`.** The
  discovery-based client is hard to unit-test for retry and error paths, and it
  fetches a discovery document at runtime. `app/integrations/google_calendar.py`
  covers exactly the endpoints used and has full retry tests.
- **The Telegram Bot API is called directly, not through `python-telegram-bot`.**
  We use webhooks, two update types and eight methods; the library's value here
  was mostly its polling loop, which we do not use.

---

## The intents

| Intent | Example | Effect |
|---|---|---|
| `CREATE_FIXED` | "Board call, Tuesday 15th, 3pm, fixed" | one green event |
| `CREATE_SUGGESTED` | "Suggest Evocabank Tuesday 2pm, Wednesday 10, Thursday 4" | N yellow placeholders in one group |
| `CONFIRM_SLOT` | "Evocabank is confirmed for Wednesday 10" | collapse N → 1 green |
| `CANCEL_MEETING` | "Cancel the Evocabank meeting entirely" | delete the whole group |
| `RESCHEDULE` | "Move the board call to Thursday 4pm" | move the confirmed event |
| `LIST_PENDING` | "What's still open?" | list open proposals |

The extraction prompt always receives the current time in your timezone and
the list of currently-open proposals, so "the Evocabank one" can be resolved.

### The LLM does not do calendar arithmetic

`app/nlu/timeparse.py` re-derives every date and time from the *spoken phrase*
and compares that against what the model returned. **The phrase wins**, and any
disagreement is printed on the card. It handles `next Tuesday` (the following
week, not tomorrow), `the 15th`, `quarter past three`, British `half three`
(15:30) and German `halb drei` (14:30) — a distinction that would otherwise put
a meeting an hour late.

Bare hours use a business-hours reading (`Thursday 4` → 16:00) and say so on the
card. Past times are refused, not silently rolled forward.

### Timezones

Everything is stored in UTC. Spoken times are read in your home zone
(`Asia/Yerevan` by default) unless the utterance names one ("3pm Vienna time").
While travelling, `/tz Europe/Vienna` switches the interpretation zone; every
card names the zone it used.

---

## Local development

Requires Python 3.12 and a PostgreSQL you can reach.

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env
python -m app.jobs.setup_webhook fernet   # paste into FERNET_KEY
$EDITOR .env

alembic upgrade head
uvicorn app.main:app --reload --port 8000
curl localhost:8000/healthz
```

```bash
pytest -q          # 197 tests, no network, no API keys needed
ruff check .
pre-commit install
```

To exercise the bot locally, expose port 8000 over HTTPS (ngrok, Cloudflare
Tunnel), set `PUBLIC_BASE_URL` to that origin, then:

```bash
python -m app.jobs.setup_webhook set
```

### Layout

```
app/
  config.py               settings; fails fast on a missing variable
  models.py               users, meetings, slots, voice_commands, sync_state
  crypto.py               Fernet encryption for the Google refresh token
  logging_setup.py        JSON logs with a per-command correlation id
  api/                    HTTP surface: health, telegram, google auth + push
  bot/handlers.py         the voice → card → button → calendar loop
  integrations/           Telegram, Google OAuth, Google Calendar clients
  nlu/                    transcription, prompt, extraction, time resolution
  services/
    planning.py           intent → Plan (read-only; this is what the card shows)
    meetings.py           Plan → calendar + database (creation, THE COLLAPSE)
    matching.py           which meeting did they mean? (asks when unsure)
    reconcile.py          drift detection, expiry, push channels
    cards.py              card and list rendering
  jobs/reconcile.py       the nightly cron entrypoint
```

---

## Deployment (Railway)

Two services in one project, plus the Postgres plugin:

| Service | Config | Command |
|---|---|---|
| `api` | `railway.json` | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
| `reconcile` | `railway.cron.json` | `python -m app.jobs.reconcile`, cron `15 2 * * *` |

- Migrations run as a **pre-deploy step** (`alembic upgrade head`), never in the
  app's startup handler — two booting instances would race.
- Healthcheck path `/healthz` (liveness, touches nothing). `/readyz` also checks
  the database.
- **A custom domain is required.** Both the Google OAuth redirect URI and the
  push-notification address must be registered against a fixed HTTPS URL; a
  rotating `*.up.railway.app` preview URL breaks OAuth on every redeploy.
- Staging is a second Railway environment with its own database, its own test
  bot token and a test Google Calendar. Never exercise collapse logic against
  the live calendar.

Environment variables are listed in `.env.example`. The ones without defaults
are required and the app refuses to start without them.

### First run

1. Create the bot with @BotFather; put the token in `TELEGRAM_BOT_TOKEN`.
2. Message the bot `/whoami`, put the id in `TELEGRAM_ALLOWED_USER_IDS`.
   **An empty allow-list means nobody** — the bot fails closed.
3. Google Cloud console: create an OAuth client, add
   `https://<domain>/auth/google/callback` as an authorised redirect URI, and
   enable the Google Calendar API.
4. `python -m app.jobs.setup_webhook set`
5. Message the bot `/connect` and complete the Google consent screen.
6. Send a voice note.

---

## Security and privacy

- The Google refresh token is Fernet-encrypted at rest; only
  `calendar.events` is requested, never full `calendar` scope.
- `X-Telegram-Bot-Api-Secret-Token` is compared in constant time on every
  request; unknown user ids are refused before any processing.
- Google push notifications must present the configured channel token.
- Transcripts are business-sensitive and are **not** logged at INFO in
  production (`LOG_TRANSCRIPTS` stays false).
- Voice audio is kept for `AUDIO_RETENTION_DAYS` (30 by default) so a bad
  transcription can be investigated, then purged by the nightly job.
- Per-user hourly command cap, audio size and duration caps.

## Operating it

`RUNBOOK.md` covers re-authorising Google, unsticking a meeting, purging
orphaned events, rotating the bot token and reading the logs.

## Not in v1

Free/busy negotiation, proposal emails to counterparties, Outlook or Apple
Calendar, a web dashboard, multi-calendar routing, recurring meetings. The
schema takes each of them without a rewrite; `users` is already multi-tenant.
