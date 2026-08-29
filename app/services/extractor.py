"""Link metadata extraction, wrapping yt-dlp.

Three things matter here beyond pulling fields out of yt-dlp:

1. Fail-soft. Extraction hits the public internet and fails routinely - private
   or deleted posts, Instagram login walls, rate limits, and TikTok *photo*
   posts, which yt-dlp rejects as an unsupported URL. Per the project
   conventions a failure must never lose the link, so every failure path
   returns Metadata(ok=False) with the raw URL intact.

2. Canonical URL resolution is deliberately independent of yt-dlp. yt-dlp does
   surface the canonical webpage_url, but only when extraction succeeds - and
   the share links we most need to resolve (vm./vt.tiktok.com) are exactly the
   ones that may point at unsupported post types. So the canonical URL is taken
   from yt-dlp when available and otherwise recovered by following HTTP
   redirects, meaning de-duplication keeps working even when metadata does not.

   The result is normalised (lowercased host, no www., no query or fragment) so
   the same post shared with different tracking parameters - Instagram's
   ?igsi=, TikTok's ?_r=/?_t= - collapses to one key.

3. Blocking. yt-dlp is synchronous and does multi-second network IO. Calling it
   from an async handler would stall the bot, so `extract_async()` runs it in a
   worker thread.
"""

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

import requests
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, ExtractorError

logger = logging.getLogger(__name__)

# Upper bound on one extraction. yt-dlp gets a shorter socket timeout so it
# usually gives up on its own first; this is the backstop.
EXTRACTION_TIMEOUT_SECONDS = 45
SOCKET_TIMEOUT_SECONDS = 15
REDIRECT_TIMEOUT_SECONDS = 10

# TikTok's public oEmbed endpoint: a single unauthenticated GET that returns the
# post's caption text, author and cover image. It is the fallback when yt-dlp's
# TikTok extractor breaks, which happens whenever TikTok changes their page
# structure and lasts until upstream patches it.
OEMBED_ENDPOINT = "https://www.tiktok.com/oembed"
OEMBED_TIMEOUT_SECONDS = 15
# A burst of requests earns a 429, so calls are serialised with a gap between
# them. This is a hard floor across threads, not a per-caller courtesy.
OEMBED_MIN_INTERVAL_SECONDS = 3.0
OEMBED_RETRY_AFTER_429_SECONDS = 20
THUMBNAIL_TIMEOUT_SECONDS = 15
MAX_THUMBNAIL_BYTES = 5_000_000

_oembed_lock = threading.Lock()
_oembed_last_call = 0.0

# Short-link hosts are plain redirectors; a browser-ish UA avoids being served
# a consent interstitial instead of the redirect.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@dataclass(frozen=True)
class Metadata:
    """Result of one extraction attempt.

    `raw_url` is always populated and `canonical_url` is often populated even
    when ok is False, so the caller can both store and de-duplicate the link
    regardless of whether metadata came back.
    """

    raw_url: str
    ok: bool = False
    canonical_url: str | None = None
    title: str | None = None
    caption: str | None = None
    location: str | None = None
    error: str | None = None
    # Which path produced the metadata: "yt-dlp", "oembed", or None. Worth
    # recording because the two differ in what they can return.
    source: str | None = None
    thumbnail_url: str | None = None
    # Cover image bytes, ready to hand to the vision parse alongside any
    # screenshots. Not persisted - only the URL is cheap to keep.
    thumbnail: bytes | None = None


class _YtdlpLogBridge:
    """Routes yt-dlp's internal chatter into our logger instead of stdout."""

    def debug(self, msg: str) -> None:
        logger.debug("yt-dlp: %s", msg)

    def info(self, msg: str) -> None:
        logger.debug("yt-dlp: %s", msg)

    def warning(self, msg: str) -> None:
        logger.debug("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        logger.debug("yt-dlp: %s", msg)


def _ydl_options() -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": SOCKET_TIMEOUT_SECONDS,
        "retries": 1,
        "logger": _YtdlpLogBridge(),
    }


