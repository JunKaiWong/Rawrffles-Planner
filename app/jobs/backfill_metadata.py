"""One-off backfill: fill in metadata for links stored before extraction existed.

Rows captured by the intake-only version have no canonical_url, so the
canonical de-duplication check cannot match them and the same post could be
saved twice. Running this once resolves and stores their canonical URLs (plus
title/caption/location where available).

    python -m app.jobs.backfill_metadata            # backfill
    python -m app.jobs.backfill_metadata --dry-run  # report only

Safe to re-run: only rows with a NULL canonical_url are touched, and existing
values are never overwritten with None.
"""

import argparse
import asyncio
import logging

from app.bot import setup_logging
from app.config import load_settings
from app.db.database import init_db, links_missing_metadata, update_link_metadata
from app.db.engine import describe
from app.services.extractor import extract_async

logger = logging.getLogger(__name__)


async def backfill(db_path, dry_run: bool = False) -> tuple[int, int]:
    """Return (updated, failed)."""
    rows = links_missing_metadata(db_path)
    if not rows:
        logger.info("nothing to backfill")
        return 0, 0

    updated = failed = 0
    for row in rows:
        link_id, url = row["id"], row["url"]
        logger.info("backfilling id=%s url=%s", link_id, url)
        metadata = await extract_async(url)

        if not metadata.canonical_url and not metadata.ok:
            logger.warning(
                "id=%s could not be resolved at all (%s); leaving as-is",
                link_id,
                metadata.error,
            )
            failed += 1
            continue

        if dry_run:
            logger.info(
                "[dry-run] id=%s would set canonical=%s title=%r location=%r",
                link_id,
                metadata.canonical_url,
                (metadata.title or "")[:60],
                metadata.location,
            )
            updated += 1
            continue

        update_link_metadata(
            db_path,
            link_id,
            canonical_url=metadata.canonical_url,
            title=metadata.title,
            caption=metadata.caption,
            location=metadata.location,
        )
        updated += 1

    logger.info("backfill complete: updated=%d failed=%d", updated, failed)
    return updated, failed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would change, write nothing"
    )
    args = parser.parse_args()

    setup_logging()
    settings = load_settings()
    # describe() strips the password. A DATABASE_URL carries credentials, so
    # it must never reach a log file or a console.
    logger.info(
        "backfilling database at %s (dry_run=%s)", describe(settings.db_path), args.dry_run
    )
    # The DB may predate the metadata columns, and this job can run before the
    # bot has started with the new schema.
    init_db(settings.db_path)
    asyncio.run(backfill(settings.db_path, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
