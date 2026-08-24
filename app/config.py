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
    allowed_user_ids: frozenset[int] = frozenset()
    webhook_url: str | None = None
    webhook_port: int = 8443
    webhook_secret: str | None = None
    db_path: Path = PROJECT_ROOT / "planner.db"
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.6-flash"


def _require(name: str, env_file: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name} is missing or empty in {env_file}")
    return value


def _parse_user_ids(raw: str) -> frozenset[int]:
    """Parse the comma-separated ALLOWED_USER_IDS list.

    A malformed entry raises rather than being skipped: silently dropping an id
    would quietly lock a real user out, and silently keeping a bad one would be
    worse.
    """
    ids = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.add(int(chunk))
        except ValueError as exc:
            raise RuntimeError(
                f"ALLOWED_USER_IDS contains a non-numeric entry: {chunk!r}"
            ) from exc
    return frozenset(ids)


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
        db_path=Path(os.getenv("DATABASE_PATH") or PROJECT_ROOT / "planner.db"),
        allowed_user_ids=_parse_user_ids(os.getenv("ALLOWED_USER_IDS") or ""),
        # Absent key is not fatal: link capture still works, captions simply go
        # unparsed until one is configured.
        gemini_api_key=(os.getenv("GEMINI_API_KEY") or "").strip() or None,
        gemini_model=(os.getenv("GEMINI_MODEL") or "gemini-3.6-flash").strip(),
    )
