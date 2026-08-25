"""Caption -> structured JSON, via Gemini (google-genai SDK).

CLAUDE.md calls for parsing the caption into
{title, location, event_start, event_end, is_evergreen} at intake, explicitly
not with regex: captions say "till 31 Aug", "this weekend only", "opening
Friday", and a pattern-matching approach gets these subtly wrong.

Three rules shape this module:

1. **Parse once.** The result is cached on the row and never recomputed. This
   is the difference between staying inside the Gemini free tier and burning
   through it, since a planning run reads many links at once.

2. **Fail soft.** A parse failure must never cost the link. Every error path
   returns ok=False and the caller stores the link unchanged.

3. **Validate in code, not on trust.** The model returns JSON, but the dates
   are re-checked here: a malformed date becomes None rather than poisoning
   the urgency tiers that planning will later compute from it.

`region` is a provisional country classification, used to keep non-Singapore
links out of MRT-based Saturday clustering while leaving them saved and
browsable. Geocoding will later set it authoritatively; until then this is a
cheap best guess taken from the same call.
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

logger = logging.getLogger(__name__)

PARSE_TIMEOUT_SECONDS = 90
# Two distinct transient failures are worth retrying, and both are common on
# the free tier:
#   429 (ClientError) - the per-minute request quota, hit by a burst of links;
#   503 (ServerError) - the newer flash models shedding load under demand.
# One retry recovers the usual case without turning either into a lost parse.
TRANSIENT_RETRIES = 2
RETRY_BACKOFF_SECONDS = 15
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# The closed taxonomy from CLAUDE.md. Free-form category naming fragments fast
# ("Japanese" / "japanese food" / "Jap cuisine" become three filter values), so
# the model picks from these lists and anything outside them is coerced to
# "other" here rather than being allowed into the database.
CATEGORIES = ("food", "activity", "place", "other")

SUBCATEGORIES = {
    "food": (
        "japanese", "korean", "chinese", "local/hawker", "western",
        "thai", "indian", "cafe/dessert", "other",
    ),
    "activity": (
        "sports", "hiking/nature", "event/festival", "arts/museum",
        "workshop", "nightlife", "other",
    ),
    "place": ("bar", "staycation", "shopping", "scenic/view", "other"),
    # CLAUDE.md defines no subcategory list for the catch-all category.
    "other": ("other",),
}

MAX_TAGS = 5

_TAXONOMY_BLOCK = "\n".join(
    f"  - {category}: {', '.join(subs)}" for category, subs in SUBCATEGORIES.items()
)

PROMPT_TEMPLATE = """\
You extract structured facts about a place or event from a social media caption.

Today's date is {today}. Resolve relative dates ("this weekend", "till Sunday",
"next Friday") against it. If a date has no year and has already passed this
year, assume the next occurrence.

Return ONLY a JSON object with exactly these keys:
{{
  "title":        short name of the place or event, or null,
  "location":     the venue, address or area exactly as stated, or null,
  "region":       the country it is in (e.g. "Singapore", "Malaysia"), or null,
  "event_start":  "YYYY-MM-DD" or null,
  "event_end":    "YYYY-MM-DD" or null,
  "is_evergreen": true or false,
  "category":     exactly one of: {categories},
  "subcategory":  exactly one, from the list for the chosen category,
  "tags":         array of 0 to {max_tags} short lowercase free-form tags
}}

Allowed subcategories per category:
{taxonomy}

Rules:
- is_evergreen is true for permanent things (a restaurant, a park, a shop) and
  false for anything with an end date (a pop-up, a festival, a limited menu).
- Use null when the caption does not say. Do NOT guess a location, a country or
  a date that is not supported by the caption.
- Do not infer a country from your own knowledge of a venue name; only use what
  the caption states or clearly implies (e.g. an explicit city or country).
- category and subcategory MUST come from the lists above, spelled exactly.
  When the caption is ambiguous, return "other" rather than guessing: a
  confidently wrong category silently hides the link from filtered views,
  which is worse than an honest "other".
- tags carry detail that does not deserve a category ("halal", "rooftop",
  "cheap eats", "queue long"). Use an empty array if nothing is worth tagging;
  do not restate the category or the venue name as a tag.