def _clean(value: object) -> str | None:
    """Normalise a yt-dlp field to a non-empty string or None."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def canonicalise(url: str | None) -> str | None:
    """Reduce a URL to a stable de-duplication key.

    Drops the query string and fragment (tracking parameters), lowercases the
    scheme and host, strips a leading www., and removes any trailing slash.
    """
    if not url:
        return None
    try:
        parts = urlparse(url)
    except ValueError:
        logger.warning("could not parse url for canonicalisation: %s", url)
        return None
    if not parts.netloc:
        return None
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/") or "/"
    return urlunparse((parts.scheme.lower() or "https", host, path, "", "", ""))


def resolve_via_redirect(url: str) -> str | None:
    """Follow HTTP redirects to find where a share link actually points.

    Used when yt-dlp cannot parse the post but we still want a canonical URL
    for de-duplication.
    """
    headers = {"User-Agent": _USER_AGENT}
    try:
        # Some short-link hosts mishandle HEAD; fall back to a streamed GET,
        # which is closed before the body is read.
        response = requests.head(
            url, allow_redirects=True, timeout=REDIRECT_TIMEOUT_SECONDS, headers=headers
        )
        if response.status_code >= 400:
            logger.debug(
                "HEAD returned %s for %s, retrying with GET", response.status_code, url
            )
            with requests.get(
                url,
                allow_redirects=True,
                timeout=REDIRECT_TIMEOUT_SECONDS,
                headers=headers,
                stream=True,
            ) as get_response:
                resolved = get_response.url
        else:
            resolved = response.url
    except requests.RequestException as exc:
        logger.warning("redirect resolution failed for %s: %s", url, exc)
        return None
    if resolved and resolved != url:
        logger.info("redirect resolved %s -> %s", url, resolved)
    return resolved


def is_tiktok(url: str | None) -> bool:
    if not url:
        return False
    host = (urlparse(url).netloc or "").lower()
    return host == "tiktok.com" or host.endswith(".tiktok.com")


def to_www_tiktok(url: str) -> str:
    """oEmbed rejects the bare host with a 400, so restore the www. that
    canonicalisation strips."""
    parts = urlparse(url)
    if parts.netloc.lower() == "tiktok.com":
        parts = parts._replace(netloc="www.tiktok.com")
    return urlunparse(parts)


def _pace_oembed() -> None:
    """Serialise oEmbed calls with a minimum gap, across threads."""
    global _oembed_last_call
    with _oembed_lock:
        wait = OEMBED_MIN_INTERVAL_SECONDS - (time.monotonic() - _oembed_last_call)
        if wait > 0:
            logger.debug("pacing oembed: sleeping %.1fs", wait)
            time.sleep(wait)
        _oembed_last_call = time.monotonic()


def fetch_oembed(url: str) -> dict | None:
    """Ask TikTok's oEmbed endpoint about a post. Never raises.

    Returns None for anything it cannot answer, which includes photo/slideshow
    posts - those 400 here, so screenshots remain the only route for them.
    """
    target = to_www_tiktok(url)
    headers = {"User-Agent": _USER_AGENT}

    for attempt in range(2):
        _pace_oembed()
        try:
            response = requests.get(
                OEMBED_ENDPOINT,
                params={"url": target},
                headers=headers,
                timeout=OEMBED_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            logger.warning("oembed request failed for %s: %s", target, exc)
            return None

        if response.status_code == 429:
            if attempt == 0:
                logger.warning(
                    "oembed rate limited, retrying in %ss", OEMBED_RETRY_AFTER_429_SECONDS
                )
                time.sleep(OEMBED_RETRY_AFTER_429_SECONDS)
                continue
            logger.warning("oembed still rate limited for %s; giving up", target)
            return None
        if response.status_code == 400:
            # Expected for photo posts and removed videos.
            logger.info("oembed has no data for %s (400)", target)
            return None
        if not response.ok:
            logger.warning("oembed returned %s for %s", response.status_code, target)
            return None

        try:
            payload = response.json()
        except ValueError:
            logger.warning("oembed returned non-JSON for %s", target)
            return None
        if not isinstance(payload, dict):
            return None
        logger.info(
            "oembed hit for %s: author=%r title_len=%s",
            target,
            payload.get("author_name"),
            len(payload.get("title") or ""),
        )
        return payload
    return None


def fetch_thumbnail(url: str) -> bytes | None:
    """Download a cover image so it can go through the vision parse.

    A TikTok cover is the post's first frame and usually carries the on-screen
    text, which is exactly what the caption alone leaves out.
    """
    try:
        response = requests.get(
            url, headers={"User-Agent": _USER_AGENT}, timeout=THUMBNAIL_TIMEOUT_SECONDS
        )
        if not response.ok:
            logger.warning("thumbnail fetch returned %s", response.status_code)
            return None
        data = response.content
    except requests.RequestException as exc:
        logger.warning("thumbnail fetch failed: %s", exc)
        return None
    if not data or len(data) > MAX_THUMBNAIL_BYTES:
        logger.warning("thumbnail missing or too large (%d bytes)", len(data or b""))
        return None
    logger.info("downloaded thumbnail (%d bytes)", len(data))
    return data


def _from_oembed(url: str, canonical: str | None, error: str | None) -> Metadata | None:
    """Build Metadata from oEmbed, or None if it had nothing."""
    payload = fetch_oembed(canonical or url)
    if payload is None:
        return None

    # TikTok's oEmbed "title" is the post's caption text, not a separate
    # headline, so it serves as both.
    caption = _clean(payload.get("title"))
    thumbnail_url = _clean(payload.get("thumbnail_url"))
    thumbnail = fetch_thumbnail(thumbnail_url) if thumbnail_url else None

    if not caption and not thumbnail:
        logger.info("oembed returned nothing usable for %s", url)
        return None

    metadata = Metadata(
        raw_url=url,
        ok=True,
        canonical_url=canonical,
        title=caption,
        caption=caption,
        source="oembed",
        thumbnail_url=thumbnail_url,
        thumbnail=thumbnail,
        # Keep the yt-dlp error: the fallback worked, but knowing the primary
        # path is broken is what tells us when it recovers.
        error=error,
    )
    logger.info(
        "recovered %s via oembed: caption_len=%s thumbnail=%s",
        url,
        len(caption) if caption else 0,
        bool(thumbnail),
    )
    return metadata


def _location_from(info: dict) -> str | None:
    """Best-effort location.

    yt-dlp exposes 'location' only for some extractors; TikTok and Instagram
    usually leave it unset. Deriving a real place name from the caption is the
    LLM parsing step's job, not this one, so this returns None rather than
    guessing.
    """
    for key in ("location", "place", "venue"):
        found = _clean(info.get(key))
        if found:
            logger.debug("location taken from field %r", key)
            return found
    return None


def _run_ytdlp(url: str) -> tuple[dict | None, str | None]:
    """Return (info, error). Never raises."""
    try:
        with YoutubeDL(_ydl_options()) as ydl:
            info = ydl.sanitize_info(ydl.extract_info(url, download=False))
    except (DownloadError, ExtractorError) as exc:
        # Routine: private/removed posts, login walls, TikTok photo posts.
        logger.warning("yt-dlp could not extract %s: %s", url, exc)
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001 - must not propagate into intake
        logger.exception("unexpected yt-dlp error for %s", url)
        return None, f"{type(exc).__name__}: {exc}"
    if not isinstance(info, dict):
        logger.warning("yt-dlp returned no info dict for %s", url)
        return None, "no metadata returned"
    return info, None


def extract(url: str) -> Metadata:
    """Blocking extraction for one URL. Never raises."""
    logger.info("extracting metadata for %s", url)
    info, error = _run_ytdlp(url)

    if info is None:
        # Metadata is unavailable, but a canonical URL usually is not - resolve
        # it separately so de-duplication still works for this link.
        #
        # Falling back to the pasted URL matters more than it looks. Resolution
        # is a network call, so a timeout used to leave canonical_url NULL even
        # for a full-form URL that never needed resolving - and a NULL key
        # de-duplicates against nothing. Canonicalising the raw URL is pure
        # string work: for a full URL it is exactly the key the success path
        # would have produced, and for a share link it degrades to a stable
        # key for that link rather than to nothing. It can never merge two
        # different posts, because it is derived from the URL itself.
        canonical = canonicalise(resolve_via_redirect(url)) or canonicalise(url)
        logger.info(
            "yt-dlp extraction failed for %s; canonical=%s (from redirect)", url, canonical
        )
        # TikTok's extractor breaks whenever they change their pages. oEmbed is
        # a different, far simpler surface and usually still answers.
        if is_tiktok(canonical or url):
            recovered = _from_oembed(url, canonical, error)
            if recovered is not None:
                return recovered
        return Metadata(raw_url=url, ok=False, canonical_url=canonical, error=error)

    canonical = (
        canonicalise(_clean(info.get("webpage_url")) or _clean(info.get("original_url")))
        or canonicalise(resolve_via_redirect(url))
        # Same last resort as the failure branch above: a key derived from the
        # pasted URL always beats no key at all.
        or canonicalise(url)
    )

    metadata = Metadata(
        raw_url=url,
        ok=True,
        canonical_url=canonical,
        title=_clean(info.get("title")),
        caption=_clean(info.get("description")),
        location=_location_from(info),
        source="yt-dlp",
    )
    logger.info(
        "extracted %s -> canonical=%s title=%r location=%r caption_len=%s",
        url,
        metadata.canonical_url,
        (metadata.title or "")[:60],
        metadata.location,
        len(metadata.caption) if metadata.caption else 0,
    )
    return metadata


async def extract_async(url: str) -> Metadata:
    """Run `extract()` off the event loop so the bot stays responsive."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(extract, url), timeout=EXTRACTION_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        logger.warning(
            "extraction timed out after %ss for %s", EXTRACTION_TIMEOUT_SECONDS, url
        )
        return Metadata(
            raw_url=url, ok=False, error=f"timed out after {EXTRACTION_TIMEOUT_SECONDS}s"
        )
