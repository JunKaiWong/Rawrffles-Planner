"""Bot entrypoint and message routing.

Transport is a swappable seam: `build_application()` wires the token, auth
filter, and handlers, and knows nothing about how updates arrive. `run()` then
picks polling (local dev) or webhook (deployed) purely by configuration. Adding
a handler therefore never touches transport code, and switching transport never
touches handlers.

Run locally:  python -m app.bot          (TELEGRAM_TRANSPORT defaults to polling)
Deployed:     TELEGRAM_TRANSPORT=webhook WEBHOOK_URL=https://host/<path>
"""

import logging

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from app.auth import allowed_chat_filter
from app.config import POLLING, WEBHOOK, Settings, load_settings

logger = logging.getLogger(__name__)

PING_PATTERN = r"(?i)^\s*ping\s*$"


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply 'pong'. Proves token, allowlist, and transport are all working."""
    message = update.effective_message
    if message is None:
        return
    logger.info("ping from chat %s", update.effective_chat.id)
    await message.reply_text("pong")


def build_application(settings: Settings) -> Application:
    """Assemble the bot with handlers registered. Transport-agnostic."""
    application = Application.builder().token(settings.bot_token).build()

    # Every handler is AND-ed with the allowlist, so no update from another chat
    # can reach handler code.
    only_our_group = allowed_chat_filter(settings.chat_id)
    application.add_handler(
        MessageHandler(
            only_our_group & filters.TEXT & ~filters.COMMAND & filters.Regex(PING_PATTERN),
            ping,
        )
    )
    return application


def run(settings: Settings | None = None) -> None:
    """Start the bot on the configured transport."""
    settings = settings or load_settings()
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


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        level=logging.INFO,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    run()


if __name__ == "__main__":
    main()
