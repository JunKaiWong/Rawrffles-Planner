"""Caption -> structured JSON, via Gemini.

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

import google.generativeai as genai

try:  # pragma: no cover - import shape varies with the client version
    from google.api_core.exceptions import ResourceExhausted
except ImportError:  # pragma: no cover
    ResourceExhausted = ()

logger = logging.getLogger(__name__)

PARSE_TIMEOUT_SECONDS = 90
# The Gemini free tier allows only a handful of requests per minute, and a
# burst of pasted links hits that ceiling immediately. One retry recovers the
# common case without turning a rate limit into a lost parse.
RATE_LIMIT_RETRIES = 1
RATE_LIMIT_BACKOFF_SECONDS = 20
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

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
  "is_evergreen": true or false
}}

Rules:
- is_evergreen is true for permanent things (a restaurant, a park, a shop) and
  false for anything with an end date (a pop-up, a festival, a limited menu).
- Use null when the caption does not say. Do NOT guess a location, a country or
  a date that is not supported by the caption.
- Do not infer a country from your own knowledge of a venue name; only use what
  the caption states or clearly implies (e.g. an explicit city or country).

Platform: {platform}
Post title: {title}
Caption:
\"\"\"
{caption}
\"\"\"
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
) -> ParsedCaption:
    """Blocking parse of one caption. Never raises."""
    source = (caption or "").strip() or (title or "").strip()
    if not source:
        # Photo posts often have no caption at all. Nothing to parse, and no
        # reason to spend a call finding that out again later.
        logger.info("no caption or title to parse; skipping model call")
        return ParsedCaption(ok=True, error=None)

    prompt = PROMPT_TEMPLATE.format(
        today=(today or date.today()).isoformat(),
        platform=platform or "unknown",
        title=title or "(none)",
        caption=source[:4000],
    )

    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(
            model_name,
            generation_config={
                "response_mime_type": "application/json",
                "temperature": 0,
            },
        )
    except Exception as exc:  # noqa: BLE001 - intake must survive any failure
        logger.exception("could not configure Gemini")
        return ParsedCaption(ok=False, error=f"{type(exc).__name__}: {exc}")

    raw = None
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        try:
            response = model.generate_content(prompt)
            raw = (response.text or "").strip()
            break
        except ResourceExhausted as exc:
            if attempt >= RATE_LIMIT_RETRIES:
                logger.warning("Gemini rate limit hit, giving up: %s", exc)
                return ParsedCaption(ok=False, error=f"rate limited: {exc}")
            logger.warning(
                "Gemini rate limit hit, retrying in %ss", RATE_LIMIT_BACKOFF_SECONDS
            )
            time.sleep(RATE_LIMIT_BACKOFF_SECONDS)
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

    parsed = ParsedCaption(
        ok=True,
        title=_clean(payload.get("title")),
        location=_clean(payload.get("location")),
        region=_clean(payload.get("region")),
        event_start=event_start,
        event_end=event_end,
        is_evergreen=is_evergreen,
    )
    logger.info(
        "parsed caption -> title=%r location=%r region=%r start=%s end=%s evergreen=%s",
        (parsed.title or "")[:60],
        (parsed.location or "")[:60],
        parsed.region,
        parsed.event_start,
        parsed.event_end,
        parsed.is_evergreen,
    )
    return parsed


async def parse_caption_async(
    caption: str | None,
    api_key: str,
    model_name: str,
    title: str | None = None,
    platform: str | None = None,
    today: date | None = None,
) -> ParsedCaption:
    """Run `parse_caption` off the event loop so the bot stays responsive."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                parse_caption, caption, api_key, model_name, title, platform, today
            ),
            timeout=PARSE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning("caption parse timed out after %ss", PARSE_TIMEOUT_SECONDS)
        return ParsedCaption(ok=False, error=f"timed out after {PARSE_TIMEOUT_SECONDS}s")
