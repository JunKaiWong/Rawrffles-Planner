"""SQLite access layer.

Connections are opened per operation rather than shared. Volume is two people
pasting links, and a short-lived connection sidesteps sqlite3's thread-affinity
rules when called from async handlers.

Every write logs what it did (or why it didn't), so a surprising row in the DB
can be traced back to a specific update in the log.
"""

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.db.engine import connect, describe, is_postgres

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
POSTGRES_SCHEMA_PATH = Path(__file__).with_name("schema_postgres.sql")

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
    # Filled by geocoding; NULL when the location could not be resolved, with
    # geocode_status recording why.
    "lat": "REAL",
    "lng": "REAL",
    # Set once a caption has been parsed, so it is never parsed twice.
    "parsed_at": "TEXT",
    # Set once geocoding has been attempted, so it runs once per link rather
    # than per plan. Separate from lat/lng because a failure is a real outcome:
    # NULL coordinates plus a status is not the same as never having tried.
    "geocoded_at": "TEXT",
    "geocode_status": "TEXT",
}

# Links outside this region are kept and browsable but excluded from MRT-based
# Saturday clustering - they are day trips, not a stop away.
HOME_REGION = "singapore"


def is_day_trip(region: str | None, home_region: str | None = None) -> bool:
    """True when a link is known to be outside the home region.

    An unknown region is deliberately NOT a day trip: an unparsed link should
    fall through to the normal planner rather than being quietly set aside.

    `home_region` comes from settings when the caller has them; the module
    constant is only the fallback.
    """
    home = (home_region or HOME_REGION).strip().lower()
    return bool(region) and region.strip().lower() != home


