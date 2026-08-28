"""Build a date plan from saved links.

The division of labour is deliberate and is the whole design:

* **Code decides what is possible.** Which links are still valid, how urgent
  each one is, and which ones are near enough to visit in one outing. Distance
  and dates are deterministic; asking a model to "keep things close together"
  from raw addresses produces confident nonsense.
* **The model decides only the arrangement.** Given a shortlist that is already
  geographically coherent, it chooses an order, times, and a sentence about why
  the day hangs together.

**Grounding is enforced structurally, not by asking nicely.** The model is
given link ids and may only refer to stops by id; any id it did not receive is
dropped. The rendered plan takes every venue name from the database, so even a
model that invents "Marina Bay Sands" in its reasoning cannot get that name in
front of the user as a stop. A plan that sends someone to a place that does not
exist fails in person, on the day, which is the one failure worth designing
against.
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from app.services.geocoder import distance_metres

logger = logging.getLogger(__name__)

# Two stops within this distance are treated as one outing. Roughly a short
# bus ride or a long walk; wide enough to pair a meal with something afterwards,
# tight enough that the day is not spent travelling.
CLUSTER_RADIUS_METRES = 2000

# Urgency tiers, computed here and passed to the model as facts.
URGENT_DAYS = 7
SOON_DAYS = 30
TIER_URGENT, TIER_SOON, TIER_FLEXIBLE = "urgent", "soon", "flexible"
_TIER_WEIGHT = {TIER_URGENT: 100, TIER_SOON: 25, TIER_FLEXIBLE: 5}

# Default only. The real value is a setting the couple can change in the Mini
# App; see app/services/appsettings.py.
MAX_STOPS = 4
# Above this, a "day" is really a list. Used to warn rather than to refuse,
# because an explicit "include everything" is a deliberate choice.
LONG_PLAN_WARNING_STOPS = 6
PLAN_TIMEOUT_SECONDS = 90
TRANSIENT_RETRIES = 2
RETRY_BACKOFF_SECONDS = 15


@dataclass(frozen=True)
class Candidate:
    id: int
    title: str
    url: str
    category: str | None
    subcategory: str | None
    tags: str | None
    location: str | None
    lat: float
    lng: float
    tier: str
    event_end: str | None
    rating: int | None
    # "saved" for a link the couple chose themselves, "discovered" for a real
    # venue found nearby to fill a gap. Kept distinct all the way to the output:
    # one has been vetted by them, the other has not.
    source: str = "saved"
    # Stable handle used in the prompt. Saved links use their row id;
    # discovered venues use "d1", "d2", so the two can never be confused.
    key: str = ""

    def handle(self) -> str:
        return self.key or str(self.id)


@dataclass
class Stop:
    link_id: int | None
    title: str
    url: str | None
    location: str | None
    when: str | None = None
    why: str | None = None
    source: str = "saved"


@dataclass
class Plan:
    ok: bool
    # Things the user should know about the plan they asked for, rather than
    # reasons it failed - an unrealistically long day is still a valid answer.
    warnings: list[str] = field(default_factory=list)
    stops: list[Stop] = field(default_factory=list)
    summary: str | None = None
    error: str | None = None
    dropped: list[str] = field(default_factory=list)
    cluster_size: int = 0
    # Links the caller asked for that could not be planned around, with why.
    # Silently ignoring a chosen link would look like the app lost it.
    excluded: dict[int, str] = field(default_factory=dict)
    # How far apart the chosen stops are, so a hand-picked selection spread
    # across the island can say so rather than pretending to be an outing.
    spread_metres: int = 0

    def render(self) -> str:
        """The message the user sees.

        Venue names come from stored data, never from the model's text.
        """
        if not self.ok:
            return f"Could not build a plan: {self.error}"
        lines = []
        if self.summary:
            lines += [self.summary, ""]
        for index, stop in enumerate(self.stops, 1):
            when = f"{stop.when} - " if stop.when else ""
            # Say plainly which stops they picked and which the app found, so a
            # suggestion is never mistaken for something already vetted.
            marker = "" if stop.source == "saved" else "  [suggested]"
            lines.append(f"{index}. {when}{stop.title}{marker}")
            if stop.location:
                lines.append(f"   {stop.location}")
            if stop.why:
                lines.append(f"   {stop.why}")
            if stop.url:
                lines.append(f"   {stop.url}")
            elif stop.source != "saved":
                lines.append("   (not one of your saved links - found nearby)")
        return "\n".join(lines)


def urgency_tier(event_end: str | None, is_evergreen: bool, today: date) -> str | None:
    """Tier for one link, or None when it has already expired.

    Computed here rather than in the prompt: the model is inconsistent about
    date arithmetic and the result is hard to debug after the fact.
    """
    if not event_end:
        return TIER_FLEXIBLE if is_evergreen else TIER_FLEXIBLE
    try:
        ends = date.fromisoformat(event_end)
    except (TypeError, ValueError):
        return TIER_FLEXIBLE
    days = (ends - today).days
    if days < 0:
        return None
    if days <= URGENT_DAYS:
        return TIER_URGENT
    if days <= SOON_DAYS:
        return TIER_SOON
    return TIER_FLEXIBLE


def select_candidates(rows, today: date | None = None) -> list[Candidate]:
    """Links that could appear in a plan today.

    Excluded: anything done, anything expired, anything without coordinates
    (it cannot be placed in a cluster), and day trips, which are kept and
    browsable but are not an MRT stop away.
    """
    today = today or date.today()
    candidates: list[Candidate] = []
    for row in rows:
        data = dict(row)
        if data.get("done"):
            continue
        if data.get("lat") is None or data.get("lng") is None:
            continue
        tier = urgency_tier(
            data.get("event_end"), bool(data.get("is_evergreen")), today
        )
        if tier is None:
            logger.debug("id=%s expired on %s, excluded", data["id"], data.get("event_end"))
            continue
        candidates.append(
            Candidate(
                id=int(data["id"]),
                title=(data.get("title") or data.get("url") or "").strip(),
                url=data.get("url") or "",
                category=data.get("category"),
                subcategory=data.get("subcategory"),
                tags=data.get("tags"),
                location=data.get("location"),
                lat=float(data["lat"]),
                lng=float(data["lng"]),
                tier=tier,
                event_end=data.get("event_end"),
                rating=data.get("rating"),
            )
        )
    logger.info("%d candidate(s) after filtering", len(candidates))
    return candidates


def cluster_by_proximity(
    candidates: list[Candidate], radius_metres: int = CLUSTER_RADIUS_METRES
) -> list[list[Candidate]]:
    """Single-linkage grouping: two stops join a cluster if either is within
    `radius_metres` of any member.

    Single linkage suits an itinerary - a chain of nearby stops is walkable
    even when its ends are further apart than the radius - and at this data
    size the quadratic comparison is free.
    """
    unassigned = list(candidates)
    clusters: list[list[Candidate]] = []

    while unassigned:
        seed = unassigned.pop(0)
        group = [seed]
        changed = True
        while changed:
            changed = False
            for other in list(unassigned):
                if any(
                    distance_metres((m.lat, m.lng), (other.lat, other.lng)) <= radius_metres
                    for m in group
                ):
                    group.append(other)
                    unassigned.remove(other)
                    changed = True
        clusters.append(group)

    clusters.sort(key=score_cluster, reverse=True)
    logger.info(
        "clustered into %s", [f"{len(c)} stop(s)" for c in clusters] or "nothing"
    )
    return clusters


def score_cluster(cluster: list[Candidate]) -> float:
    """How promising a cluster is as the basis for one outing.

    Urgency dominates, because an expiring link is the only thing here with a
    deadline. Variety and past ratings break ties: a food stop plus an activity
    is a day out, whereas three cafes within one block is one errand.
    """
    score = sum(_TIER_WEIGHT.get(c.tier, 0) for c in cluster)
    categories = {c.category for c in cluster if c.category and c.category != "other"}
    score += 15 * max(0, len(categories) - 1)
    score += min(len(cluster), MAX_STOPS) * 3
    rated = [c.rating for c in cluster if c.rating]
    if rated:
        score += sum(rated) / len(rated)
    return score


def _shortlist(cluster: list[Candidate], max_stops: int = MAX_STOPS) -> list[Candidate]:
    """Trim a cluster to what one day can hold, urgent items first."""
    ordered = sorted(
        cluster, key=lambda c: (-_TIER_WEIGHT.get(c.tier, 0), -(c.rating or 0), c.id)
    )
    return ordered[:max_stops]


PROMPT_TEMPLATE = """\
You are arranging a Saturday outing for a couple in Singapore.

