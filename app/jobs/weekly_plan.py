"""Build a date plan and post it to the group.

    python -m app.jobs.weekly_plan
    python -m app.jobs.weekly_plan --dry-run     # build and print, do not post
    python -m app.jobs.weekly_plan --no-store    # do not file it

The same `plan_date()` the /plan command uses, so the scheduled result cannot
drift from what the manual trigger produces. Intended for a Friday schedule
once the output has been judged worth receiving unprompted.
"""

import argparse
import asyncio
import logging
from datetime import date

from telegram import Bot

from app.config import load_settings
from app.db.database import init_db, list_links, save_plan
from app.handlers.plan_handler import next_saturday
from app.services.planner import plan_date

logger = logging.getLogger(__name__)


async def run(settings, today: date, dry_run: bool = False, store: bool = True) -> bool:
    rows = list_links(settings.db_path)
    plan = plan_date(
        rows, settings.gemini_api_key, settings.gemini_model, today=today
    )
    if not plan.ok:
        # Nothing plannable is a normal state, not a failure: it usually means
        # every saved link is done, expired, or has no coordinates.
        logger.info("no plan to post: %s", plan.error)
        return False

    saturday = next_saturday(today)
    body = plan.render()
    message = f"Plan for Saturday {saturday.isoformat()}:\n{body}"
    logger.info("plan for %s:\n%s", saturday, body)

    if dry_run:
        logger.info("[dry-run] not posting")
        return True

    bot = Bot(settings.bot_token)
    async with bot:
        await bot.send_message(
            chat_id=settings.chat_id, text=message, disable_web_page_preview=True
        )
    logger.info("posted plan to chat %s", settings.chat_id)

    if store:
        save_plan(settings.db_path, saturday.isoformat(), body)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="build but do not post")
    parser.add_argument("--no-store", action="store_true", help="do not file the plan")
    parser.add_argument("--today", default=None, help="pretend it is this date")
    args = parser.parse_args()

    from app.bot import setup_logging

    setup_logging()
    settings = load_settings()
    init_db(settings.db_path)
    today = date.fromisoformat(args.today) if args.today else date.today()
    asyncio.run(run(settings, today, dry_run=args.dry_run, store=not args.no_store))


if __name__ == "__main__":
    main()
