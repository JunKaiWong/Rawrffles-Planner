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
    get_link,
    insert_link,
    is_day_trip,
    save_caption_parse,
    set_photo_file_id,
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


async def _fetch_photo(context, message) -> tuple[str | None, bytes | None]:
    """Return (file_id, image bytes) for a photo message, or (None, None).

    Telegram sends several sizes; the last is the largest. Only the file_id is
    stored - Telegram re-serves the image on demand, so the bytes are used for
    this one parse and then discarded rather than costing us storage.
    """
    photos = getattr(message, "photo", None)
    if not photos:
        return None, None

    largest = photos[-1]
    file_id = largest.file_id
    try:
        telegram_file = await context.bot.get_file(file_id)
        image = bytes(await telegram_file.download_as_bytearray())
    except Exception:
        # The file_id is still worth keeping even if the download failed: the
        # Mini App can render it later.
        logger.exception("could not download photo file_id=%s", file_id)
        return file_id, None

    logger.info("downloaded photo file_id=%s (%d bytes)", file_id, len(image))
    return file_id, image


async def _parse_and_store(
    context, db_path, link_id: int, platform: str, metadata, image_bytes=None
):
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
        image_bytes=image_bytes,
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
        category=parsed.category,
        subcategory=parsed.subcategory,
        tags=parsed.tags,
    )
    return parsed


async def _enrich_existing(context, db_path, existing_id: int, platform: str, photo_file_id, image_bytes):
    """Use a screenshot to fill in a link that was already saved.

    The normal sequence for a photo/slideshow post is two messages: the URL
    first, which yt-dlp cannot read and so stores almost nothing, then the
    screenshot. Treating the second as a bare duplicate would throw away the
    only content this post will ever have.

    Only links without a screenshot are enriched, so re-sending an image does
    not re-spend quota.
    """
    row = get_link(db_path, existing_id)
    if row is None:
        return None
    if row["photo_file_id"]:
        logger.info("link id=%s already has a screenshot; not re-parsing", existing_id)
        return None

    if photo_file_id:
        set_photo_file_id(db_path, existing_id, photo_file_id)
    if image_bytes is None:
        return None

    logger.info("enriching existing link id=%s from screenshot", existing_id)

    class _Meta:  # matches the shape _parse_and_store expects
        caption = row["caption"]
        title = row["title"]

    return await _parse_and_store(
        context, db_path, existing_id, platform, _Meta(), image_bytes
    )


def _summarise_enriched(link_id: int, platform: str, parsed) -> str:
    """Confirmation for a link filled in from a screenshot."""
    headline = parsed.title or "(no title found in image)"
    lines = [f"  #{link_id} {_platform_label(platform)} - {headline}"]
    lines.extend(_summarise_parsed(parsed))
    return "\n".join(lines)


def _summarise_parsed(parsed) -> list[str]:
    """The extracted fields, shown so the result is verifiable in chat."""
    lines = []
    if parsed.location:
        suffix = f" ({parsed.region})" if parsed.region and is_day_trip(parsed.region) else ""
        lines.append(f"     location: {parsed.location}{suffix}")
    if parsed.category:
        label = f"{parsed.category}/{parsed.subcategory}"
        if parsed.tags:
            label += f" · {', '.join(parsed.tags)}"
        lines.append(f"     {label}")
    if parsed.event_end:
        window = parsed.event_start or "?"
        lines.append(f"     runs: {window} to {parsed.event_end}")
    return lines


def _summarise_saved(
    link_id: int, platform: str, url: str, metadata, parsed=None, from_image: bool = False
) -> str:
    """One confirmation entry: what was saved, and what we learned about it."""
    headline = (parsed.title if parsed and parsed.title else None) or metadata.title or url
    lines = [f"  #{link_id} {_platform_label(platform)} - {headline}"]
    if from_image:
        # Say where the information came from, since for a photo post the
        # screenshot is the only source there is.
        lines.append("     read from your screenshot")

    location = (parsed.location if parsed else None) or metadata.location
    if location:
        region = parsed.region if parsed else None
        suffix = f" ({region})" if region and is_day_trip(region) else ""
        lines.append(f"     location: {location}{suffix}")

    if parsed and parsed.category:
        label = f"{parsed.category}/{parsed.subcategory}"
        if parsed.tags:
            label += f" · {', '.join(parsed.tags)}"
        lines.append(f"     {label}")
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
    updated: list[str] = []
    duplicates: list[str] = []
    failed: list[str] = []

    # Extraction takes seconds, so show the typing indicator rather than
    # leaving the group wondering whether the bot noticed.
    try:
        await message.chat.send_action(ChatAction.TYPING)
    except Exception:  # noqa: BLE001 - cosmetic only
        logger.debug("could not send typing action", exc_info=True)

    # A screenshot sent with the URL in its caption is how photo/slideshow
    # posts get their content to us, since yt-dlp cannot read them.
    photo_file_id, image_bytes = await _fetch_photo(context, message)
    if photo_file_id:
        logger.info(
            "message_id=%s carries a photo (file_id=%s, downloaded=%s)",
            message.message_id,
            photo_file_id,
            image_bytes is not None,
        )

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
                enriched = await _enrich_existing(
                    context, db_path, existing["id"], platform, photo_file_id, image_bytes
                )
                if enriched:
                    updated.append(_summarise_enriched(existing["id"], platform, enriched))
                else:
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
                    # A screenshot for a link we already hold is the point of
                    # the photo workflow, not a duplicate to discard.
                    enriched = await _enrich_existing(
                        context, db_path, existing["id"], platform, photo_file_id, image_bytes
                    )
                    if enriched:
                        updated.append(_summarise_enriched(existing["id"], platform, enriched))
                    else:
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
                photo_file_id=photo_file_id,
            )
            # Caption parse runs once, here at intake, and the result is cached
            # on the row. Planning never re-parses. A screenshot rides along in
            # that same call rather than costing a second one.
            parsed = None
            if metadata.caption or metadata.title or image_bytes:
                parsed = await _parse_and_store(
                    context, db_path, link_id, platform, metadata, image_bytes
                )
            saved.append(
                _summarise_saved(link_id, platform, url, metadata, parsed, bool(image_bytes))
            )
        except Exception:
            # Never let one bad link kill the rest of the message.
            logger.exception("failed to store url=%s platform=%s", url, platform)
            failed.append(f"  {_describe(url, platform)}")

    # Entries already carry their own indentation.
    lines: list[str] = []
    if saved:
        lines.append(f"Saved {len(saved)} link{'s' if len(saved) != 1 else ''}:")
        lines.extend(saved)
    if updated:
        lines.append(f"Updated from your screenshot ({len(updated)}):")
        lines.extend(updated)
    if duplicates:
        lines.append(f"Already saved ({len(duplicates)}):")
        lines.extend(duplicates)
    if failed:
        lines.append(f"Could not save ({len(failed)}) - check the logs:")
        lines.extend(failed)

    reply = "\n".join(lines)
    logger.info(
        "intake result for message_id=%s: saved=%d updated=%d duplicate=%d failed=%d",
        message.message_id,
        len(saved),
        len(updated),
        len(duplicates),
        len(failed),
    )
    try:
        # No parse_mode: URLs and captions are user input and would need escaping.
        await message.reply_text(reply, disable_web_page_preview=True)
    except Exception:
        logger.exception("failed to send confirmation for message_id=%s", message.message_id)
