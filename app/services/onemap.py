"""OneMap thematic layers: real venues near a point.

Search needs no authentication, but the thematic layers do, so this module owns
the token as well as the queries.

**Token handling.** OneMap issues a token that lasts about three days. It is
requested once, cached, and reused until shortly before it expires. Two things
matter beyond that:

* a 401 mid-run forces one refresh and a retry rather than failing the call - a
  token that expires between two requests of the same plan should recover, not
  take the plan down with it;
* every refresh, and every failure to refresh, is logged plainly. A silently
  dead token would make venue discovery quietly return nothing, which looks
  like "there is nothing nearby" rather than "authentication is broken".

**Theme choice is curated, not exhaustive.** OneMap publishes 166 themes and
most are wrong for a date: `historicsites` is mostly plaques and markers,
`nationalparks` includes unnamed grass verges under names like "GANGES AVE OS",
and `healthierdining` returned 132 results near Tanjong Pagar that were almost
entirely chains - McDonald's, Mr Bean. Suggesting those would technically
satisfy "a real place" while being useless advice. The themes kept are the ones
that name somewhere worth going.

Results are cached: hawker centres near a point do not change hourly, and a
planning run should not re-query the same neighbourhood.
"""

import logging
import threading
import time
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

TOKEN_URL = "https://www.onemap.gov.sg/api/auth/post/getToken"
THEME_URL = "https://www.onemap.gov.sg/api/public/themesvc/retrieveTheme"
TIMEOUT_SECONDS = 30

# Refresh this long before the stated expiry, so a request cannot set off with
# a token that dies in flight.
EXPIRY_MARGIN_SECONDS = 15 * 60
# Nearby amenities are stable; re-querying them within a day is waste.
CACHE_TTL_SECONDS = 24 * 60 * 60

# Themes worth suggesting, by the gap they fill. See the module docstring for
# why the rest of OneMap's catalogue is left out.
THEMES_BY_CATEGORY = {
    "food": ["ssot_hawkercentres"],
    "activity": ["tourism", "museum"],
}

_token_lock = threading.Lock()
_token: str | None = None
_token_expires_at: float = 0.0

_cache_lock = threading.Lock()
_cache: dict[tuple, tuple[float, list]] = {}


@dataclass(frozen=True)
class Place:
    """A venue returned by OneMap. Real by construction - it came from a search
    result, never from a model."""

    name: str
    lat: float
    lng: float
    theme: str
    address: str | None = None
    description: str | None = None


class OneMapAuthError(Exception):
    """Credentials are missing or rejected."""


def _request_token(email: str, password: str) -> tuple[str, float]:
    response = requests.post(
        TOKEN_URL, json={"email": email, "password": password}, timeout=TIMEOUT_SECONDS
    )
    if not response.ok:
        # Never log the body: it can echo the submitted credentials.
        raise OneMapAuthError(f"token request returned {response.status_code}")
    payload = response.json()
    token = payload.get("access_token")
    if not token:
        raise OneMapAuthError("token response contained no access_token")
    try:
        expires_at = float(payload.get("expiry_timestamp"))
    except (TypeError, ValueError):
        # Undated token: assume the documented three days, minus the margin.
        expires_at = time.time() + 3 * 24 * 3600
    return token, expires_at


def get_token(email: str | None, password: str | None, force: bool = False) -> str:
    """Return a usable token, refreshing when required.

    `force` bypasses the cache and is what a 401 triggers.
    """
    global _token, _token_expires_at
    if not email or not password:
        raise OneMapAuthError("ONEMAP_EMAIL and ONEMAP_PASSWORD are not configured")

    with _token_lock:
        fresh_enough = (
            _token is not None and time.time() < _token_expires_at - EXPIRY_MARGIN_SECONDS
        )
        if fresh_enough and not force:
            return _token

        reason = "forced by a 401" if force else ("expired" if _token else "none cached")
        logger.info("requesting a OneMap token (%s)", reason)
        try:
            token, expires_at = _request_token(email, password)
        except Exception as exc:
            # Loud on purpose: silent failure here looks like an empty city.
            logger.error("OneMap token refresh FAILED (%s): %s", reason, exc)
            raise
        _token, _token_expires_at = token, expires_at
        hours = (expires_at - time.time()) / 3600
        logger.info("OneMap token obtained, valid for about %.1f hours", hours)
        return _token


