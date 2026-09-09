"""One-shot operational commands.

    python -m app.jobs.setup_webhook set      # point Telegram at this deployment
    python -m app.jobs.setup_webhook delete   # unhook (e.g. before local testing)
    python -m app.jobs.setup_webhook info     # what Telegram currently thinks
    python -m app.jobs.setup_webhook fernet   # print a fresh FERNET_KEY
"""

from __future__ import annotations

import json
import sys

from app.config import get_settings
from app.crypto import generate_key
from app.integrations.telegram import TelegramClient

COMMANDS = [
    ("pending", "List meetings still waiting on a decision"),
    ("connect", "Link your Google Calendar"),
    ("tz", "Set the timezone spoken times are read in"),
    ("cleanup", "Retry failed placeholder deletions"),
    ("cancel", "Discard any pending confirmation cards"),
    ("help", "How to talk to this bot"),
]


def main(argv: list[str]) -> int:
    action = argv[1] if len(argv) > 1 else "info"

    if action == "fernet":
        print(generate_key())
        return 0

    settings = get_settings()
    with TelegramClient() as telegram:
        if action == "set":
            url = f"{settings.public_base_url}/webhooks/telegram"
            result = telegram.set_webhook(url, settings.telegram_webhook_secret)
            telegram.set_my_commands(COMMANDS)
            print(f"Webhook set to {url}: {result}")
        elif action == "delete":
            print(telegram.delete_webhook())
        elif action == "info":
            print(json.dumps({"bot": telegram.get_me()}, indent=2))
        else:
            print(__doc__)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