def utc_now_iso() -> str:
    """Timestamp format used for every date/time column."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
    """Create tables if absent, then apply additive migrations.

    The Postgres schema is written with every column present, so it needs no
    incremental migration; only the SQLite file, which predates several
    columns, is patched in place.
    """
    logger.info("initialising database at %s", describe(db_path))

    if is_postgres(db_path):
        with connect(db_path) as conn:
            conn.executescript(POSTGRES_SCHEMA_PATH.read_text(encoding="utf-8"))
            tables = [
                dict(row)["table_name"]
                for row in conn.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' ORDER BY table_name"
                )
            ]
        logger.info("database ready (postgres), tables: %s", ", ".join(tables))
        return

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as conn:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        _migrate(conn)
        _migrate_availability(conn)
        tables = [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
    logger.info("database ready (sqlite), tables: %s", ", ".join(tables))


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
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
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
        # RETURNING rather than lastrowid: supported by Postgres and by SQLite
        # since 3.35, so one statement serves both engines.
        link_id = int(dict(cursor.fetchone())["id"])
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
                bool(is_evergreen),
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


def split_file_ids(value: str | None) -> list[str]:
    """Read the comma-separated photo_file_id column.

    One column holds several ids because a slideshow post is several slides of
    one post - they belong to the same link, not to separate rows. Telegram
    file_ids are base64url-ish and never contain a comma, so the split is safe.
    """
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def add_photo_file_ids(
    db_path: str | Path, link_id: int, file_ids: list[str]
) -> list[str]:
    """Append screenshots to a link, ignoring ones already attached.

    Returns the ids that were actually new, so the caller can decide whether
    anything changed and a re-parse is warranted.
    """
    if not file_ids:
        return []
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT photo_file_id FROM links WHERE id = ?", (link_id,)
        ).fetchone()
        if row is None:
            return []
        existing = split_file_ids(row["photo_file_id"])
        added = [fid for fid in file_ids if fid not in existing]
        if not added:
            logger.info("link id=%s already has these screenshot(s)", link_id)
            return []
        conn.execute(
            "UPDATE links SET photo_file_id = ? WHERE id = ?",
            (",".join(existing + added), link_id),
        )
    logger.info(
        "attached %d screenshot(s) to link id=%s (now %d)",
        len(added),
        link_id,
        len(existing) + len(added),
    )
    return added


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


def links_needing_extraction_retry(db_path: str | Path) -> list[sqlite3.Row]:
    """Links yt-dlp could not read, and for which no screenshot exists.

    `caption` is written only by the extractor, so an empty one means yt-dlp
    returned nothing for this link. A link with screenshots is excluded because
    the vision path has already supplied its content - retrying the extractor
    there would spend time to learn nothing new.
    """
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, url, canonical_url, platform, title, caption, parsed_at "
            "FROM links "
            "WHERE (caption IS NULL OR caption = '') "
            "  AND (photo_file_id IS NULL OR photo_file_id = '') "
            "ORDER BY id"
        ).fetchall()
    logger.info("%d link(s) eligible for an extraction retry", len(rows))
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
            "parsed_at, geocode_status "
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
            "parsed_at, geocode_status "
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
            updates["done"] = True
            updates["done_at"] = utc_now_iso()
            updates["done_by"] = acting_user_id
        else:
            updates["done"] = False
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


def save_geocode(
    db_path: str | Path,
    link_id: int,
    status: str,
    lat: float | None = None,
    lng: float | None = None,
    geocoded_at: str | None = None,
) -> None:
    """Record the outcome of geocoding a link.

    geocoded_at is always written, including for a failure, because the marker
    is what makes this once-per-link: a link OneMap cannot place should not be
    looked up again on every planning run.
    """
    geocoded_at = geocoded_at or utc_now_iso()
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE links SET lat = ?, lng = ?, geocode_status = ?, geocoded_at = ? "
            "WHERE id = ?",
            (lat, lng, status, geocoded_at, link_id),
        )
    logger.info(
        "geocode recorded for id=%s: status=%s coords=%s",
        link_id,
        status,
        f"{lat},{lng}" if lat is not None else "none",
    )


def links_needing_geocode(db_path: str | Path) -> list:
    """Links never put through geocoding.

    geocoded_at IS NULL is the whole check, so a link that failed is not
    retried automatically - re-running is a deliberate act.
    """
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, url, platform, title, location, region "
            "FROM links WHERE geocoded_at IS NULL ORDER BY id"
        ).fetchall()
    logger.info("%d link(s) awaiting geocoding", len(rows))
    return rows


def links_for_regeocode(db_path: str | Path) -> list:
    """Every link with a location, ignoring the once-only marker."""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, url, platform, title, location, region FROM links ORDER BY id"
        ).fetchall()
    logger.info("%d link(s) selected for forced re-geocoding", len(rows))
    return rows


# Long enough to cover any retry Telegram will attempt, short enough that the
# table stays small. Telegram gives up well inside this.
PROCESSED_UPDATE_RETENTION_DAYS = 7


def claim_update(db_path: str | Path, update_id: int) -> bool:
    """Claim a Telegram update, returning True if this caller got it first.

    The insert is the lock. Checking first and inserting second would leave a
    window in which a cold-start delivery and its retry both pass the check -
    which is precisely the case this exists to stop - so the claim is one
    statement, and losing the race is reported by it inserting nothing.

    ON CONFLICT DO NOTHING ... RETURNING works on both engines: Postgres has
    had it for years, SQLite since 3.24 (and RETURNING since 3.35).
    """
    with connect(db_path) as conn:
        row = conn.execute(
            "INSERT INTO processed_updates (update_id, seen_at) VALUES (?, ?) "
            "ON CONFLICT (update_id) DO NOTHING RETURNING update_id",
            (int(update_id), utc_now_iso()),
        ).fetchone()
    claimed = row is not None
    if not claimed:
        logger.info("update %s already processed; skipping", update_id)
    return claimed


def prune_processed_updates(
    db_path: str | Path, keep_days: int = PROCESSED_UPDATE_RETENTION_DAYS
) -> int:
    """Drop claims older than the window Telegram could still retry within."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat(
        timespec="seconds"
    )
    with connect(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM processed_updates WHERE seen_at < ?", (cutoff,)
        )
        removed = cursor.rowcount or 0
    if removed:
        logger.info("pruned %d processed-update record(s)", removed)
    return removed


