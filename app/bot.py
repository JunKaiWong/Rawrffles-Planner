"""Bot entrypoint and message routing.

Transport is a swappable seam: `build_application()` wires the token, auth
filter, and handlers, and knows nothing about how updates arrive. `run()` then
picks polling (local dev) or webhook (deployed) purely by configuration. Adding
a handler therefore never touches transport code, and switching transport never
touches handlers.

Run locally:  python -m app.bot          (TELEGRAM_TRANSPORT defaults to polling)
Deployed:     TELEGRAM_TRANSPORT=webhook WEBHOOK_URL=https://host/<path>

Logging goes to both the console and logs/bot.log (gitignored), so a failure
that happened while nobody was watching can still be traced afterwards.
"""

import logging
import logging.handlers
import sys
from pathlib import Path

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from app.auth import allowed_chat_filter
from app.config import POLLING, PROJECT_ROOT, WEBHOOK, Settings, load_settings
from app.db.database import count_links, init_db
from app.handlers.link_handler import LINK_PATTERN, handle_links

logger = logging.getLogger(__name__)

PING_PATTERN = r"(?i)^\s*ping\s*$"
LOG_FILE = PROJECT_ROOT / "logs" / "bot.log"


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply 'pong'. Proves token, allowlist, and transport are all working."""
    message = update.effective_message
    if message is None:
        return
    logger.info("ping from chat %s", update.effective_chat.id)
    await message.reply_text("pong")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Last-resort handler so no exception disappears silently."""
    logger.exception("unhandled error while processing update: %s", update, exc_info=context.error)


def build_application(settings: Settings) -> Application:
    """Assemble the bot with handlers registered. Transport-agnostic."""
    application = Application.builder().token(settings.bot_token).build()

    # Handlers read configuration from here rather than importing settings.
    application.bot_data["db_path"] = settings.db_path
    application.bot_data["settings"] = settings

    # Every handler is AND-ed with the allowlist, so no update from another chat
    # can reach handler code.
    only_our_group = allowed_chat_filter(settings.chat_id)

    application.add_handler(
        MessageHandler(
            only_our_group & filters.TEXT & ~filters.COMMAND & filters.Regex(PING_PATTERN),
            ping,
        )
    )
    # Link intake matches message text or a photo/video caption. Only the first
    # matching handler runs, and 'ping' is registered above, so the two cannot
    # both fire on one message.
    application.add_handler(
        MessageHandler(
            only_our_group
            & ~filters.COMMAND
            & (filters.Regex(LINK_PATTERN) | filters.CaptionRegex(LINK_PATTERN)),
            handle_links,
        )
    )
    application.add_error_handler(on_error)

    logger.info(
        "application built: %d handler(s), allowlisted chat=%s, db=%s",
        sum(len(group) for group in application.handlers.values()),
        settings.chat_id,
        settings.db_path,
    )
    return application


def run(settings: Settings | None = None) -> None:
    """Start the bot on the configured transport."""
    settings = settings or load_settings()

    init_db(settings.db_path)
    logger.info("links currently stored: %d", count_links(settings.db_path))

    application = build_application(settings)

    if settings.transport == POLLING:
        logger.info("starting polling; serving chat %s", settings.chat_id)
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    elif settings.transport == WEBHOOK:
        logger.info("starting webhook at %s", settings.webhook_url)
        application.run_webhook(
            listen="0.0.0.0",
            port=settings.webhook_port,
            webhook_url=settings.webhook_url,
            secret_token=settings.webhook_secret,
            allowed_updates=Update.ALL_TYPES,
        )
    else:  # pragma: no cover - load_settings() already rejects other values
        raise RuntimeError(f"unknown transport {settings.transport!r}")


def setup_logging(log_file: Path = LOG_FILE) -> None:
    """Console + rotating file logging."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s - %(message)s"
    )

    # Captions routinely contain emoji, which the Windows console's default
    # cp1252 codec cannot encode - without this, logging one raises
    # UnicodeEncodeError and takes the handler down with it.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover - non-standard stdout
        pass

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)

    # 1 MB per file, five generations kept.
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file_handler)

    # These are chatty at INFO and drown out our own lines.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext.Application").setLevel(logging.INFO)
    logger.info("logging to %s", log_file)


def main() -> None:
    setup_logging()
    run()


if __name__ == "__main__":
    main()
