"""SQLite access layer.

Connections are opened per operation rather than shared. Volume is two people
pasting links, and a short-lived connection sidesteps sqlite3's thread-affinity
rules when called from async handlers.

Every write logs what it did (or why it didn't), so a surprising row in the DB
can be traced back to a specific update in the log.
"""

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Columns added after the first release. schema.sql creates them for new
# databases; existing ones are patched by _migrate(), since
# CREATE TABLE IF NOT EXISTS silently skips an existing table.
_EXPECTED_LINK_COLUMNS = {
    "canonical_url": "TEXT",
    "title": "TEXT",
    "location": "TEXT",
    "region": "TEXT",
    "category": "TEXT",
    "subcategory": "TEXT",
    # Reserved for the geocoding step, which is not built yet; they stay NULL
    # until then but are part of the documented schema.
    "lat": "REAL",
    "lng": "REAL",
    # Set once a caption has been parsed, so it is never parsed twice.
    "parsed_at": "TEXT",
}

# Links outside this region are kept and browsable but excluded from MRT-based
# Saturday clustering - they are day trips, not a stop away.
HOME_REGION = "singapore"


def is_day_trip(region: str | None) -> bool:
    """True when a link is known to be outside the home region.

    An unknown region is deliberately NOT a day trip: an unparsed link should
    fall through to the normal planner rather than being quietly set aside.
    """
    return bool(region) and region.strip().lower() != HOME_REGION


def utc_now_iso() -> str:
    """Timestamp format used for every date/time column."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    """Open a connection, commit on success, roll back and log on failure."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("database operation failed, rolled back (db=%s)", db_path)
        raise
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns missing from an older database. Additive only - no data is
    rewritten or dropped, so this is safe to run on every startup."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(links)")}
    for column, column_type in _EXPECTED_LINK_COLUMNS.items():
        if column not in existing:
            logger.info("migrating: adding links.%s (%s)", column, column_type)
            conn.execute(f"ALTER TABLE links ADD COLUMN {column} {column_type}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_links_canonical ON links (canonical_url)"
    )


