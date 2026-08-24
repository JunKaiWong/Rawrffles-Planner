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
    is_day_trip,
    save_caption_parse,
)
from app.services.caption_parser import parse_caption_async
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


async def _parse_and_store(context, db_path, link_id: int, platform: str, metadata):
    """Parse the caption once and cache it. Returns the parse, or None.

    A parse failure is logged and swallowed: the link is already stored, and
    losing the structured fields is far better than losing the link.
    """
    settings = context.bot_data.get("settings")
    api_key = getattr(settings, "gemini_api_key", None)
    if not api_key:
        logger.warning("no GEMINI_API_KEY configured; skipping caption parse for id=%s", link_id)
        return None

    parsed = await parse_caption_async(
        metadata.caption,
        api_key=api_key,
        model_name=settings.gemini_model,
        title=metadata.title,
        platform=platform,
    )
    if not parsed.ok:
        # parsed_at stays NULL so a later backfill can retry this one.
        logger.warning("caption parse failed for id=%s: %s", link_id, parsed.error)
        return None

    save_caption_parse(
        db_path,
        link_id,
        title=parsed.title,
        location=parsed.location,
        region=parsed.region,
        event_start=parsed.event_start,
        event_end=parsed.event_end,
        is_evergreen=parsed.is_evergreen,
    )
    return parsed


def _summarise_saved(link_id: int, platform: str, url: str, metadata, parsed=None) -> str:
    """One confirmation entry: what was saved, and what we learned about it."""
    headline = (parsed.title if parsed and parsed.title else None) or metadata.title or url
    lines = [f"  #{link_id} {_platform_label(platform)} - {headline}"]

    location = (parsed.location if parsed else None) or metadata.location
    if location:
        region = parsed.region if parsed else None
        suffix = f" ({region})" if region and is_day_trip(region) else ""
        lines.append(f"     location: {location}{suffix}")

    if parsed and parsed.event_end:
        window = parsed.event_start or "?"
        lines.append(f"     runs: {window} to {parsed.event_end}")
    if parsed and is_day_trip(parsed.region):
        # Say it plainly: this one will not be clustered into a Saturday plan.
        lines.append("     day trip - outside Singapore")

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
            # Caption parse runs once, here at intake, and the result is cached
            # on the row. Planning never re-parses.
            parsed = None
            if metadata.caption or metadata.title:
                parsed = await _parse_and_store(
                    context, db_path, link_id, platform, metadata
                )
            saved.append(_summarise_saved(link_id, platform, url, metadata, parsed))
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
