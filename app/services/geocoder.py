"""Turn a location string into coordinates using OneMap search.

OneMap is Singapore-only, free, and needs no authentication for search. It is
also fussy in a way that matters: the *more* precise the string, the more likely
a naive lookup fails.

Measured against real stored locations:

    "63 Chulia St, 01-01, Singapore"   -> found=0
    "63 Chulia St"                     -> found=1, OCBC Centre East
    "531A Upper Cross St, Hong Lim Food Centre #02-38, Singapore 051531"
                                       -> found=0
    "051531"                           -> found=1, Hong Lim Complex
    "Hong Lim Food Centre"             -> found=1

So a unit number or a trailing ", Singapore" is enough to reduce a perfectly
good address to nothing. Rather than pass the string through and record a
failure, this tries a ladder of progressively simpler queries derived from it,
most precise first, and stops at the first confident answer.

The opposite failure also exists. "Orchard" returns 59 results spread over
kilometres and "McDonald's" returns a dozen branches across the island; there
is no single correct coordinate for either. Those are reported as ambiguous
with no coordinates rather than silently resolved to whichever branch happened
to sort first - a plan built around the wrong McDonald's is discovered in
person, which is exactly the failure the grounding rule exists to prevent.
"""

import logging
import math
import re
import threading
import time
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

SEARCH_URL = "https://www.onemap.gov.sg/api/common/elastic/search"
TIMEOUT_SECONDS = 20

# Politeness rather than a documented limit: a shared floor across threads, so
# a burst of intakes cannot hammer a free public service.
MIN_INTERVAL_SECONDS = 1.0

# Results this close together describe one place under several names, so the
# first is safe to use. Wider than this and the string is genuinely ambiguous.
CLUSTER_RADIUS_METRES = 750
# Only the leading results decide ambiguity; a long tail of weak matches is
# normal even for a good query.
RESULTS_CONSIDERED = 5

# Outcomes stored on the row, so "never tried" stays distinguishable from
# "tried and could not".
OK = "ok"
NOT_FOUND = "not_found"
AMBIGUOUS = "ambiguous"
OUTSIDE_REGION = "outside_region"
NO_LOCATION = "no_location"
ERROR = "error"

_lock = threading.Lock()
_last_call = 0.0


@dataclass(frozen=True)
class GeocodeResult:
    status: str
    lat: float | None = None
    lng: float | None = None
    matched: str | None = None
    query_used: str | None = None
    candidates: int = 0

    @property
    def ok(self) -> bool:
        return self.status == OK


def _pace() -> None:
    global _last_call
    with _lock:
        wait = MIN_INTERVAL_SECONDS - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


def distance_metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Haversine. Singapore-scale distances, so precision here is ample."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def _clean(text: str) -> str:
    """Strip the parts of an address that defeat OneMap's search."""
    out = text
    out = re.sub(r"#\s*\d+\s*-\s*\d+", " ", out)          # unit: #02-38
    out = re.sub(r"\b\d{2}\s*-\s*\d{2}\b", " ", out)      # unit: 01-01
    out = re.sub(r"\bS\s*\d{6}\b", " ", out, flags=re.I)  # postal: S 089587
    out = re.sub(r"\bSingapore\b\s*\d{0,6}", " ", out, flags=re.I)
    out = re.sub(r"[,\s]+", " ", out)
    return out.strip(" ,")


def build_queries(location: str) -> list[str]:
    """Candidate lookups for one location string, most precise first."""
    queries: list[str] = []

    def add(candidate: str) -> None:
        candidate = candidate.strip(" ,")
        if len(candidate) >= 4 and candidate.lower() not in {q.lower() for q in queries}:
            queries.append(candidate)

    # A postal code identifies a building outright, so it is tried first.
    for postal in re.findall(r"\b\d{6}\b", location):
        add(postal)

    cleaned = _clean(location)
    add(cleaned)

    # Then each comma-separated part: typically the street address, then the
    # building name, either of which OneMap resolves on its own.
    for part in location.split(","):
        add(_clean(part))

    return queries


def _search(query: str) -> list[dict]:
    _pace()
    try:
        response = requests.get(
            SEARCH_URL,
            params={
                "searchVal": query,
                "returnGeom": "Y",
                "getAddrDetails": "Y",
                "pageNum": 1,
            },
            timeout=TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        logger.warning("OneMap request failed for %r: %s", query, exc)
        raise
    if not response.ok:
        logger.warning("OneMap returned %s for %r", response.status_code, query)
        return []
    try:
        payload = response.json()
    except ValueError:
        logger.warning("OneMap returned non-JSON for %r", query)
        return []
    return payload.get("results") or []


def _coords(result: dict) -> tuple[float, float] | None:
    try:
        return float(result["LATITUDE"]), float(result["LONGITUDE"])
    except (KeyError, TypeError, ValueError):
        return None


def _assess(results: list[dict]) -> tuple[bool, int]:
    """(is_confident, spread_in_metres) for a result set."""
    points = [c for c in (_coords(r) for r in results[:RESULTS_CONSIDERED]) if c]
    if not points:
        return False, 0
    if len(points) == 1:
        return True, 0
    spread = max(
        distance_metres(points[i], points[j])
        for i in range(len(points))
        for j in range(i + 1, len(points))
    )
    return spread <= CLUSTER_RADIUS_METRES, int(spread)


def geocode(location: str | None, region: str | None = None) -> GeocodeResult:
    """Resolve one location string. Never raises."""
    if not location or not location.strip():
        return GeocodeResult(status=NO_LOCATION)

    # OneMap covers Singapore only. Asking it about Johor Bahru wastes a request
    # to learn nothing, and the link keeps its day-trip flag either way.
    if region and region.strip().lower() != "singapore":
        logger.info("skipping geocode for %r: region=%s is outside coverage", location[:50], region)
        return GeocodeResult(status=OUTSIDE_REGION)

    queries = build_queries(location)
    logger.info("geocoding %r via %d candidate query/queries", location[:60], len(queries))

    ambiguous_seen = False
    for query in queries:
        try:
            results = _search(query)
        except requests.RequestException:
            return GeocodeResult(status=ERROR, query_used=query)

        if not results:
            logger.debug("  %r -> no results", query)
            continue

        confident, spread = _assess(results)
        if not confident:
            logger.info(
                "  %r -> %d results spread over ~%dm, ambiguous", query, len(results), spread
            )
            ambiguous_seen = True
            continue

        point = _coords(results[0])
        if point is None:
            continue
        matched = (results[0].get("SEARCHVAL") or "").strip() or None
        logger.info("  %r -> %s at %.6f,%.6f", query, matched, point[0], point[1])
        return GeocodeResult(
            status=OK,
            lat=point[0],
            lng=point[1],
            matched=matched,
            query_used=query,
            candidates=len(results),
        )

    # Every query either found nothing or found too many. Ambiguity is the more
    # informative of the two, so it wins when both occurred.
    status = AMBIGUOUS if ambiguous_seen else NOT_FOUND
    logger.info("geocode of %r ended as %s", location[:60], status)
    return GeocodeResult(status=status)