def init_db(db_path: str | Path) -> None:
    """Create tables if absent, then apply additive migrations."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("initialising database at %s", path.resolve())
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    with connect(path) as conn:
        conn.executescript(schema)
        _migrate(conn)
        tables = [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
    logger.info("database ready, tables: %s", ", ".join(tables))


def find_link_by_url(db_path: str | Path, url: str) -> sqlite3.Row | None:
    """Match on the URL exactly as pasted. Cheap pre-check before any network
    call, so a link pasted twice verbatim never triggers extraction."""
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, url, platform, added_by, added_at FROM links WHERE url = ?",
            (url,),
        ).fetchone()
    logger.debug("lookup by url=%s -> %s", url, "hit" if row else "miss")
    return row


def find_link_by_canonical_url(
    db_path: str | Path, canonical_url: str
) -> sqlite3.Row | None:
    """Match on the resolved, normalised URL.

    This is what catches the same post arriving via different share links or
    with different tracking parameters. NULL never matches, which is correct:
    an unresolved link is not known to be a duplicate of anything.
    """
    if not canonical_url:
        return None
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, url, canonical_url, platform, added_by, added_at "
            "FROM links WHERE canonical_url = ?",
            (canonical_url,),
        ).fetchone()
    logger.debug(
        "lookup by canonical_url=%s -> %s", canonical_url, "hit" if row else "miss"
    )
    return row


def insert_link(
    db_path: str | Path,
    url: str,
    platform: str,
    added_by: int,
    canonical_url: str | None = None,
    title: str | None = None,
    caption: str | None = None,
    location: str | None = None,
    photo_file_id: str | None = None,
    added_at: str | None = None,
) -> int:
    """Insert a link and return its new id.

    `url` is the raw pasted URL and is always stored; every metadata field is
    optional so a failed extraction still results in a saved link.
    """
    added_at = added_at or utc_now_iso()
    with connect(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO links (url, canonical_url, platform, title, caption, "
            "location, photo_file_id, added_by, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                url,
                canonical_url,
                platform,
                title,
                caption,
                location,
                photo_file_id,
                added_by,
                added_at,
            ),
        )
        link_id = int(cursor.lastrowid)
    logger.info(
        "stored link id=%s platform=%s added_by=%s url=%s canonical=%s "
        "title=%r caption_len=%s location=%r",
        link_id,
        platform,
        added_by,
        url,
        canonical_url,
        (title or "")[:60],
        len(caption) if caption else 0,
        location,
    )
    return link_id


def update_link_metadata(
    db_path: str | Path,
    link_id: int,
    canonical_url: str | None = None,
    title: str | None = None,
    caption: str | None = None,
    location: str | None = None,
) -> None:
    """Fill in metadata for an already-stored link (used to backfill rows saved
    before extraction existed). Only non-None values overwrite."""
    updates = {
        "canonical_url": canonical_url,
        "title": title,
        "caption": caption,
        "location": location,
    }
    updates = {k: v for k, v in updates.items() if v is not None}
    if not updates:
        logger.debug("no metadata to update for link id=%s", link_id)
        return
    assignments = ", ".join(f"{column} = ?" for column in updates)
    with connect(db_path) as conn:
        conn.execute(
            f"UPDATE links SET {assignments} WHERE id = ?",
            (*updates.values(), link_id),
        )
    logger.info("updated link id=%s fields=%s", link_id, ", ".join(updates))


def save_caption_parse(
    db_path: str | Path,
    link_id: int,
    title: str | None = None,
    location: str | None = None,
    region: str | None = None,
    event_start: str | None = None,
    event_end: str | None = None,
    is_evergreen: bool = True,
    category: str | None = None,
    subcategory: str | None = None,
    tags: tuple[str, ...] | list[str] | None = None,
    parsed_at: str | None = None,
) -> None:
    """Store a caption parse and stamp parsed_at so it never runs again.

    parsed_at is always written, including when the parse found nothing: the
    marker records that the question was asked, which is what keeps the call
    count at one per link.

    The parsed `title` wins over yt-dlp's when present. yt-dlp returns the
    post's headline, which for Instagram is the useless "Video by <handle>",
    whereas the parse returns the name of the actual place - which is what the
    Mini App lists and what planning reasons about. Nothing is lost: the full
    original text stays in `caption`.
    """
    parsed_at = parsed_at or utc_now_iso()
    # Tags are stored comma-separated; the parser strips commas from individual
    # tags so this split is unambiguous on read.
    tags_value = ",".join(tags) if tags else None
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE links SET "
            "  title = COALESCE(?, title), "
            "  location = COALESCE(?, location), "
            "  region = COALESCE(?, region), "
            "  event_start = COALESCE(?, event_start), "
            "  event_end = COALESCE(?, event_end), "
            "  is_evergreen = ?, "
            "  category = COALESCE(?, category), "
            "  subcategory = COALESCE(?, subcategory), "
            "  tags = COALESCE(?, tags), "
            "  parsed_at = ? "
            "WHERE id = ?",
            (
                title,
                location,
                region,
                event_start,
                event_end,
                1 if is_evergreen else 0,
                category,
                subcategory,
                tags_value,
                parsed_at,
                link_id,
            ),
        )
    logger.info(
        "cached caption parse for id=%s: location=%r region=%r start=%s end=%s "
        "evergreen=%s category=%s/%s tags=%s",
        link_id,
        (location or "")[:60],
        region,
        event_start,
        event_end,
        is_evergreen,
        category,
        subcategory,
        tags_value,
    )


def set_photo_file_id(db_path: str | Path, link_id: int, photo_file_id: str) -> None:
    """Attach a screenshot to an existing link.

    The usual sequence is: the URL is pasted (photo posts yield no metadata),
    and the screenshot arrives afterwards to supply what yt-dlp could not read.
    """
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE links SET photo_file_id = ? WHERE id = ?", (photo_file_id, link_id)
        )
    logger.info("attached photo to link id=%s", link_id)


def links_needing_caption_parse(db_path: str | Path) -> list[sqlite3.Row]:
    """Rows whose caption has never been parsed.

    parsed_at IS NULL is the whole cache check - a parsed row is never
    reconsidered, however sparse its result was.
    """
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, url, platform, title, caption FROM links "
            "WHERE parsed_at IS NULL ORDER BY id"
        ).fetchall()
    logger.info("%d link(s) awaiting caption parse", len(rows))
    return rows


def all_links_for_reparse(db_path: str | Path) -> list[sqlite3.Row]:
    """Every link with a caption, ignoring the parsed_at cache.

    Only for a deliberate re-parse after the prompt or storage rules change;
    it re-spends quota, which normal operation never does.
    """
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, url, platform, title, caption FROM links ORDER BY id"
        ).fetchall()
    logger.info("%d link(s) selected for forced re-parse", len(rows))
    return rows


def links_missing_metadata(db_path: str | Path) -> list[sqlite3.Row]:
    """Rows stored before extraction ran, i.e. with no canonical URL yet."""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, url, platform FROM links WHERE canonical_url IS NULL "
            "ORDER BY id"
        ).fetchall()
    logger.info("%d link(s) missing metadata", len(rows))
    return rows


def list_links(db_path: str | Path) -> list[sqlite3.Row]:
    """Every link, newest first. The Mini App splits these into To visit / Done
    client-side, so both are returned in one call."""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, url, canonical_url, platform, title, caption, location, "
            "region, category, subcategory, lat, lng, tags, added_by, added_at, "
            "done, done_at, done_by, "
            "rating, note, photo_file_id, event_start, event_end, is_evergreen, "
            "parsed_at "
            "FROM links ORDER BY added_at DESC, id DESC"
        ).fetchall()
    logger.info("listed %d link(s)", len(rows))
    return rows


def get_link(db_path: str | Path, link_id: int) -> sqlite3.Row | None:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, url, canonical_url, platform, title, caption, location, "
            "region, category, subcategory, lat, lng, tags, added_by, added_at, "
            "done, done_at, done_by, "
            "rating, note, photo_file_id, event_start, event_end, is_evergreen, "
            "parsed_at "
            "FROM links WHERE id = ?",
            (link_id,),
        ).fetchone()
    logger.debug("get link id=%s -> %s", link_id, "hit" if row else "miss")
    return row


def update_link(
    db_path: str | Path,
    link_id: int,
    changes: dict,
    acting_user_id: int,
) -> sqlite3.Row | None:
    """Apply a partial update to a link and return the updated row.

    `changes` holds only the fields the caller actually sent, so omitting a
    field leaves it untouched while explicitly sending null clears it.

    Marking done/undone also maintains done_at and done_by, which the caller
    never sets directly - they record when it happened and who did it.
    """
    allowed_columns = {"done", "rating", "note"}
    unknown = set(changes) - allowed_columns
    if unknown:
        # Guards against a typo'd field silently doing nothing, and keeps the
        # column list here rather than in an f-string built from user input.
        raise ValueError(f"cannot update unknown field(s): {sorted(unknown)}")

    updates = dict(changes)
    if "done" in updates:
        if updates["done"]:
            updates["done"] = 1
            updates["done_at"] = utc_now_iso()
            updates["done_by"] = acting_user_id
        else:
            updates["done"] = 0
            updates["done_at"] = None
            updates["done_by"] = None

    if not updates:
        logger.debug("no changes for link id=%s", link_id)
        return get_link(db_path, link_id)

    assignments = ", ".join(f"{column} = ?" for column in updates)
    with connect(db_path) as conn:
        cursor = conn.execute(
            f"UPDATE links SET {assignments} WHERE id = ?",
            (*updates.values(), link_id),
        )
        if cursor.rowcount == 0:
            logger.info("update skipped: link id=%s not found", link_id)
            return None
    logger.info(
        "updated link id=%s by user=%s fields=%s",
        link_id,
        acting_user_id,
        ", ".join(updates),
    )
    return get_link(db_path, link_id)


def count_links(db_path: str | Path) -> int:
    with connect(db_path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM links").fetchone()[0])