Today is {today}.

Below is a shortlist of places they have already saved. The shortlist has
ALREADY been checked: every place is real, currently valid, and close enough to
the others to visit in one day. Distances and deadlines were computed before
you were asked.

Your job is ONLY to arrange them: choose a sensible order and rough times, and
explain briefly why the day works.

STRICT RULES:
- Use ONLY the places listed below, referenced by their exact "id".
- Do NOT introduce any other venue, restaurant, cafe, attraction or landmark,
  not even as a suggestion or an aside.
- Places marked SUGGESTED are real nearby venues the couple has not been to.
  They may be used, but do not claim they have been there or that they are
  favourites.
- Do not invent addresses, opening hours, prices or menu items.
- Items marked urgent end soon; build the day around them.

Places:
{places}

Return ONLY a JSON object:
{{
  "summary": "one or two sentences about the day as a whole",
  "stops": [
    {{"id": <id from the list>, "when": "e.g. 11:00am", "why": "one short sentence"}}
  ]
}}
Order "stops" in visiting order. {stop_count_rule}
"""


def _describe_place(candidate: Candidate) -> str:
    origin = "SAVED by them" if candidate.source == "saved" else "SUGGESTED (found nearby, not yet visited)"
    bits = [f'  - id {candidate.handle()}: "{candidate.title}"  [{origin}]']
    if candidate.category:
        kind = candidate.category
        if candidate.subcategory and candidate.subcategory != "other":
            kind += f"/{candidate.subcategory}"
        bits.append(f"    type: {kind}")
    if candidate.location:
        bits.append(f"    location: {candidate.location}")
    if candidate.tags:
        bits.append(f"    tags: {candidate.tags}")
    bits.append(f"    urgency: {candidate.tier}")
    if candidate.event_end:
        bits.append(f"    ends: {candidate.event_end}")
    if candidate.rating:
        bits.append(f"    they rated a previous visit {candidate.rating}/10")
    return "\n".join(bits)


# Capitalised words that are not venue names, so a phrase made only of these
# is not a smuggled place. Without this, "Saturday Morning" would read as a
# proper noun and cost a perfectly good sentence.
_GENERIC_CAPS = {
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
    "singapore", "mrt", "morning", "afternoon", "evening", "night", "lunch",
    "dinner", "brunch", "breakfast", "coffee", "dessert", "start", "then",
    "next", "finally", "first", "after", "afterwards", "end", "the", "a", "of",
    "and", "at", "in", "on", "for", "with", "you", "your", "it", "this", "that",
    "day", "date", "walk", "head", "stop", "grab", "wrap", "close", "begin",
}

_PROPER_NOUN = re.compile(r"\b(?:[A-Z][\w'&-]*)(?:\s+(?:[A-Z][\w'&-]*|of|and|the))+\b")


def _scrub_prose(text: str | None, allowed: str) -> str | None:
    """Drop model prose that names a place we did not supply.

    Enforcing grounding on stop ids alone is not enough: a response can carry a
    perfectly valid set of ids and still write "start at Marina Bay Sands" in
    its summary, which reaches the user as a real instruction. Any multi-word
    proper noun that does not appear in the shortlist's own titles or addresses
    costs the sentence - the plan still renders, with its grounded stops, minus
    the invented detail.
    """
    if not text:
        return None
    haystack = allowed.lower()
    for match in _PROPER_NOUN.findall(text):
        words = [w for w in re.split(r"\s+", match) if w]
        # A venue name needs at least two capitalised words. Without this, a
        # sentence opening like "Explore the exhibits" reads as a proper noun -
        # the capital is just the start of the sentence - and costs a perfectly
        # good line.
        if sum(1 for w in words if w[:1].isupper()) < 2:
            continue
        if all(w.lower() in _GENERIC_CAPS for w in words):
            continue
        # A match can swallow the words in front of a name - "Visit the Asian
        # Civilisations Museum" - so the name is taken as the trailing run of
        # capitalised words, which stops at the lowercase "the". Checking
        # arbitrary suffixes instead would be too lax: "Food Centre" is a
        # substring of an allowed name, which would wave through any invented
        # "<somewhere> Food Centre".
        trailing: list[str] = []
        for word in reversed(words):
            if not word[:1].isupper():
                break
            trailing.insert(0, word)
        name = " ".join(trailing) if len(trailing) >= 2 else match
        if match.lower() in haystack or name.lower() in haystack:
            continue
        logger.warning(
            "dropping ungrounded prose: %r mentions %r, which is not in the shortlist",
            text[:80],
            match,
        )
        return None
    return text


def _extract_json(raw: str) -> dict | None:
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


def _call_model(prompt: str, api_key: str, model_name: str) -> tuple[str | None, str | None]:
    """(raw_text, error). Never raises."""
    try:
        client = genai.Client(api_key=api_key)
        config = genai_types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.4,
            automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(
                disable=True
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("could not create Gemini client")
        return None, f"{type(exc).__name__}: {exc}"

    for attempt in range(TRANSIENT_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=model_name, contents=prompt, config=config
            )
            return (response.text or "").strip(), None
        except (genai_errors.ClientError, genai_errors.ServerError) as exc:
            status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
            retryable = isinstance(exc, genai_errors.ServerError) or status == 429
            if not retryable or attempt >= TRANSIENT_RETRIES:
                return None, f"{type(exc).__name__}: {exc}"
            logger.warning("planner call transient failure (%s), retrying", status)
            time.sleep(RETRY_BACKOFF_SECONDS)
        except Exception as exc:  # noqa: BLE001
            logger.exception("planner call failed")
            return None, f"{type(exc).__name__}: {exc}"
    return None, "no response from model"


GAP_SEARCH_RADIUS_METRES = 1200
MAX_DISCOVERED = 4
# A discovered venue this close to a chosen stop is almost certainly the same
# place under another name, and suggesting it back would be noise.
DUPLICATE_RADIUS_METRES = 120


def _centroid(candidates: list[Candidate]) -> tuple[float, float]:
    return (
        sum(c.lat for c in candidates) / len(candidates),
        sum(c.lng for c in candidates) / len(candidates),
    )


def find_gap_fillers(
    shortlist: list[Candidate],
    settings,
    radius_metres: int = GAP_SEARCH_RADIUS_METRES,
    max_stops: int = MAX_STOPS,
) -> list[Candidate]:
    """Real venues near the shortlist that fill a missing category.

    A day of three cafes is not an outing, and a day with nowhere to eat is not
    one either. Where the saved links do not cover food or an activity, OneMap
    is asked for real places near the centroid - never the model, which would
    happily invent something plausible and closed.
    """
    if not shortlist:
        return []

    email = getattr(settings, "onemap_email", None)
    password = getattr(settings, "onemap_password", None)
    if not email or not password:
        logger.info("no OneMap credentials; skipping venue discovery")
        return []

    present = {c.category for c in shortlist if c.category}
    gaps = [category for category in ("food", "activity") if category not in present]
    if not gaps:
        logger.info("shortlist already covers food and activity; no gaps to fill")
        return []

    lat, lng = _centroid(shortlist)
    logger.info(
        "filling gap(s) %s near centroid %.5f,%.5f (radius %dm)", gaps, lat, lng, radius_metres
    )

    from app.services.onemap import find_places

    discovered: list[Candidate] = []
    for category in gaps:
        if len(discovered) >= MAX_DISCOVERED:
            break
        for place in find_places(
            category, lat, lng, radius_metres, email, password, limit=MAX_DISCOVERED
        ):
            # Skip anything that is effectively a stop they already chose.
            if any(
                distance_metres((place.lat, place.lng), (c.lat, c.lng))
                <= DUPLICATE_RADIUS_METRES
                for c in shortlist
            ):
                continue
            discovered.append(
                Candidate(
                    id=-1,
                    title=place.name,
                    url="",
                    category=category,
                    subcategory=place.theme,
                    tags=None,
                    location=place.address or place.description,
                    lat=place.lat,
                    lng=place.lng,
                    tier=TIER_FLEXIBLE,
                    event_end=None,
                    rating=None,
                    source="discovered",
                    key=f"d{len(discovered) + 1}",
                )
            )
            break  # one suggestion per gap is enough to round out a day

    logger.info(
        "discovered %d venue(s) to fill gaps: %s",
        len(discovered),
        [(c.key, c.title[:30]) for c in discovered],
    )
    return discovered


def _spread(candidates: list[Candidate]) -> int:
    if len(candidates) < 2:
        return 0
    return int(
        max(
            distance_metres((a.lat, a.lng), (b.lat, b.lng))
            for i, a in enumerate(candidates)
            for b in candidates[i + 1 :]
        )
    )


def _explain_exclusions(rows, chosen: set[int], usable: set[int], today: date) -> dict[int, str]:
    """Why a explicitly chosen link did not make it into the plan."""
    reasons: dict[int, str] = {}
    for row in rows:
        data = dict(row)
        link_id = int(data["id"])
        if link_id not in chosen or link_id in usable:
            continue
        if data.get("done"):
            reasons[link_id] = "already done"
        elif data.get("lat") is None:
            reasons[link_id] = f"no coordinates ({data.get('geocode_status') or 'not geocoded'})"
        elif urgency_tier(data.get("event_end"), bool(data.get("is_evergreen")), today) is None:
            reasons[link_id] = "expired"
        else:
            reasons[link_id] = "not eligible"
    return reasons


def plan_date(
    rows,
    api_key: str,
    model_name: str,
    today: date | None = None,
    radius_metres: int | None = None,
    link_ids: list[int] | None = None,
    settings=None,
    include_all: bool = False,
) -> Plan:
    """The single seam for the planning LLM call. Never raises.

    With `link_ids`, the caller's selection is honoured as-is rather than
    re-clustered: someone who picked three places has already decided they
    belong together, and quietly dropping two of them for being 3km apart would
    be the app overruling a deliberate choice. Their spread is measured and
    reported instead.

    `include_all` is a per-plan decision rather than a preference: it says "put
    everything eligible in this one plan" and overrides the configured stop
    limit. An unrealistically long result is flagged, not refused - the caller
    asked for it deliberately.
    """
    today = today or date.today()

    # How many stops, and how wide a cluster, are settings the couple can
    # change from the Mini App, so they are read rather than compiled in.
    limit = MAX_STOPS
    if settings is not None:
        try:
            from app.services.appsettings import load as load_app_settings

            configured = load_app_settings(settings.db_path)
            limit = configured.max_stops
            if radius_metres is None:
                radius_metres = configured.cluster_radius_metres
        except Exception:
            logger.exception("could not read app settings; using defaults")
    if radius_metres is None:
        radius_metres = CLUSTER_RADIUS_METRES

    warnings: list[str] = []

    candidates = select_candidates(rows, today)
    if link_ids:
        chosen = {int(i) for i in link_ids}
        usable = [c for c in candidates if c.id in chosen]
        excluded = _explain_exclusions(rows, chosen, {c.id for c in usable}, today)
        if not usable:
            return Plan(
                ok=False,
                error="none of the selected links can be planned around",
                excluded=excluded,
            )
        shortlist = usable if include_all else _shortlist(usable, limit)
        for c in usable:
            if c not in shortlist:
                excluded[c.id] = f"only {limit} stops fit in one day"
        cluster_size = len(usable)
        logger.info("planning around a selection of %d link(s)", len(shortlist))
    else:
        if not candidates:
            return Plan(ok=False, error="no candidate links with coordinates")
        clusters = cluster_by_proximity(candidates, radius_metres)
        best = clusters[0]
        if include_all:
            # Everything eligible, not just the best cluster - the point of
            # asking for all of them is to see them all.
            shortlist = sorted(
                candidates, key=lambda c: (-_TIER_WEIGHT.get(c.tier, 0), c.id)
            )
        else:
            shortlist = _shortlist(best, limit)
        excluded = {}
        cluster_size = len(best)
        logger.info(
            "planning around %d stop(s): %s",
            len(shortlist),
            [(c.id, c.title[:24], c.tier) for c in shortlist],
        )

    # Round the day out with real nearby venues where the saved links leave a
    # gap. These are search results, never model inventions, and stay marked as
    # unvetted suggestions all the way to the output.
    if settings is not None:
        try:
            fillers = [] if include_all else find_gap_fillers(shortlist, settings, max_stops=limit)
            if fillers:
                # A full shortlist with a gap in it is the case this feature
                # exists for - four cafes in a row is not an outing. Make room
                # by dropping the lowest-priority saved stop, which stays saved
                # for another day, rather than declining to fill the gap.
                while len(shortlist) + len(fillers) > limit and len(shortlist) > 1:
                    demoted = shortlist[-1]
                    logger.info(
                        "making room for a gap-filler: holding back #%s (%s)",
                        demoted.id,
                        demoted.title[:40],
                    )
                    shortlist = shortlist[:-1]
                shortlist = shortlist + fillers[: max(0, limit - len(shortlist))]
        except Exception:
            # Discovery is a bonus; a plan built only from saved links is still
            # a plan.
            logger.exception("venue discovery failed; continuing without it")

    if not api_key:
        return Plan(ok=False, error="GEMINI_API_KEY is not configured", excluded=excluded)

    prompt = PROMPT_TEMPLATE.format(
        today=today.isoformat(),
        places="\n".join(_describe_place(c) for c in shortlist),
        stop_count_rule=(
            # include_all is an explicit "everything, please", so the usual
            # licence to use fewer stops is withdrawn for that request.
            f"Include EVERY place listed above - all {len(shortlist)} of them - "
            "even if that makes for a long day."
            if include_all
            else f"Include between 1 and {len(shortlist)} stops."
        ),
    )
    raw, error = _call_model(prompt, api_key, model_name)
    if raw is None:
        return Plan(ok=False, error=error or "model call failed", excluded=excluded)

    payload = _extract_json(raw)
    if payload is None:
        logger.warning("planner response was not JSON: %r", raw[:200])
        return Plan(ok=False, error="model did not return JSON", excluded=excluded)

    # Grounding enforcement: only ids we supplied may appear, and the names the
    # user sees are taken from our own records rather than from the response.
    by_id = {c.handle(): c for c in shortlist}
    # Everything the model is allowed to name, drawn from our own records.
    allowed_text = " | ".join(
        " ".join(filter(None, (c.title, c.location, c.tags))) for c in shortlist
    ).lower()
    stops: list[Stop] = []
    dropped: list[str] = []
    for entry in payload.get("stops") or []:
        if not isinstance(entry, dict):
            continue
        handle = str(entry.get("id") or "").strip()
        candidate = by_id.get(handle)
        if candidate is None:
            logger.warning("model referenced unknown place id %r; dropping", handle)
            dropped.append(handle)
            continue
        if any(s.title == candidate.title for s in stops):
            continue
        stops.append(
            Stop(
                link_id=candidate.id if candidate.source == "saved" else None,
                title=candidate.title,
                url=candidate.url or None,
                location=candidate.location,
                source=candidate.source,
                when=(str(entry.get("when")).strip() or None) if entry.get("when") else None,
                why=_scrub_prose(
                    str(entry.get("why")).strip() if entry.get("why") else None,
                    allowed_text,
                ),
            )
        )

    if not stops:
        return Plan(ok=False, error="model returned no usable stops", dropped=dropped, excluded=excluded)

    summary = payload.get("summary")
    summary = _scrub_prose(str(summary).strip() if summary else None, allowed_text)
    if len(stops) > LONG_PLAN_WARNING_STOPS:
        warnings.append(
            f"{len(stops)} stops is a lot for one day - this reads more like a "
            "shortlist than an itinerary."
        )
    if include_all:
        warnings.append(
            f"Included every eligible link ({len(shortlist)}), ignoring the "
            f"{limit}-stop setting."
        )
        if len(stops) < len(shortlist):
            warnings.append(
                f"{len(shortlist) - len(stops)} of them did not make it into the "
                "arrangement."
            )

    logger.info(
        "plan built: %d stop(s), %d dropped for grounding, %d warning(s)",
        len(stops), len(dropped), len(warnings),
    )
    return Plan(
        ok=True,
        warnings=warnings,
        stops=stops,
        summary=summary,
        dropped=dropped,
        cluster_size=cluster_size,
        excluded=excluded,
        spread_metres=_spread(shortlist),
    )
