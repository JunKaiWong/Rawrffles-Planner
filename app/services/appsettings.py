"""Values the couple can change themselves, stored in the database.

Per the convention in CLAUDE.md, anything they might reasonably want different
lives here rather than as a constant in the source. The constants below are
only the defaults for a key nobody has set - changing one from the Mini App
takes effect without a redeploy.

Reads are cached for a few seconds. Every plan and every link listing consults
these, and re-querying per row would be silly; a few seconds' staleness after
someone saves a setting is imperceptible, and the cache is cleared on write
anyway so the person who changed it sees it immediately.
"""

import logging
import threading
import time
from dataclasses import dataclass

from app.db.database import get_all_settings

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 10

MAX_STOPS_KEY = "max_stops"
CLUSTER_RADIUS_KEY = "cluster_radius_metres"
HOME_REGION_KEY = "home_region"

DEFAULT_MAX_STOPS = 4
DEFAULT_CLUSTER_RADIUS_METRES = 2000
DEFAULT_HOME_REGION = "Singapore"

# Bounds exist because these are free-text fields in a phone UI, not because
# the values are sacred. A plan of 40 stops or a 500km "neighbourhood" is a
# typo, and failing loudly beats a nonsensical plan.
LIMITS = {
    MAX_STOPS_KEY: (1, 12),
    CLUSTER_RADIUS_KEY: (200, 50_000),
}


@dataclass(frozen=True)
class AppSettings:
    max_stops: int = DEFAULT_MAX_STOPS
    cluster_radius_metres: int = DEFAULT_CLUSTER_RADIUS_METRES
    home_region: str = DEFAULT_HOME_REGION


_lock = threading.Lock()
_cached: AppSettings | None = None
_cached_at: float = 0.0


def _coerce_int(raw: str | None, default: int, key: str) -> int:
    if raw is None:
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("setting %s is not a number (%r); using %s", key, raw, default)
        return default
    low, high = LIMITS.get(key, (None, None))
    if low is not None and not (low <= value <= high):
        logger.warning(
            "setting %s=%s is outside %s-%s; using %s", key, value, low, high, default
        )
        return default
    return value


def load(db_path) -> AppSettings:
    """Current settings, from cache when fresh."""
    global _cached, _cached_at
    with _lock:
        if _cached is not None and time.time() - _cached_at < CACHE_TTL_SECONDS:
            return _cached

    try:
        stored = {
            dict(r)["key"]: dict(r)["value"] for r in get_all_settings(db_path)
        }
    except Exception:
        # A settings table that cannot be read must not take planning down.
        logger.exception("could not read settings; falling back to defaults")
        stored = {}

    resolved = AppSettings(
        max_stops=_coerce_int(stored.get(MAX_STOPS_KEY), DEFAULT_MAX_STOPS, MAX_STOPS_KEY),
        cluster_radius_metres=_coerce_int(
            stored.get(CLUSTER_RADIUS_KEY),
            DEFAULT_CLUSTER_RADIUS_METRES,
            CLUSTER_RADIUS_KEY,
        ),
        home_region=(stored.get(HOME_REGION_KEY) or DEFAULT_HOME_REGION).strip()
        or DEFAULT_HOME_REGION,
    )
    with _lock:
        _cached, _cached_at = resolved, time.time()
    return resolved


def invalidate() -> None:
    """Drop the cache, so a save is visible to the next read."""
    global _cached, _cached_at
    with _lock:
        _cached, _cached_at = None, 0.0


def defaults() -> dict:
    return {
        MAX_STOPS_KEY: DEFAULT_MAX_STOPS,
        CLUSTER_RADIUS_KEY: DEFAULT_CLUSTER_RADIUS_METRES,
        HOME_REGION_KEY: DEFAULT_HOME_REGION,
    }
