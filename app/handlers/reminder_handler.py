"""Bot commands for anniversaries and important dates.

    /dates                              list what is coming up
    /adddate 2027-03-14 Concert         a one-off
    /adddate yearly 2020-03-14 Together an anniversary, repeating every year
    /deldate 3                          remove one

The `yearly` keyword may appear before or after the date, because there is no
reason to make someone remember which. Everything after those two is the label,
so it can contain spaces without quoting.
"""

import logging
from datetime import date

from telegram import Update
from telegram.ext import ContextTypes

from app.db.database import add_date, delete_date, list_dates
from app.services.reminders import upcoming

logger = logging.getLogger(__name__)

RECURRING_WORDS = {"yearly", "annual", "annually", "recurring", "every-year"}

USAGE = (
    "Add a date:\n"
    "  /adddate 2026-12-25 Christmas market\n"
    "  /adddate yearly 2020-03-14 Our anniversary\n"
    "\n"
    "Dates are YYYY-MM-DD. Add 'yearly' for something that repeats.\n"
    "See them with /dates, remove one with /deldate <id>."
)


def _parse_add_arguments(args: list[str]) -> tuple[str, str, bool] | None:
    """Return (iso_date, label, recurring), or None if it cannot be read."""
    recurring = False
    remaining = []
    for token in args:
        if token.lower() in RECURRING_WORDS and not recurring:
            recurring = True
        else:
            remaining.append(token)

    if len(remaining) < 2:
        return None

    when, label = remaining[0], " ".join(remaining[1:]).strip()
    try:
        parsed = date.fromisoformat(when)
    except ValueError:
        return None
    if not label:
        return None
    return parsed.isoformat(), label, recurring


async def add_date_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    db_path = context.bot_data["db_path"]

    parsed = _parse_add_arguments(context.args or [])
    if parsed is None:
        await message.reply_text("I could not read that.\n\n" + USAGE)
        return

    when, label, recurring = parsed
    stored = date.fromisoformat(when)
    if not recurring and stored < date.today():
        # A past one-off would be stored and then never shown, which looks like
        # the command silently failed.
        await message.reply_text(
            f"{when} is in the past, so a one-off there would never come up.\n"
            "Add 'yearly' if it is an anniversary."
        )
        return

    date_id = add_date(db_path, label=label, when=when, recurring=recurring)
    resolved = upcoming([{"id": date_id, "label": label, "date": when, "recurring": recurring}])
    when_text = resolved[0].describe_when() if resolved else when
    kind = "every year" if recurring else "one-off"
    await message.reply_text(f"Saved #{date_id}: {label} ({kind}) - {when_text}.")


async def list_dates_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    rows = list_dates(context.bot_data["db_path"])
    items = upcoming(rows)
    if not items:
        await message.reply_text("No dates saved yet.\n\n" + USAGE)
        return
    lines = ["Coming up:"]
    lines += [f"  #{u.id} {u.describe()}" for u in items]
    await message.reply_text("\n".join(lines))


async def delete_date_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    args = context.args or []
    if len(args) != 1 or not args[0].lstrip("#").isdigit():
        await message.reply_text("Usage: /deldate <id>   (see ids with /dates)")
        return
    date_id = int(args[0].lstrip("#"))
    if delete_date(context.bot_data["db_path"], date_id):
        await message.reply_text(f"Removed date #{date_id}.")
    else:
        await message.reply_text(f"No date #{date_id}.")
