"""Detects TikTok/Instagram URLs in group messages, extracts metadata, stores.

Goal #3 of CLAUDE.md: capture the URL, run yt-dlp for title/caption/location,
and record the canonical URL so the same post shared via different short links
de-duplicates to one row.

De-duplication runs in two stages, cheapest first:

  1. exact match on the URL as pasted - no network call needed;
  2. match on the canonical URL after resolution - catches vm./vt. share links
     and differing tracking parameters.

Per the project conventions this fails soft at every level: a link whose
metadata cannot be extracted is still stored with its raw URL, and one link
failing never costs the others in the same message.
"""

import logging
import re

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from app.db.database import (
    find_link_by_canonical_url,
    find_link_by_url,
    insert_link,
)
from app.services.extractor import extract_async

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


def _platform_label(platform: str) -> str:
    return PLATFORM_LABELS.get(platform, platform)


def _describe(url: str, platform: str) -> str:
    return f"{_platform_label(platform)} - {url}"


def _summarise_saved(link_id: int, platform: str, url: str, metadata) -> str:
    """One confirmation entry: what was saved, and what we learned about it."""
    headline = metadata.title or url
    lines = [f"  #{link_id} {_platform_label(platform)} - {headline}"]
    if metadata.location:
        lines.append(f"     location: {metadata.location}")
    if not metadata.ok:
        # Be explicit rather than silently storing a bare URL.
        lines.append("     (saved, but metadata could not be read)")
    return "\n".join(lines)


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

    # Extraction takes seconds, so show the typing indicator rather than
    # leaving the group wondering whether the bot noticed.
    try:
        await message.chat.send_action(ChatAction.TYPING)
    except Exception:  # noqa: BLE001 - cosmetic only
        logger.debug("could not send typing action", exc_info=True)

    for url, platform in links:
        try:
            # Stage 1: exact match on the pasted URL, before any network call.
            existing = find_link_by_url(db_path, url)
            if existing is not None:
                logger.info(
                    "duplicate (exact url) %s already stored as id=%s (added_at=%s)",
                    url,
                    existing["id"],
                    existing["added_at"],
                )
                duplicates.append(f"  #{existing['id']} {_describe(url, platform)}")
                continue

            metadata = await extract_async(url)

            # Stage 2: match on the resolved URL. Catches the same post arriving
            # through a different share link or with other tracking parameters.
            if metadata.canonical_url:
                existing = find_link_by_canonical_url(db_path, metadata.canonical_url)
                if existing is not None:
                    logger.info(
                        "duplicate (canonical) %s -> %s already stored as id=%s "
                        "(originally pasted as %s)",
                        url,
                        metadata.canonical_url,
                        existing["id"],
                        existing["url"],
                    )
                    duplicates.append(
                        f"  #{existing['id']} {_platform_label(platform)} - "
                        f"same post as {existing['url']}"
                    )
                    continue

            link_id = insert_link(
                db_path,
                url=url,
                platform=platform,
                added_by=added_by,
                canonical_url=metadata.canonical_url,
                title=metadata.title,
                caption=metadata.caption,
                location=metadata.location,
            )
            saved.append(_summarise_saved(link_id, platform, url, metadata))
        except Exception:
            # Never let one bad link kill the rest of the message.
            logger.exception("failed to store url=%s platform=%s", url, platform)
            failed.append(f"  {_describe(url, platform)}")

    # Entries already carry their own indentation.
    lines: list[str] = []
    if saved:
        lines.append(f"Saved {len(saved)} link{'s' if len(saved) != 1 else ''}:")
        lines.extend(saved)
    if duplicates:
        lines.append(f"Already saved ({len(duplicates)}):")
        lines.extend(duplicates)
    if failed:
        lines.append(f"Could not save ({len(failed)}) - check the logs:")
        lines.extend(failed)

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
