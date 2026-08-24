"""One-off backfill: parse captions for links stored before parsing existed.

Separate from backfill_metadata.py on purpose - that job calls yt-dlp, this one
spends Gemini free-tier quota, and mixing them would make it easy to re-spend
the expensive one while retrying the cheap one.

    python -m app.jobs.backfill_captions              # parse unparsed rows
    python -m app.jobs.backfill_captions --dry-run    # show, write nothing
    python -m app.jobs.backfill_captions --limit 5    # cap the calls

Only rows with parsed_at IS NULL are touched, so re-running costs nothing and
cannot double-parse. A failed parse leaves parsed_at NULL so it can be retried;
a successful one is never repeated.
"""

import argparse
import asyncio
import logging

from app.bot import setup_logging
from app.config import load_settings
from app.db.database import (
    all_links_for_reparse,
    init_db,
    is_day_trip,
    links_needing_caption_parse,
    save_caption_parse,
)
from app.services.caption_parser import parse_caption_async

logger = logging.getLogger(__name__)

# The Gemini free tier permits roughly five requests per minute. Pacing at ~4/min
# keeps a backfill under that ceiling instead of relying on retries.
DELAY_BETWEEN_CALLS_SECONDS = 14


async def backfill(
    settings, dry_run: bool = False, limit: int | None = None, force: bool = False
):
    rows = (
        all_links_for_reparse(settings.db_path)
        if force
        else links_needing_caption_parse(settings.db_path)
    )
    if limit is not None:
        rows = rows[:limit]
    if not rows:
        logger.info("nothing to parse")
        return 0, 0

    parsed_count = failed = 0
    for index, row in enumerate(rows):
        link_id = row["id"]
        logger.info(
            "parsing id=%s platform=%s caption_len=%s",
            link_id,
            row["platform"],
            len(row["caption"]) if row["caption"] else 0,
        )

        result = await parse_caption_async(
            row["caption"],
            api_key=settings.gemini_api_key,
            model_name=settings.gemini_model,
            title=row["title"],
            platform=row["platform"],
        )

        if not result.ok:
            logger.warning("id=%s parse failed: %s", link_id, result.error)
            failed += 1
            continue

        if dry_run:
            logger.info(
                "[dry-run] id=%s would set title=%r location=%r region=%r "
                "start=%s end=%s evergreen=%s category=%s/%s tags=%s day_trip=%s",
                link_id,
                (result.title or "")[:50],
                (result.location or "")[:50],
                result.region,
                result.event_start,
                result.event_end,
                result.is_evergreen,
                result.category,
                result.subcategory,
                list(result.tags),
                is_day_trip(result.region),
            )
        else:
            save_caption_parse(
                settings.db_path,
                link_id,
                title=result.title,
                location=result.location,
                region=result.region,
                event_start=result.event_start,
                event_end=result.event_end,
                is_evergreen=result.is_evergreen,
                category=result.category,
                subcategory=result.subcategory,
                tags=result.tags,
            )
        parsed_count += 1

        if index < len(rows) - 1:
            await asyncio.sleep(DELAY_BETWEEN_CALLS_SECONDS)

    logger.info("caption backfill complete: parsed=%d failed=%d", parsed_count, failed)
    return parsed_count, failed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="write nothing")
    parser.add_argument("--limit", type=int, default=None, help="max links to parse")
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-parse already-parsed links (re-spends quota; use after a prompt change)",
    )
    args = parser.parse_args()

    setup_logging()
    settings = load_settings()
    if not settings.gemini_api_key:
        raise SystemExit("GEMINI_API_KEY is not set in .env.planner")

    logger.info(
        "caption backfill on %s using %s (dry_run=%s)",
        settings.db_path,
        settings.gemini_model,
        args.dry_run,
    )
    init_db(settings.db_path)
    asyncio.run(
        backfill(settings, dry_run=args.dry_run, limit=args.limit, force=args.force)
    )


if __name__ == "__main__":
    main()
