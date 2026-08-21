"""Access control for both surfaces.

Chat allowlist (bot): the bot serves exactly one private group. Bots are
publicly discoverable by username, so this is enforced in code, not just via
BotFather. Enforcement is a handler *filter* rather than a check inside each
handler, so an update from any other chat is dropped before a handler body runs
- no parsing, no LLM call, no API quota spent. It applies identically under
polling and webhook transports, since both dispatch through the same chain.

User allowlist (Mini App): `Telegram.WebApp.initData` is a signed query string.
Unvalidated it is trivially forgeable - anyone can POST a user id - so the
HMAC-SHA256 signature is verified against the bot token *before* any user id in
it is trusted. Two independent checks then apply, per CLAUDE.md:

  1. the signature proves the data really came from Telegram;
  2. the user id must additionally appear in ALLOWED_USER_IDS, proving it is
     one of our two users rather than any Telegram user in the world.

A replay window is also enforced: initData older than MAX_INIT_DATA_AGE is
rejected, so a signature captured once cannot be reused indefinitely.
"""

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

from telegram.ext import filters

logger = logging.getLogger(__name__)

# initData older than this is refused even when the signature is valid.
MAX_INIT_DATA_AGE_SECONDS = 24 * 60 * 60


class InitDataError(Exception):
    """initData was missing, malformed, unsigned, stale, or not ours."""


@dataclass(frozen=True)
class TelegramUser:
    """A caller proven to be a Telegram user on our allowlist."""

    id: int
    first_name: str | None = None
    username: str | None = None


# --- Bot side -------------------------------------------------------------


def is_allowed_chat(chat_id: int | None, allowed_chat_id: int) -> bool:
    """Plain predicate, for callers that aren't PTB handlers (e.g. a future
    FastAPI webhook route)."""
    return chat_id is not None and chat_id == allowed_chat_id


def allowed_chat_filter(allowed_chat_id: int) -> filters.BaseFilter:
    """Filter admitting only the one allowlisted group chat."""
    return filters.Chat(chat_id=allowed_chat_id)


# --- Mini App side --------------------------------------------------------


def _secret_key(bot_token: str) -> bytes:
    """Telegram's WebApp key derivation: HMAC of the bot token keyed by the
    literal string "WebAppData" (note the argument order - it is the reverse of
    the usual convention and getting it backwards silently fails every check).
    """
    return hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()


def verify_init_data(
    init_data: str,
    bot_token: str,
    max_age_seconds: int = MAX_INIT_DATA_AGE_SECONDS,
) -> dict:
    """Verify the signature on initData and return its parsed fields.

    Raises InitDataError on anything suspicious. Does NOT check the allowlist -
    that is a separate decision, made by `authorise_user`.
    """
    if not init_data:
        raise InitDataError("missing initData")

    # strict_parsing so a malformed string is rejected rather than silently
    # parsed into something that happens to validate.
    try:
        fields = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError as exc:
        raise InitDataError(f"malformed initData: {exc}") from exc

    received_hash = fields.pop("hash", None)
    if not received_hash:
        raise InitDataError("initData has no hash")

    # The signed payload is every remaining field, sorted by key, as "k=v"
    # joined by newlines.
    data_check_string = "\n".join(
        f"{key}={fields[key]}" for key in sorted(fields)
    )
    expected_hash = hmac.new(
        _secret_key(bot_token), data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    # Constant-time comparison: a plain == leaks timing information.
    if not hmac.compare_digest(expected_hash, received_hash):
        logger.warning("initData signature mismatch")
        raise InitDataError("bad initData signature")

    auth_date = fields.get("auth_date")
    if not auth_date or not auth_date.isdigit():
        raise InitDataError("initData has no usable auth_date")
    age = time.time() - int(auth_date)
    if age > max_age_seconds:
        logger.warning("initData rejected: %.0fs old (max %s)", age, max_age_seconds)
        raise InitDataError("initData has expired")
    if age < -60:  # tolerate small clock skew, refuse the future
        raise InitDataError("initData auth_date is in the future")

    return fields


def parse_user(fields: dict) -> TelegramUser:
    """Pull the user object out of already-verified initData fields."""
    raw_user = fields.get("user")
    if not raw_user:
        raise InitDataError("initData contains no user")
    try:
        payload = json.loads(raw_user)
        user_id = int(payload["id"])
    except (ValueError, KeyError, TypeError) as exc:
        raise InitDataError(f"initData user is unreadable: {exc}") from exc
    return TelegramUser(
        id=user_id,
        first_name=payload.get("first_name"),
        username=payload.get("username"),
    )


def is_allowed_user(user_id: int, allowed_user_ids: frozenset[int]) -> bool:
    return user_id in allowed_user_ids


def authorise_user(
    init_data: str,
    bot_token: str,
    allowed_user_ids: frozenset[int],
    max_age_seconds: int = MAX_INIT_DATA_AGE_SECONDS,
) -> TelegramUser:
    """Full Mini App check: valid signature, fresh, and on the allowlist.

    This is the single entry point the API middleware calls, so the two checks
    cannot be applied independently or accidentally skipped one at a time.
    """
    fields = verify_init_data(init_data, bot_token, max_age_seconds)
    user = parse_user(fields)
    if not is_allowed_user(user.id, allowed_user_ids):
        # Signature was genuine, so this is a real Telegram user - just not ours.
        logger.warning("rejected authenticated user %s: not in ALLOWED_USER_IDS", user.id)
        raise InitDataError("user is not allowed to use this app")
    logger.info("authorised user %s (%s)", user.id, user.first_name)
    return user
