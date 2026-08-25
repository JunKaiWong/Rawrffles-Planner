"""Update yt-dlp, then retry the links it previously could not read.

TikTok and Instagram change their page structure regularly, which breaks
yt-dlp's extractors until upstream patches them. A stale yt-dlp is therefore a
recurring cause of links stored with no metadata at all - the failure looks
permanent but is usually just old code.

Two details shape this job:

1. **The retry must run in a fresh process.** Upgrading a package does not
   affect the interpreter that already imported it: the running process keeps
   the old yt-dlp in memory. So the upgrade step re-invokes this module as a
   subprocess with --retry-only, which imports the newly installed version.

2. **Re-extraction can cost Gemini quota.** A link that suddenly yields a
   caption deserves parsing, but the free tier allows only ~20 calls per day
   per model, so parses are capped and paced. Extraction retries themselves are
   free and are not capped.

    python -m app.jobs.refresh_extractor                 # upgrade, then retry
    python -m app.jobs.refresh_extractor --dry-run       # report only
    python -m app.jobs.refresh_extractor --no-upgrade    # retry with what's installed
    python -m app.jobs.refresh_extractor --retry-only    # internal: post-upgrade pass
"""

import argparse
import asyncio
import logging
import subprocess
import sys

from app.config import Settings, load_settings
from app.db.database import (
    init_db,
    links_needing_extraction_retry,
    save_caption_parse,
    update_link_metadata,
)
from app.services.caption_parser import parse_caption_async
from app.services.extractor import extract_async

logger = logging.getLogger(__name__)

# Extraction retries are free; Gemini calls are not. Cap the parses per run so
# an upgrade that fixes many links cannot exhaust the day's quota in one go.
DEFAULT_MAX_PARSES = 5
DELAY_BETWEEN_PARSES_SECONDS = 14
PIP_TIMEOUT_SECONDS = 300


