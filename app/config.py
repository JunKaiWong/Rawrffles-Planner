"""Environment-backed settings.

Secrets live in .env.planner (gitignored); .env.example is the empty template.
Override the file with ENV_FILE=... for tests or alternate deployments.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

POLLING = "polling"
WEBHOOK = "webhook"


@dataclass(frozen=True)
class Settings:
    """Everything the bot needs to start, already validated."""

    bot_token: str
    chat_id: int
    transport: str
    webhook_url: str | None = None
    webhook_port: int = 8443
    webhook_secret: str | None = None


def _require(name: str, env_file: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name} is missing or empty in {env_file}")
    return value


def load_settings(env_file: str | None = None) -> Settings:
    """Read and validate settings. Fails loudly at startup rather than midway
    through the first update."""
    env_file = env_file or os.getenv("ENV_FILE", ".env.planner")
    load_dotenv(PROJECT_ROOT / env_file, override=False)

    chat_id_raw = _require("TELEGRAM_CHAT_ID", env_file)
    try:
        chat_id = int(chat_id_raw)
    except ValueError as exc:
        raise RuntimeError(
            f"TELEGRAM_CHAT_ID must be a number, got {chat_id_raw!r}"
        ) from exc

    transport = (os.getenv("TELEGRAM_TRANSPORT") or POLLING).strip().lower()
    if transport not in (POLLING, WEBHOOK):
        raise RuntimeError(
            f"TELEGRAM_TRANSPORT must be {POLLING!r} or {WEBHOOK!r}, got {transport!r}"
        )

    webhook_url = (os.getenv("WEBHOOK_URL") or "").strip() or None
    if transport == WEBHOOK and not webhook_url:
        raise RuntimeError("WEBHOOK_URL is required when TELEGRAM_TRANSPORT=webhook")

    return Settings(
        bot_token=_require("TELEGRAM_BOT_TOKEN", env_file),
        chat_id=chat_id,
        transport=transport,
        webhook_url=webhook_url,
        webhook_port=int(os.getenv("WEBHOOK_PORT") or 8443),
        webhook_secret=(os.getenv("WEBHOOK_SECRET") or "").strip() or None,
    )
