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
        canonical = canonicalise(resolve_via_redirect(url))
        logger.info(
            "extraction failed for %s; canonical=%s (from redirect)", url, canonical
        )
        return Metadata(raw_url=url, ok=False, canonical_url=canonical, error=error)

    canonical = canonicalise(
        _clean(info.get("webpage_url")) or _clean(info.get("original_url"))
    ) or canonicalise(resolve_via_redirect(url))

    metadata = Metadata(
        raw_url=url,
        ok=True,
        canonical_url=canonical,
        title=_clean(info.get("title")),
        caption=_clean(info.get("description")),
        location=_location_from(info),
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
