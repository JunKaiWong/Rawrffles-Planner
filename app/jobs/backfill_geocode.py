"""Resolve stored location strings to coordinates, once per link.

    python -m app.jobs.backfill_geocode
    python -m app.jobs.backfill_geocode --dry-run
    python -m app.jobs.backfill_geocode --force     # re-run links already tried

Only links with no geocoded_at are touched, so re-running costs nothing and a
planning run never triggers a lookup. OneMap search is free and unauthenticated
but is a public service, so requests are paced by the geocoder itself.

Nothing here can fail a link: a location outside Singapore, one OneMap cannot
find, and one too vague to place are all recorded as outcomes, and the link
keeps everything else it had - including its day-trip flag.

A link with a geocode_hint is looked up by the hint rather than by its
displayed location, and collections are skipped entirely, so neither --force
nor a normal run can undo a correction made from the Mini App.
"""

import argparse
import logging

from app.config import load_settings
from app.db.database import (
    init_db,
    is_day_trip,
    links_for_regeocode,
    links_needing_geocode,
    save_geocode,
)
from app.services.geocoder import geocode

logger = logging.getLogger(__name__)


def run(settings, dry_run: bool = False, force: bool = False, limit: int | None = None):
    rows = (
        links_for_regeocode(settings.db_path)
        if force
        else links_needing_geocode(settings.db_path)
    )
    if limit is not None:
        rows = rows[:limit]
    if not rows:
        logger.info("nothing to geocode")
        return {}

    tally: dict[str, int] = {}
    for row in rows:
        data = dict(row)
        link_id = data["id"]
        region = data.get("region")
        # The hint wins, exactly as it does on the API path (app.api._geocode_link).
        # This job used to look up `location` alone, so a --force run would
        # re-resolve by the display address and overwrite coordinates that a
        # hand-typed hint had corrected - undoing the fix the badge asked for.
        hint = (data.get("geocode_hint") or "").strip()
        location = hint or data.get("location")

        result = geocode(location, region)
        tally[result.status] = tally.get(result.status, 0) + 1

        label = (data.get("title") or data.get("url") or "")[:38]
        logger.info(
            "id=%-3s %-38s %-14s %s%s",
            link_id,
            label,
            result.status,
            f"{result.lat:.6f},{result.lng:.6f}" if result.ok else "",
            f"  (day trip: {region})" if is_day_trip(region) else "",
        )

        if not dry_run:
            save_geocode(
                settings.db_path,
                link_id,
                status=result.status,
                lat=result.lat,
                lng=result.lng,
            )

    logger.info(
        "geocoding complete: %s",
        ", ".join(f"{status}={count}" for status, count in sorted(tally.items())),
    )
    return tally


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="write nothing")
    parser.add_argument("--limit", type=int, default=None, help="max links to process")
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-geocode links already attempted (after a geocoder change)",
    )
    args = parser.parse_args()

    from app.bot import setup_logging

    setup_logging()
    settings = load_settings()
    init_db(settings.db_path)
    run(settings, dry_run=args.dry_run, force=args.force, limit=args.limit)


if __name__ == "__main__":
    main()