def installed_ytdlp_version() -> str | None:
    """Ask a fresh interpreter, so this reflects what is on disk rather than
    whatever this process imported at startup."""
    try:
        result = subprocess.run(
            [sys.executable, "-c", "import yt_dlp.version as v; print(v.__version__)"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("could not read installed yt-dlp version: %s", exc)
        return None
    return result.stdout.strip() or None


def upgrade_ytdlp() -> tuple[bool, str | None, str | None]:
    """Upgrade yt-dlp in place. Returns (changed, before, after).

    Never raises: a failed upgrade should still let the retry pass run with
    whatever version is installed.
    """
    before = installed_ytdlp_version()
    logger.info("upgrading yt-dlp (currently %s)", before)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"],
            capture_output=True,
            text=True,
            timeout=PIP_TIMEOUT_SECONDS,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("yt-dlp upgrade could not run: %s", exc)
        return False, before, before
    if result.returncode != 0:
        logger.warning(
            "yt-dlp upgrade failed (exit %s): %s",
            result.returncode,
            (result.stderr or result.stdout or "")[-400:],
        )
        return False, before, before

    after = installed_ytdlp_version()
    changed = bool(after and after != before)
    logger.info(
        "yt-dlp %s", f"upgraded {before} -> {after}" if changed else f"already current ({after})"
    )
    return changed, before, after


async def retry_failed_extractions(
    settings: Settings,
    dry_run: bool = False,
    limit: int | None = None,
    max_parses: int = DEFAULT_MAX_PARSES,
) -> tuple[int, int]:
    """Re-run extraction for links that previously yielded nothing.

    Returns (recovered, parsed).
    """
    rows = links_needing_extraction_retry(settings.db_path)
    if limit is not None:
        rows = rows[:limit]
    if not rows:
        logger.info("no links need an extraction retry")
        return 0, 0

    recovered = parsed_count = 0
    for row in rows:
        link_id, url = row["id"], row["url"]
        logger.info("retrying extraction for id=%s url=%s", link_id, url)
        metadata = await extract_async(url)

        if not metadata.ok or not (metadata.caption or metadata.title):
            logger.info("id=%s still cannot be read", link_id)
            continue

        recovered += 1
        logger.info(
            "id=%s recovered via %s: title=%r caption_len=%s thumbnail=%s",
            link_id,
            metadata.source or "unknown",
            (metadata.title or "")[:60],
            len(metadata.caption) if metadata.caption else 0,
            bool(metadata.thumbnail),
        )

        if dry_run:
            continue

        update_link_metadata(
            settings.db_path,
            link_id,
            canonical_url=metadata.canonical_url,
            title=metadata.title,
            caption=metadata.caption,
            location=metadata.location,
        )

        # A caption that did not exist before is genuinely new information, so
        # it is worth a parse even though parsed_at may already be set from the
        # earlier empty attempt.
        if not metadata.caption or parsed_count >= max_parses:
            if metadata.caption:
                logger.info(
                    "id=%s recovered a caption but the parse cap (%d) is reached; "
                    "it will be picked up by backfill_captions",
                    link_id,
                    max_parses,
                )
            continue

        if parsed_count:
            await asyncio.sleep(DELAY_BETWEEN_PARSES_SECONDS)

        # The oEmbed cover image rides along in the same call as the caption.
        result = await parse_caption_async(
            metadata.caption,
            api_key=settings.gemini_api_key,
            model_name=settings.gemini_model,
            title=metadata.title,
            platform=row["platform"],
            images=[metadata.thumbnail] if metadata.thumbnail else None,
        )
        if not result.ok:
            logger.warning("id=%s parse failed after recovery: %s", link_id, result.error)
            continue

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

    logger.info(
        "extraction retry complete: %d recovered, %d parsed, %d still failing",
        recovered,
        parsed_count,
        len(rows) - recovered,
    )
    return recovered, parsed_count


def _spawn_retry_pass(args) -> None:
    """Run the retry in a new interpreter so the upgraded yt-dlp is loaded."""
    command = [sys.executable, "-m", "app.jobs.refresh_extractor", "--retry-only"]
    if args.dry_run:
        command.append("--dry-run")
    if args.limit is not None:
        command += ["--limit", str(args.limit)]
    command += ["--max-parses", str(args.max_parses)]
    logger.info("starting retry pass in a fresh process")
    try:
        subprocess.run(command, timeout=1800, check=False)
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("retry pass could not run: %s", exc)


async def run_scheduled(settings: Settings | None = None) -> None:
    """Entry point for the scheduler: upgrade, then retry out-of-process."""
    settings = settings or load_settings()
    changed, _, _ = await asyncio.to_thread(upgrade_ytdlp)
    logger.info("scheduled extractor refresh (yt-dlp changed=%s)", changed)
    await asyncio.to_thread(
        _spawn_retry_pass,
        argparse.Namespace(dry_run=False, limit=None, max_parses=DEFAULT_MAX_PARSES),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="write nothing")
    parser.add_argument("--limit", type=int, default=None, help="max links to retry")
    parser.add_argument(
        "--max-parses",
        type=int,
        default=DEFAULT_MAX_PARSES,
        help="cap Gemini calls for newly recovered captions",
    )
    parser.add_argument("--no-upgrade", action="store_true", help="skip the yt-dlp upgrade")
    parser.add_argument(
        "--retry-only",
        action="store_true",
        help="internal: the post-upgrade pass, already running fresh code",
    )
    args = parser.parse_args()

    from app.bot import setup_logging

    setup_logging()
    settings = load_settings()
    init_db(settings.db_path)

    if args.retry_only or args.no_upgrade:
        logger.info("yt-dlp in use: %s", installed_ytdlp_version())
        asyncio.run(
            retry_failed_extractions(
                settings,
                dry_run=args.dry_run,
                limit=args.limit,
                max_parses=args.max_parses,
            )
        )
        return

    upgrade_ytdlp()
    # The upgrade cannot affect this process, so the retry runs in a new one.
    _spawn_retry_pass(args)


if __name__ == "__main__":
    main()
