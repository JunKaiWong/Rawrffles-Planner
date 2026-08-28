"""The /plan command: build a date plan on demand.

Manual trigger for the same `plan_date()` the Friday job will use, so the
output can be judged before anything is scheduled around it.
"""

import asyncio
import logging
from datetime import date, timedelta

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from app.db.database import list_links, save_plan
from app.services.planner import plan_date

logger = logging.getLogger(__name__)


def next_saturday(today: date | None = None) -> date:
    """The Saturday a plan is for. Today, if today is Saturday."""
    today = today or date.today()
    return today + timedelta(days=(5 - today.weekday()) % 7)


async def plan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return

    settings = context.bot_data.get("settings")
    db_path = context.bot_data["db_path"]

    # The model call takes several seconds; say something first.
    try:
        await message.chat.send_action(ChatAction.TYPING)
    except Exception:  # noqa: BLE001 - cosmetic only
        logger.debug("could not send typing action", exc_info=True)

    rows = await asyncio.to_thread(list_links, db_path)
    plan = await asyncio.to_thread(
        lambda: plan_date(
            rows,
            settings.gemini_api_key,
            settings.gemini_model,
            settings=settings,
        )
    )

    if not plan.ok:
        logger.info("plan request produced nothing: %s", plan.error)
        await message.reply_text(
            "Could not build a plan right now.\n"
            f"Reason: {plan.error}\n"
            "Links need coordinates before they can be planned around - "
            "geocoding runs at intake."
        )
        return

    saturday = next_saturday()
    body = plan.render()
    header = f"Plan for Saturday {saturday.isoformat()}:\n"
    await message.reply_text(header + body, disable_web_page_preview=True)

    try:
        await asyncio.to_thread(save_plan, db_path, saturday.isoformat(), body)
    except Exception:
        # The plan has already been delivered; failing to file it is not worth
        # an error message to the user.
        logger.exception("could not store the generated plan")