def get_all_settings(db_path: str | Path) -> list:
    with connect(db_path) as conn:
        return conn.execute("SELECT key, value FROM settings").fetchall()


def set_setting(db_path: str | Path, key: str, value: str) -> None:
    """Upsert one setting.

    Written as select-then-write rather than an ON CONFLICT clause because the
    two engines spell that differently, and this table sees a handful of writes
    in its lifetime.
    """
    with connect(db_path) as conn:
        existing = conn.execute(
            "SELECT key FROM settings WHERE key = ?", (key,)
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, utc_now_iso()),
            )
        else:
            conn.execute(
                "UPDATE settings SET value = ?, updated_at = ? WHERE key = ?",
                (value, utc_now_iso(), key),
            )
    logger.info("setting %s set to %r", key, value)


def save_plan(db_path: str | Path, week_of: str, summary: str) -> int:
    """Keep a generated plan, so past suggestions can be looked back on."""
    with connect(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO plans (week_of, summary, created_at) VALUES (?, ?, ?) "
            "RETURNING id",
            (week_of, summary, utc_now_iso()),
        )
        plan_id = int(dict(cursor.fetchone())["id"])
    logger.info("stored plan id=%s week_of=%s", plan_id, week_of)
    return plan_id


def get_plan(db_path: str | Path, plan_id: int):
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, week_of, summary, created_at FROM plans WHERE id = ?",
            (plan_id,),
        ).fetchone()
    logger.debug("get plan id=%s -> %s", plan_id, "hit" if row else "miss")
    return row


def list_plans(db_path: str | Path, limit: int = 10) -> list:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, week_of, summary, created_at FROM plans "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return rows


# The shared calendar stores prose, not free/busy, so a note covers the whole
# day and `slot` is a constant rather than a time.
CALENDAR_SLOT = "day"


