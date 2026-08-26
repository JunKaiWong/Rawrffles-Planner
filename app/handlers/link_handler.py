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

import asyncio
import logging
import re

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from app.db.database import (
    add_photo_file_ids,
    find_link_by_canonical_url,
    find_link_by_url,
    get_link,
    insert_link,
    is_day_trip,
    save_caption_parse,
    save_geocode,
    split_file_ids,
)
from app.services.caption_parser import parse_caption_async
from app.services.geocoder import geocode
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

# Telegram delivers an album as separate messages sharing a media_group_id,
# and usually only one of them carries the caption. They arrive back-to-back,
# so the group is buffered briefly and processed once the flow stops - which
# is also what keeps several slides to a single Gemini call.
MEDIA_GROUP_DEBOUNCE_SECONDS = 2.5
MAX_IMAGES_PER_POST = 6

# Sending a screenshot for a link that already has one is skipped by default so
# a re-send cannot re-spend quota. This marker in the caption says "I mean it",
# adding the slide and re-reading the post.
ADD_PHOTO_MARKER = re.compile(r"(?:^|\s)(?:/addphoto|\+photo)(?:\s|$)", re.IGNORECASE)

# Buffered album messages, keyed by (chat_id, media_group_id). Handlers run
# sequentially by default, so a plain dict needs no lock.
_pending_media_groups: dict[tuple[int, str], dict] = {}


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


def _photo_file_id(message) -> str | None:
    """Largest available size of a photo message, or None."""
    photos = getattr(message, "photo", None)
    if not photos:
        return None
    return photos[-1].file_id  # Telegram orders sizes smallest to largest.


async def _download_images(context, file_ids: list[str]) -> list[bytes]:
    """Fetch image bytes for the parse.

    Only file_ids are persisted - Telegram re-serves the images on demand - so
    these bytes live just long enough for one call. A failed download is
    skipped rather than aborting: three slides minus one still describes the
    post.
    """
    images: list[bytes] = []
    for file_id in file_ids[:MAX_IMAGES_PER_POST]:
        try:
            telegram_file = await context.bot.get_file(file_id)
            data = bytes(await telegram_file.download_as_bytearray())
        except Exception:
            logger.exception("could not download photo file_id=%s", file_id)
            continue
        logger.info("downloaded photo file_id=%s (%d bytes)", file_id, len(data))
        images.append(data)
    if len(file_ids) > MAX_IMAGES_PER_POST:
        logger.info(
            "message carried %d images; using the first %d",
            len(file_ids),
            MAX_IMAGES_PER_POST,
        )
    return images


async def _flush_media_group(context) -> None:
    """Process one buffered album as a single post."""
    key = context.job.data
    pending = _pending_media_groups.pop(key, None)
    if not pending:
        return

    file_ids = pending["file_ids"]
    caption = pending["caption"]
    message = pending["message"]
    logger.info(
        "media group %s complete: %d photo(s), caption=%r",
        key[1],
        len(file_ids),
        (caption or "")[:80],
    )

    if not extract_links(caption or ""):
        # An ordinary photo album with no link in any caption: not ours.
        logger.info("media group %s has no supported link; ignoring", key[1])
        return

    await _run_intake(context, message, caption or "", file_ids)


async def _buffer_media_group(context, message) -> None:
    """Collect album members, restarting the timer as each one arrives.

    Only one message in an album carries the caption, so every member must be
    buffered and the caption taken from whichever has it.
    """
    key = (message.chat.id, message.media_group_id)
    file_id = _photo_file_id(message)
    pending = _pending_media_groups.get(key)

    if pending is None:
        pending = {"file_ids": [], "caption": None, "message": message, "job": None}
        _pending_media_groups[key] = pending

    if file_id and file_id not in pending["file_ids"]:
        pending["file_ids"].append(file_id)
    caption = (message.caption or "").strip()
    if caption and not pending["caption"]:
        pending["caption"] = caption
        # Reply to the message that carried the caption; it reads naturally.
        pending["message"] = message

    # Restart the debounce so the group is processed only once it stops growing.
    if pending["job"] is not None:
        pending["job"].schedule_removal()
    pending["job"] = context.job_queue.run_once(
        _flush_media_group, MEDIA_GROUP_DEBOUNCE_SECONDS, data=key
    )
    logger.debug(
        "buffered media group %s (%d photo(s) so far)", key[1], len(pending["file_ids"])
    )


