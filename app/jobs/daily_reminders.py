"""Post a reminder to the group when a saved date is approaching.

    python -m app.jobs.daily_reminders
    python -m app.jobs.daily_reminders --dry-run
    python -m app.jobs.daily_reminders --today 2026-12-24   # rehearse a day

Announcements happen on milestones (30, 14, 7, 3, 1 and 0 days out) rather than
every day inside a window, so a date is mentioned a handful of times as it
approaches instead of every morning for a month, which is how a reminder ends
up muted.

Run from GitHub Actions when deployed: Render's free plan sleeps, and a
sleeping service fires no timers, so an in-process schedule would simply never
reach the day it was waiting for.
"""

import argparse
import asyncio
import logging
from datetime import date

from telegram import Bot

from app.config import load_settings
from app.db.database import init_db, list_dates
from app.services.reminders import due_today

logger = logging.getLogger(__name__)


def compose(items) -> str:
    """The message body. Kept separate so it can be checked without sending."""
    today = [u for u in items if u.is_today]
    soon = [u for u in items if not u.is_today]

    lines: list[str] = []
    if today:
        lines.append("Today:")
        lines += [f"  {u.describe()}" for u in today]
    if soon:
        if lines:
            lines.append("")
        lines.append("Coming up:")
        lines += [f"  {u.describe()}" for u in soon]
    return "\n".join(lines)


async def send_reminders(settings, today: date, dry_run: bool = False) -> int:
    rows = list_dates(settings.db_path)
    items = due_today(rows, today)
    logger.info(
        "%d date(s) stored, %d at a reminder milestone for %s",
        len(rows),
        len(items),
        today.isoformat(),
    )
    if not items:
        return 0

    body = compose(items)
    logger.info("reminder body:\n%s", body)
    if dry_run:
        logger.info("[dry-run] not sending")
        return len(items)

    bot = Bot(settings.bot_token)
    try:
        async with bot:
            await bot.send_message(chat_id=settings.chat_id, text=body)
    except Exception:
        # A failed send must not mark the run successful; the job exits non-zero
        # so a scheduled run shows up as failed rather than silently missing.
        logger.exception("could not send reminder to chat %s", settings.chat_id)
        raise
    logger.info("sent reminder for %d date(s)", len(items))
    return len(items)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="compose but do not send")
    parser.add_argument(
        "--today",
        default=None,
        help="pretend it is this date (YYYY-MM-DD), for rehearsing a reminder",
    )
    args = parser.parse_args()

    from app.bot import setup_logging

    setup_logging()
    settings = load_settings()
    init_db(settings.db_path)

    today = date.fromisoformat(args.today) if args.today else date.today()
    asyncio.run(send_reminders(settings, today, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