def _migrate_availability(conn) -> None:
    """Add the calendar and per-entry reminder columns to an older SQLite file."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(availability)")}
    for column in ("note", "author_name", "reminder_days"):
        if column not in existing:
            logger.info("migrating: adding availability.%s (TEXT)", column)
            conn.execute(f"ALTER TABLE availability ADD COLUMN {column} TEXT")

    existing = {row["name"] for row in conn.execute("PRAGMA table_info(dates)")}
    for column in ("recurrence", "reminder_days"):
        if column not in existing:
            logger.info("migrating: adding dates.%s (TEXT)", column)
            conn.execute(f"ALTER TABLE dates ADD COLUMN {column} TEXT")
    # A boolean cannot express a monthsary, so recurring is backfilled into the
    # richer column rather than being read from directly.
    conn.execute(
        "UPDATE dates SET recurrence = CASE WHEN recurring THEN 'yearly' ELSE 'once' END "
        "WHERE recurrence IS NULL"
    )


def list_calendar_notes(db_path: str | Path, start: str, end: str) -> list:
    """Every note in a date range, from both users.

    Ordered by day then id so a day's entries keep a stable order rather than
    shuffling between requests.
    """
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, user_id, day, note, author_name, reminder_days "
            "FROM availability "
            "WHERE day >= ? AND day <= ? AND note IS NOT NULL AND note != '' "
            "ORDER BY day, id",
            (start, end),
        ).fetchall()
    logger.info("listed %d calendar note(s) between %s and %s", len(rows), start, end)
    return rows


def save_calendar_note(
    db_path: str | Path,
    user_id: int,
    day: str,
    note: str,
    author_name: str | None = None,
    reminder_days: str | None = None,
) -> str:
    """Create, update or clear one person's note for one day.

    One note per person per day: writing again replaces it, and writing an
    empty note removes it, which is what "clear this" means from the UI. A
    person can only ever touch their own row.
    """
    note = (note or "").strip()
    with connect(db_path) as conn:
        existing = conn.execute(
            "SELECT id FROM availability WHERE user_id = ? AND day = ?",
            (user_id, day),
        ).fetchone()

        if not note:
            if existing is None:
                return "unchanged"
            conn.execute("DELETE FROM availability WHERE id = ?", (dict(existing)["id"],))
            logger.info("cleared calendar note for user=%s day=%s", user_id, day)
            return "cleared"

        if existing is None:
            conn.execute(
                "INSERT INTO availability "
                "(user_id, day, slot, available, note, author_name, reminder_days) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, day, CALENDAR_SLOT, False, note, author_name, reminder_days),
            )
            logger.info("added calendar note for user=%s day=%s", user_id, day)
            return "created"

        conn.execute(
            "UPDATE availability SET note = ?, "
            "author_name = COALESCE(?, author_name), reminder_days = ? "
            "WHERE id = ?",
            (note, author_name, reminder_days, dict(existing)["id"]),
        )
    logger.info("updated calendar note for user=%s day=%s", user_id, day)
    return "updated"


def add_date(
    db_path: str | Path,
    label: str,
    when: str,
    recurrence: str = "once",
    reminder_days: str | None = None,
) -> int:
    """Store a date. `when` is ISO YYYY-MM-DD, recurrence is once|monthly|yearly.

    `recurring` is still written so an older reader of this table sees something
    sensible, but `recurrence` is what the app reads.
    """
    with connect(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO dates (label, date, recurring, recurrence, reminder_days) "
            "VALUES (?, ?, ?, ?, ?) RETURNING id",
            (label, when, recurrence == "yearly", recurrence, reminder_days),
        )
        date_id = int(dict(cursor.fetchone())["id"])
    logger.info(
        "stored date id=%s label=%r date=%s recurrence=%s reminders=%r",
        date_id, label, when, recurrence, reminder_days,
    )
    return date_id


def update_date(
    db_path: str | Path,
    date_id: int,
    label: str | None = None,
    when: str | None = None,
    recurrence: str | None = None,
    reminder_days: str | None = None,
    set_reminders: bool = False,
):
    """Partial update of one date.

    `set_reminders` distinguishes "leave the reminder setting alone" from
    "store this value", which matters because an empty string is meaningful
    here: it means never announce.
    """
    updates: dict[str, object] = {}
    if label is not None:
        updates["label"] = label
    if when is not None:
        updates["date"] = when
    if recurrence is not None:
        updates["recurrence"] = recurrence
        updates["recurring"] = recurrence == "yearly"
    if set_reminders:
        updates["reminder_days"] = reminder_days

    if not updates:
        return get_date(db_path, date_id)

    assignments = ", ".join(f"{column} = ?" for column in updates)
    with connect(db_path) as conn:
        cursor = conn.execute(
            f"UPDATE dates SET {assignments} WHERE id = ?",
            (*updates.values(), date_id),
        )
        if cursor.rowcount == 0:
            return None
    logger.info("updated date id=%s fields=%s", date_id, ", ".join(updates))
    return get_date(db_path, date_id)


def get_date(db_path: str | Path, date_id: int):
    with connect(db_path) as conn:
        return conn.execute(
            "SELECT id, label, date, recurring, recurrence, reminder_days "
            "FROM dates WHERE id = ?",
            (date_id,),
        ).fetchone()


def list_dates(db_path: str | Path) -> list:
    """Every stored date. Whether one is upcoming is decided in code, since a
    recurring date's next occurrence cannot be expressed as a stored value."""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, label, date, recurring, recurrence, reminder_days "
            "FROM dates ORDER BY date"
        ).fetchall()
    logger.debug("listed %d date(s)", len(rows))
    return rows


def delete_date(db_path: str | Path, date_id: int) -> bool:
    with connect(db_path) as conn:
        cursor = conn.execute("DELETE FROM dates WHERE id = ?", (date_id,))
        removed = cursor.rowcount > 0
    logger.info("delete date id=%s -> %s", date_id, "removed" if removed else "not found")
    return removed


def count_links(db_path: str | Path) -> int:
    with connect(db_path) as conn:
        # Named rather than positional: psycopg returns mappings, where [0]
        # would be a missing key rather than the first column.
        row = conn.execute("SELECT COUNT(*) AS n FROM links").fetchone()
    return int(dict(row)["n"])
