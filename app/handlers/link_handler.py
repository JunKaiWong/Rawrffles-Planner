"""Detects TikTok/Instagram URLs in group messages and stores them.

Part one of CLAUDE.md first session goal #3: capture URL, platform, sender and
timestamp only. Caption/tag/date extraction (yt-dlp, LLM) comes later, so those
columns stay NULL for now.

Per the project conventions this fails soft: if one URL in a message cannot be
stored, the others are still saved and the user is told what happened, rather
than the whole message being dropped.
"""

import logging
import re

from telegram import Update
from telegram.ext import ContextTypes

from app.db.database import find_link_by_url, insert_link

logger = logging.getLogger(__name__)

# Matches a TikTok/Instagram URL with a path. Scheme optional, since Telegram
# linkifies bare "www.tiktok.com/..." too. Subdomains cover vm./vt. (TikTok
# share links) and m./www.
# The lookbehind stops the domain matching inside another site's path or
# hostname, e.g. https://example.com/tiktok.com/fake must not be treated as a
# TikTok link. ("tiktok.com.evil.com/x" is already rejected: the pattern needs
# "/" straight after the domain.)
LINK_PATTERN = re.compile(
    r"(?<![\w./@-])"
    r"(?:https?://)?(?:[a-z0-9-]+\.)*"
    r"(?P<domain>tiktok\.com|instagram\.com|instagr\.am)"
    r"/\S+",
    re.IGNORECASE,
)

# Trailing characters that are almost certainly sentence punctuation rather
# than part of the URL, e.g. "check this out (https://tiktok.com/x)."
_TRAILING_JUNK = ".,!?;:'\"()[]<>"

_PLATFORM_BY_DOMAIN = {
    "tiktok.com": "tiktok",
    "instagram.com": "instagram",
    "instagr.am": "instagram",
}

PLATFORM_LABELS = {"tiktok": "TikTok", "instagram": "Instagram"}


def normalise_url(raw: str) -> str:
    """Strip trailing punctuation and ensure a scheme is present."""
    url = raw.rstrip(_TRAILING_JUNK)
    if not url.lower().startswith(("http://", "https://")):
        url = f"https://{url}"
    return url


def extract_links(text: str) -> list[tuple[str, str]]:
    """Return [(url, platform)] for every supported link in `text`, in order and
    de-duplicated within the message."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in LINK_PATTERN.finditer(text or ""):
        url = normalise_url(match.group(0))
        platform = _PLATFORM_BY_DOMAIN[match.group("domain").lower()]
        if url in seen:
            logger.debug("skipping duplicate-within-message url=%s", url)
            continue
        seen.add(url)
        found.append((url, platform))
    return found


def _describe(url: str, platform: str) -> str:
    return f"{PLATFORM_LABELS.get(platform, platform)} - {url}"


async def handle_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Store every supported link in the message and confirm what was saved."""
    message = update.effective_message
    if message is None:
        return

    db_path = context.bot_data["db_path"]
    user = message.from_user
    added_by = user.id if user else 0
    text = message.text or message.caption or ""

    logger.info(
        "link intake: chat=%s message_id=%s from=%s(%s)",
        update.effective_chat.id,
        message.message_id,
        added_by,
        user.first_name if user else "unknown",
    )
    logger.debug("message text: %r", text)

    links = extract_links(text)
    if not links:
        # Reachable if the filter regex matches something extract_links rejects.
        logger.info("no supported links found in message_id=%s", message.message_id)
        return

    logger.info(
        "found %d link(s) in message_id=%s: %s",
        len(links),
        message.message_id,
        [url for url, _ in links],
    )

    saved: list[str] = []
    duplicates: list[str] = []
    failed: list[str] = []

    for url, platform in links:
        try:
            existing = find_link_by_url(db_path, url)
            if existing is not None:
                logger.info(
                    "duplicate url=%s already stored as id=%s (added_at=%s)",
                    url,
                    existing["id"],
                    existing["added_at"],
                )
                duplicates.append(_describe(url, platform))
                continue
            link_id = insert_link(db_path, url=url, platform=platform, added_by=added_by)
            saved.append(f"#{link_id} {_describe(url, platform)}")
        except Exception:
            # Never let one bad link kill the rest of the message.
            logger.exception("failed to store url=%s platform=%s", url, platform)
            failed.append(_describe(url, platform))

    lines: list[str] = []
    if saved:
        lines.append(f"Saved {len(saved)} link{'s' if len(saved) != 1 else ''}:")
        lines.extend(f"  {item}" for item in saved)
    if duplicates:
        lines.append(f"Already saved ({len(duplicates)}):")
        lines.extend(f"  {item}" for item in duplicates)
    if failed:
        lines.append(f"Could not save ({len(failed)}) - check the logs:")
        lines.extend(f"  {item}" for item in failed)

    reply = "\n".join(lines)
    logger.info(
        "intake result for message_id=%s: saved=%d duplicate=%d failed=%d",
        message.message_id,
        len(saved),
        len(duplicates),
        len(failed),
    )
    try:
        # No parse_mode: URLs and captions are user input and would need escaping.
        await message.reply_text(reply, disable_web_page_preview=True)
    except Exception:
        logger.exception("failed to send confirmation for message_id=%s", message.message_id)