{image_note}Platform: {platform}
Post title: {title}
Caption:
\"\"\"
{caption}
\"\"\"
"""

# Added to the prompt when a screenshot accompanies the post. yt-dlp cannot read
# TikTok photo/slideshow posts, so the users screenshot the slide that matters
# and send it with the URL in the caption - which means the image, not the
# caption, carries the actual content.
IMAGE_NOTE = """\
An image is attached: a screenshot of the post, supplied because this post type
cannot be read automatically. Treat the image as the primary source and read any
text in it. The caption below may contain nothing but the post URL.

"""


@dataclass(frozen=True)
class ParsedCaption:
    """Outcome of one caption parse."""

    ok: bool = False
    title: str | None = None
    location: str | None = None
    region: str | None = None
    event_start: str | None = None
    event_end: str | None = None
    is_evergreen: bool = True
    category: str = "other"
    subcategory: str = "other"
    tags: tuple[str, ...] = ()
    error: str | None = None


def _clean(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"null", "none", "n/a", "unknown"}:
        return None
    return text


def _clean_date(value: object, field: str) -> str | None:
    """Accept only a real YYYY-MM-DD date; anything else becomes None.

    Planning derives urgency tiers from these, so a plausible-looking but
    invalid string is worse than no date at all.
    """
    text = _clean(value)
    if text is None:
        return None
    if not _DATE_PATTERN.match(text):
        logger.warning("discarding malformed %s from model: %r", field, text)
        return None
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        logger.warning("discarding impossible %s from model: %r", field, text)
        return None
    return text


def _clean_taxonomy(payload: dict) -> tuple[str, str]:
    """Force category/subcategory into the closed taxonomy.

    Coercion is logged rather than silent: an unexpected value means the prompt
    and the taxonomy have drifted apart, and the symptom otherwise is links
    quietly missing from a filtered view.
    """
    category = (_clean(payload.get("category")) or "other").lower()
    if category not in CATEGORIES:
        logger.warning("model returned unknown category %r, using 'other'", category)
        category = "other"

    subcategory = (_clean(payload.get("subcategory")) or "other").lower()
    allowed = SUBCATEGORIES[category]
    if subcategory not in allowed:
        logger.warning(
            "model returned subcategory %r not valid for category %r, using 'other'",
            subcategory,
            category,
        )
        subcategory = "other"
    return category, subcategory


def _clean_tags(value: object) -> tuple[str, ...]:
    """Normalise tags to a short, de-duplicated, lowercase tuple.

    Commas are stripped from individual tags because the column stores them
    comma-separated; a tag containing one would corrupt the split on read.
    """
    if isinstance(value, str):
        # Tolerate a comma-separated string where an array was asked for.
        items = value.split(",")
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        if value is not None:
            logger.warning("model returned tags of unexpected type %s", type(value))
        return ()

    seen: list[str] = []
    for item in items:
        tag = _clean(item)
        if tag is None:
            continue
        tag = tag.lower().replace(",", " ").strip()
        if tag and tag not in seen:
            seen.append(tag)
    if len(seen) > MAX_TAGS:
        logger.info("trimming %d tags to %d", len(seen), MAX_TAGS)
    return tuple(seen[:MAX_TAGS])


def _extract_json(raw: str) -> dict | None:
    """Pull the JSON object out of a model response.

    JSON mode normally returns clean JSON, but a fenced block or stray prose
    should not lose an otherwise good answer.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return payload if isinstance(payload, dict) else None