def reset_token_cache() -> None:
    """Drop the cached token. For tests and for forcing a clean retry."""
    global _token, _token_expires_at
    with _token_lock:
        _token, _token_expires_at = None, 0.0


def _extents(lat: float, lng: float, radius_metres: int) -> str:
    """A bounding box around a point, in the order OneMap expects."""
    # ~111km per degree of latitude; longitude is scaled by cos(lat), which at
    # Singapore's latitude is within a percent of 1, so it is treated as equal.
    delta = radius_metres / 111_000
    return f"{lat - delta:.6f},{lng - delta:.6f},{lat + delta:.6f},{lng + delta:.6f}"


def _parse_places(theme: str, payload: dict) -> list[Place]:
    places: list[Place] = []
    for item in payload.get("SrchResults") or []:
        name = (item.get("NAME") or "").strip()
        latlng = item.get("LatLng") or ""
        if not name or "," not in latlng:
            # The first element is a metadata record with no NAME; skip it.
            continue
        try:
            lat_text, lng_text = latlng.split(",")[:2]
            lat, lng = float(lat_text), float(lng_text)
        except (TypeError, ValueError):
            continue
        address_parts = [
            item.get("ADDRESSBLOCKHOUSENUMBER"),
            item.get("ADDRESSSTREETNAME"),
            item.get("ADDRESSBUILDINGNAME"),
        ]
        address = " ".join(p for p in address_parts if p) or None
        places.append(
            Place(
                name=name,
                lat=lat,
                lng=lng,
                theme=theme,
                address=address,
                description=(item.get("DESCRIPTION") or "").strip() or None,
            )
        )
    return places


def retrieve_theme(
    theme: str,
    lat: float,
    lng: float,
    radius_metres: int,
    email: str | None,
    password: str | None,
) -> list[Place]:
    """Places of one theme near a point. Never raises; returns [] on failure."""
    # Rounding the key means two nearby centroids share a cache entry, which is
    # the point - the answer is the same neighbourhood either way.
    key = (theme, round(lat, 3), round(lng, 3), radius_metres)
    now = time.time()
    with _cache_lock:
        cached = _cache.get(key)
        if cached and now - cached[0] < CACHE_TTL_SECONDS:
            logger.debug("theme cache hit for %s near %.3f,%.3f", theme, lat, lng)
            return cached[1]

    params = {"queryName": theme, "extents": _extents(lat, lng, radius_metres)}

    for attempt in range(2):
        try:
            token = get_token(email, password, force=(attempt == 1))
        except OneMapAuthError as exc:
            logger.error("cannot query theme %s: %s", theme, exc)
            return []
        except Exception:
            return []

        try:
            response = requests.get(
                THEME_URL, params=params, headers={"Authorization": token},
                timeout=TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            logger.warning("theme %s request failed: %s", theme, exc)
            return []

        if response.status_code == 401 and attempt == 0:
            # The token died between issue and use; refresh once and retry
            # rather than losing the plan over it.
            logger.warning("theme %s returned 401; refreshing the token and retrying", theme)
            continue
        if not response.ok:
            logger.warning("theme %s returned %s", theme, response.status_code)
            return []

        try:
            places = _parse_places(theme, response.json())
        except ValueError:
            logger.warning("theme %s returned non-JSON", theme)
            return []

        with _cache_lock:
            _cache[key] = (now, places)
        logger.info("theme %s near %.4f,%.4f -> %d place(s)", theme, lat, lng, len(places))
        return places

    logger.error("theme %s still unauthorised after a token refresh", theme)
    return []


def find_places(
    category: str,
    lat: float,
    lng: float,
    radius_metres: int,
    email: str | None,
    password: str | None,
    limit: int = 6,
) -> list[Place]:
    """Real venues near a point that fill a given category gap."""
    themes = THEMES_BY_CATEGORY.get(category, [])
    found: list[Place] = []
    for theme in themes:
        found.extend(
            retrieve_theme(theme, lat, lng, radius_metres, email, password)
        )
    # Closest first: a suggestion three streets away beats one across town.
    from app.services.geocoder import distance_metres

    found.sort(key=lambda p: distance_metres((lat, lng), (p.lat, p.lng)))
    return found[:limit]


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