async def _parse_and_store(
    context, db_path, link_id: int, platform: str, metadata, images=None
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

    # A cover image from oEmbed goes through the same vision call as any
    # screenshots: a TikTok cover is the first frame and usually carries the
    # on-screen text the caption leaves out. Still one call.
    all_images = list(images or [])
    thumbnail = getattr(metadata, "thumbnail", None)
    if thumbnail:
        all_images.append(thumbnail)
        logger.info("including oEmbed cover image in the parse for id=%s", link_id)

    parsed = await parse_caption_async(
        metadata.caption,
        api_key=api_key,
        model_name=settings.gemini_model,
        title=metadata.title,
        platform=platform,
        images=all_images,
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

    # Geocode straight after parsing, while the location string is fresh, so it
    # happens once per link and never during a planning run. Failure here costs
    # nothing already stored.
    if parsed.location:
        try:
            located = await asyncio.to_thread(geocode, parsed.location, parsed.region)
            save_geocode(
                db_path,
                link_id,
                status=located.status,
                lat=located.lat,
                lng=located.lng,
            )
        except Exception:
            logger.exception("geocoding failed for id=%s", link_id)

    return parsed


async def _enrich_existing(
    context, db_path, existing_id: int, platform: str, file_ids, images, force: bool
):
    """Use screenshots to fill in a link that was already saved.

    The normal sequence for a photo/slideshow post is two messages: the URL
    first, which yt-dlp cannot read and so stores almost nothing, then the
    screenshots. Treating those as bare duplicates would throw away the only
    content this post will ever have.

    A link with no screenshot yet is always enriched. One that already has some
    is left alone unless `force` is set, so an accidental re-send cannot
    re-spend quota - the caption marker is how the user says they mean it.
    """
    row = get_link(db_path, existing_id)
    if row is None:
        return None

    already = split_file_ids(row["photo_file_id"])
    if already and not force:
        logger.info(
            "link id=%s already has %d screenshot(s); not re-parsing (use +photo to force)",
            existing_id,
            len(already),
        )
        return None

    added = add_photo_file_ids(db_path, existing_id, file_ids)
    if not added and already:
        # The same screenshot again: nothing new to read.
        logger.info("link id=%s got no new screenshots; not re-parsing", existing_id)
        return None
    if not images:
        return None

    logger.info(
        "enriching existing link id=%s from %d screenshot(s)%s",
        existing_id,
        len(images),
        " (forced)" if force and already else "",
    )

    class _Meta:  # matches the shape _parse_and_store expects
        caption = row["caption"]
        title = row["title"]

    return await _parse_and_store(
        context, db_path, existing_id, platform, _Meta(), images
    )


def _summarise_enriched(link_id: int, platform: str, parsed) -> str:
    """Confirmation for a link filled in from a screenshot."""
    headline = parsed.title or "(no title found in image)"
    lines = [f"  #{link_id} {_platform_label(platform)} - {headline}"]
    lines.extend(_summarise_parsed(parsed))
    return "\n".join(lines)


def _parse_yielded_content(parsed) -> bool:
    """Did the vision/caption parse actually learn anything?

    ok=True only means the call completed - a post with nothing readable still
    returns an empty result. "other/other" is the model's honest shrug, so it
    does not count as content on its own.
    """
    if parsed is None or not parsed.ok:
        return False
    return bool(
        parsed.title
        or parsed.location
        or parsed.tags
        or parsed.event_start
        or parsed.event_end
        or (parsed.category and parsed.category != "other")
    )


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
    link_id: int, platform: str, url: str, metadata, parsed=None, image_count: int = 0
) -> str:
    """One confirmation entry: what was saved, and what we learned about it."""
    headline = (parsed.title if parsed and parsed.title else None) or metadata.title or url
    lines = [f"  #{link_id} {_platform_label(platform)} - {headline}"]
    if image_count:
        # Say where the information came from, since for a photo post the
        # screenshots are the only source there is.
        plural = "screenshot" if image_count == 1 else f"{image_count} screenshots"
        lines.append(f"     read from your {plural}")

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

    # Report the outcome, not the stage. yt-dlp failing is routine and
    # uninteresting when the screenshots supplied the facts instead; only say
    # something went unread when nothing was learned from any source.
    if not _parse_yielded_content(parsed) and not (metadata.title or metadata.caption):
        if image_count:
            lines.append("     saved, but nothing readable in the screenshots")
        else:
            lines.append(
                "     saved, but nothing could be read - reply with a screenshot"
            )
            lines.append("     (photo + this URL in the caption) to fill it in")
    return "\n".join(lines)


async def handle_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Entry point: route albums to the buffer, everything else straight through."""
    message = update.effective_message
    if message is None:
        return

    # An album arrives as several messages; collect them before doing anything,
    # so all its slides reach Gemini in one call.
    if getattr(message, "media_group_id", None):
        await _buffer_media_group(context, message)
        return

    text = message.text or message.caption or ""
    file_id = _photo_file_id(message)
    if not extract_links(text):
        # A lone photo with no link: the filter admits photos so album members
        # without captions can be buffered, so this is the expected non-match.
        logger.debug("message_id=%s has no supported link; ignoring", message.message_id)
        return

    await _run_intake(context, message, text, [file_id] if file_id else [])


async def _run_intake(
    context: ContextTypes.DEFAULT_TYPE,
    message,
    text: str,
    photo_file_ids: list[str],
) -> None:
    """Store every supported link and confirm what was saved."""
    db_path = context.bot_data["db_path"]
    user = message.from_user
    added_by = user.id if user else 0

    logger.info(
        "link intake: chat=%s message_id=%s from=%s(%s) photos=%d",
        message.chat.id,
        message.message_id,
        added_by,
        user.first_name if user else "unknown",
        len(photo_file_ids),
    )
    logger.debug("message text: %r", text)

    # An explicit marker in the caption means "add this slide even though the
    # link already has one", which is otherwise skipped to protect quota.
    force_add = bool(ADD_PHOTO_MARKER.search(text))
    if force_add:
        logger.info("caption carries the add-photo marker; forcing re-read")

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

    # Screenshots sent with the URL in the caption are how photo/slideshow
    # posts get their content to us, since yt-dlp cannot read them.
    images = await _download_images(context, photo_file_ids) if photo_file_ids else []

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
                    context, db_path, existing["id"], platform, photo_file_ids, images, force_add
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
                        context, db_path, existing["id"], platform, photo_file_ids, images, force_add
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
                photo_file_id=",".join(photo_file_ids) or None,
            )
            # Caption parse runs once, here at intake, and the result is cached
            # on the row. Planning never re-parses. A screenshot rides along in
            # that same call rather than costing a second one.
            parsed = None
            if metadata.caption or metadata.title or images or metadata.thumbnail:
                parsed = await _parse_and_store(
                    context, db_path, link_id, platform, metadata, images
                )
            saved.append(
                _summarise_saved(link_id, platform, url, metadata, parsed, len(images))
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
        if images:
            # The screenshots were skipped, so say how to override.
            lines.append("  (add +photo to the caption to attach these anyway)")
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