def parse_caption(
    caption: str | None,
    api_key: str,
    model_name: str,
    title: str | None = None,
    platform: str | None = None,
    today: date | None = None,
    image_bytes: bytes | None = None,
    image_mime: str = "image/jpeg",
) -> ParsedCaption:
    """Blocking parse of one post. Never raises.

    When `image_bytes` is supplied the image is sent alongside the same prompt,
    in the *same* call - vision and text are one request, never two, because
    the free tier's daily quota is the binding constraint. The schema and
    taxonomy are identical either way, so both paths produce one shape.
    """
    source = (caption or "").strip() or (title or "").strip()
    if not source and not image_bytes:
        # Nothing to work with, and no reason to spend a call finding that out.
        logger.info("no caption, title or image to parse; skipping model call")
        return ParsedCaption(ok=True, error=None)

    prompt = PROMPT_TEMPLATE.format(
        today=(today or date.today()).isoformat(),
        platform=platform or "unknown",
        title=title or "(none)",
        caption=source[:4000] or "(none)",
        categories=" | ".join(CATEGORIES),
        taxonomy=_TAXONOMY_BLOCK,
        max_tags=MAX_TAGS,
        image_note=IMAGE_NOTE if image_bytes else "",
    )

    try:
        client = genai.Client(api_key=api_key)
        config = genai_types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0,
            # No tools are supplied, so automatic function calling would only
            # add a warning on every call.
            automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(
                disable=True
            ),
        )
    except Exception as exc:  # noqa: BLE001 - intake must survive any failure
        logger.exception("could not create Gemini client")
        return ParsedCaption(ok=False, error=f"{type(exc).__name__}: {exc}")

    # One request carries both the screenshot and the prompt.
    contents = (
        [genai_types.Part.from_bytes(data=image_bytes, mime_type=image_mime), prompt]
        if image_bytes
        else prompt
    )
    if image_bytes:
        logger.info("parsing with attached image (%d bytes, %s)", len(image_bytes), image_mime)

    raw = None
    for attempt in range(TRANSIENT_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=model_name, contents=contents, config=config
            )
            raw = (response.text or "").strip()
            break
        except (genai_errors.ClientError, genai_errors.ServerError) as exc:
            # ClientError covers 4xx; only 429 is worth retrying, since a 404
            # for a retired model will never succeed.
            status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
            retryable = isinstance(exc, genai_errors.ServerError) or status == 429
            if not retryable or attempt >= TRANSIENT_RETRIES:
                logger.warning("Gemini call failed (status=%s): %s", status, exc)
                return ParsedCaption(ok=False, error=f"{type(exc).__name__}: {exc}")
            logger.warning(
                "Gemini transient failure (status=%s), retrying in %ss",
                status,
                RETRY_BACKOFF_SECONDS,
            )
            time.sleep(RETRY_BACKOFF_SECONDS)
        except Exception as exc:  # noqa: BLE001 - intake must survive any failure
            logger.exception("Gemini caption parse failed")
            return ParsedCaption(ok=False, error=f"{type(exc).__name__}: {exc}")

    if raw is None:
        return ParsedCaption(ok=False, error="no response from model")

    payload = _extract_json(raw)
    if payload is None:
        logger.warning("could not read JSON from model response: %r", raw[:200])
        return ParsedCaption(ok=False, error="model did not return JSON")

    event_start = _clean_date(payload.get("event_start"), "event_start")
    event_end = _clean_date(payload.get("event_end"), "event_end")

    is_evergreen = payload.get("is_evergreen")
    if not isinstance(is_evergreen, bool):
        is_evergreen = str(is_evergreen).strip().lower() in {"true", "1", "yes"}
    # An end date and "evergreen" contradict each other; the date is the harder
    # fact, so it wins.
    if event_end:
        is_evergreen = False

    category, subcategory = _clean_taxonomy(payload)

    parsed = ParsedCaption(
        ok=True,
        title=_clean(payload.get("title")),
        location=_clean(payload.get("location")),
        region=_clean(payload.get("region")),
        event_start=event_start,
        event_end=event_end,
        is_evergreen=is_evergreen,
        category=category,
        subcategory=subcategory,
        tags=_clean_tags(payload.get("tags")),
    )
    logger.info(
        "parsed caption -> title=%r location=%r region=%r start=%s end=%s "
        "evergreen=%s category=%s/%s tags=%s",
        (parsed.title or "")[:60],
        (parsed.location or "")[:60],
        parsed.region,
        parsed.event_start,
        parsed.event_end,
        parsed.is_evergreen,
        parsed.category,
        parsed.subcategory,
        list(parsed.tags),
    )
    return parsed


async def parse_caption_async(
    caption: str | None,
    api_key: str,
    model_name: str,
    title: str | None = None,
    platform: str | None = None,
    today: date | None = None,
    image_bytes: bytes | None = None,
    image_mime: str = "image/jpeg",
) -> ParsedCaption:
    """Run `parse_caption` off the event loop so the bot stays responsive."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                parse_caption,
                caption,
                api_key,
                model_name,
                title,
                platform,
                today,
                image_bytes,
                image_mime,
            ),
            timeout=PARSE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning("caption parse timed out after %ss", PARSE_TIMEOUT_SECONDS)
        return ParsedCaption(ok=False, error=f"timed out after {PARSE_TIMEOUT_SECONDS}s")
