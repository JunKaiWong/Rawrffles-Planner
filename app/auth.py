"""Access control.

Chat allowlist: the bot serves exactly one private group. Bots are publicly
discoverable by username, so this is enforced in code, not just via BotFather.

Enforcement is a handler *filter* rather than a check inside each handler, so an
update from any other chat is dropped before a handler body runs - no parsing,
no LLM call, no API quota spent. Applies identically under polling and webhook
transports, since both dispatch through the same handler chain.

Mini App initData/HMAC validation will also live here (First session goal #4).
"""

from telegram.ext import filters


def is_allowed_chat(chat_id: int | None, allowed_chat_id: int) -> bool:
    """Plain predicate, for callers that aren't PTB handlers (e.g. the future
    FastAPI webhook route)."""
    return chat_id is not None and chat_id == allowed_chat_id


def allowed_chat_filter(allowed_chat_id: int) -> filters.BaseFilter:
    """Filter admitting only the one allowlisted group chat."""
    return filters.Chat(chat_id=allowed_chat_id)
