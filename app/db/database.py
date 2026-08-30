"""SQLite access layer.

Connections are opened per operation rather than shared. Volume is two people
pasting links, and a short-lived connection sidesteps sqlite3's thread-affinity
rules when called from async handlers.

Every write logs what it did (or why it didn't), so a surprising row in the DB
can be traced back to a specific update in the log.
"""

import hashlib
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
    # Lookup-only address, never displayed. See schema.sql for why this is not
    # just `location`.
    "geocode_hint": "TEXT",
    # A roundup with no single location. NOT NULL needs a default here, which
    # SQLite allows on ADD COLUMN, so existing rows become 0 rather than NULL.
    "is_collection": "INTEGER NOT NULL DEFAULT 0",
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


# Both describe the post a link came from, and a manual entry has no post. They
# are relaxed together rather than one now and one later.
_NULLABLE_FOR_MANUAL_ENTRIES = ("url", "platform")


def _migrate_links_url_nullable(conn) -> None:
    """Drop the NOT NULL on links.url and links.platform for a SQLite database
    that predates manual entries. Postgres does this with one ALTER each in
    schema_postgres.sql.

    SQLite cannot alter a constraint in place, so the table is rebuilt. The new
    definition is derived from PRAGMA table_info rather than written out here,
    because the column list drifts with every migration and a hardcoded copy
    would silently drop whatever was added last.
    """
    columns = [dict(row) for row in conn.execute("PRAGMA table_info(links)")]
    if not columns:
        return
    still_required = [
        c["name"]
        for c in columns
        if c["name"] in _NULLABLE_FOR_MANUAL_ENTRIES and c["notnull"]
    ]
    if not still_required:
        return

    logger.info("migrating: rebuilding links so %s may be NULL", ", ".join(still_required))

    def definition(column: dict) -> str:
        parts = [column["name"], column["type"] or "TEXT"]
        if column["pk"]:
            # id is the only primary key here, and it must keep AUTOINCREMENT
            # so a deleted row's id is never handed out again.
            parts.append("PRIMARY KEY AUTOINCREMENT")
        elif column["notnull"] and column["name"] not in _NULLABLE_FOR_MANUAL_ENTRIES:
            parts.append("NOT NULL")
        if column["dflt_value"] is not None:
            parts.append(f"DEFAULT {column['dflt_value']}")
        return " ".join(parts)

    names = ", ".join(c["name"] for c in columns)
    body = ",\n            ".join(definition(c) for c in columns)
    conn.execute("ALTER TABLE links RENAME TO links_old")
    conn.executescript(
        f"""
        CREATE TABLE links (
            {body}
        );
        INSERT INTO links ({names}) SELECT {names} FROM links_old;
        DROP TABLE links_old;
        CREATE INDEX IF NOT EXISTS idx_links_url       ON links (url);
        CREATE INDEX IF NOT EXISTS idx_links_done      ON links (done);
        CREATE INDEX IF NOT EXISTS idx_links_canonical ON links (canonical_url);
        """
    )
    logger.info("links rebuilt with %d column(s)", len(columns))


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
            # Comma-separated file_ids predate link_photos on both engines, so
            # the copy across is Python rather than engine-specific SQL.
            _backfill_link_photos(conn)
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
        # After _migrate: the rebuild copies whatever columns exist, so the new
        # ones must already have been added.
        _migrate_links_url_nullable(conn)
        _migrate_link_photos(conn)
        _backfill_link_photos(conn)
        tables = [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
    logger.info("database ready (sqlite), tables: %s", ", ".join(tables))


def create_manual_link(
    db_path: str | Path,
    added_by: int,
    title: str,
    location: str | None = None,
    geocode_hint: str | None = None,
    category: str | None = None,
    subcategory: str | None = None,
    tags: str | None = None,
    note: str | None = None,
    rating: int | None = None,
    done: bool = False,
    is_collection: bool = False,
    added_at: str | None = None,
) -> int:
    """Store a place tried without a link, returning its new id.

    Deliberately a row in `links` rather than a table of its own: it needs the
    same fields, the same geocoding, and the planner must not be able to tell
    the difference. `url`, `canonical_url` and `platform` are NULL - there is
    no post behind it.

    `parsed_at` is stamped at creation. Nothing here came from a caption, so
    there is nothing to extract, and leaving the marker NULL would put the row
    in the caption-parse queue to spend Gemini quota learning what was just
    typed in by hand.
    """
    added_at = added_at or utc_now_iso()
    with connect(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO links (url, canonical_url, platform, title, location, "
            "geocode_hint, category, subcategory, tags, note, rating, done, "
            "done_at, done_by, added_by, added_at, parsed_at, is_evergreen, "
            "is_collection) "
            "VALUES (NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "RETURNING id",
            (
                title,
                location,
                geocode_hint,
                category,
                subcategory,
                tags,
                note,
                rating,
                bool(done),
                added_at if done else None,
                added_by if done else None,
                added_by,
                added_at,
                added_at,
                True,
                bool(is_collection),
            ),
        )
        link_id = int(dict(cursor.fetchone())["id"])
    logger.info(
        "stored manual entry id=%s by=%s title=%r location=%r category=%s/%s done=%s",
        link_id,
        added_by,
        (title or "")[:60],
        (location or "")[:60],
        category,
        subcategory,
        bool(done),
    )
    return link_id


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
    added_at: str | None = None,
) -> int:
    """Insert a link and return its new id.

    `url` is the raw pasted URL and is always stored; every metadata field is
    optional so a failed extraction still results in a saved link.

    Photos are not a parameter: they are rows in link_photos, attached with
    add_link_photos() once the id exists. The old links.photo_file_id column is
    left in place but is no longer written - a value put there now would be
    invisible, because the migration only adopts links that have no photo rows
    yet.
    """
    added_at = added_at or utc_now_iso()
    with connect(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO links (url, canonical_url, platform, title, caption, "
            "location, added_by, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (
                url,
                canonical_url,
                platform,
                title,
                caption,
                location,
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


# An intake screenshot is model input; a visit photo is a memory. Keeping the
# two apart is the whole reason link_photos has a kind column - see schema.sql.
PHOTO_INTAKE = "intake"
PHOTO_VISIT = "visit"
PHOTO_KINDS = (PHOTO_INTAKE, PHOTO_VISIT)


def split_file_ids(value: str | None) -> list[str]:
    """Read the legacy comma-separated links.photo_file_id column.

    Retained only for the migration into link_photos and for the SQLite to
    Postgres copy, both of which read databases written before that table
    existed. Nothing else should call this: photos are rows now. Telegram
    file_ids are base64url-ish and never contain a comma, so the split is safe.
    """
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def add_link_photos(
    db_path: str | Path,
    link_id: int,
    photos: list[tuple[str, str | None]],
    kind: str,
    added_by: int | None = None,
    added_at: str | None = None,
) -> list[str]:
    """Attach photos to a link, skipping any already present.

    `photos` is (file_id, thumb_file_id) pairs: the full-size id is what the
    parser reads, the thumbnail is what a card renders. Returns the file_ids
    actually inserted, so a caller can tell whether anything changed and
    whether a re-parse is warranted.

    A file_id already attached is skipped whatever its kind. The same image
    cannot be both a menu the model reads and a souvenir, so a second send is
    a repeat rather than a reclassification.
    """
    if kind not in PHOTO_KINDS:
        raise ValueError(f"unknown photo kind {kind!r}; expected one of {PHOTO_KINDS}")
    if not photos:
        return []
    added_at = added_at or utc_now_iso()
    with connect(db_path) as conn:
        if conn.execute("SELECT id FROM links WHERE id = ?", (link_id,)).fetchone() is None:
            logger.info("cannot attach photos: link id=%s does not exist", link_id)
            return []
        existing = {
            dict(row)["file_id"]
            for row in conn.execute(
                "SELECT file_id FROM link_photos WHERE link_id = ?", (link_id,)
            )
        }
        added: list[str] = []
        for file_id, thumb_file_id in photos:
            if not file_id or file_id in existing:
                continue
            conn.execute(
                "INSERT INTO link_photos "
                "(link_id, file_id, thumb_file_id, kind, added_by, added_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (link_id, file_id, thumb_file_id, kind, added_by, added_at),
            )
            existing.add(file_id)
            added.append(file_id)
    if not added:
        logger.info("link id=%s already has these %s photo(s)", link_id, kind)
        return []
    logger.info(
        "attached %d %s photo(s) to link id=%s by user %s",
        len(added),
        kind,
        link_id,
        added_by,
    )
    return added


def list_link_photos(
    db_path: str | Path, link_id: int, kind: str | None = None
) -> list[sqlite3.Row]:
    """Photos for one link, oldest first, optionally of a single kind."""
    sql = (
        # image_data is deliberately absent: it is megabytes, and every caller
        # of this listing wants metadata. read_link_photo_bytes() fetches it.
        "SELECT id, link_id, file_id, thumb_file_id, content_type, digest, kind, added_by, added_at "
        "FROM link_photos WHERE link_id = ?"
    )
    params: list = [link_id]
    if kind is not None:
        sql += " AND kind = ?"
        params.append(kind)
    with connect(db_path) as conn:
        return conn.execute(sql + " ORDER BY id", tuple(params)).fetchall()


def photos_by_link(db_path: str | Path) -> dict[int, list[dict]]:
    """Every photo, grouped by link id.

    The Mini App lists all links in one request, so their photos come back in
    one query too rather than one per card.
    """
    grouped: dict[int, list[dict]] = {}
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, link_id, file_id, thumb_file_id, content_type, digest, kind, added_by, added_at "
            "FROM link_photos ORDER BY link_id, id"
        ).fetchall()
    for row in rows:
        data = dict(row)
        grouped.setdefault(int(data["link_id"]), []).append(data)
    logger.debug("loaded %d photo(s) across %d link(s)", len(rows), len(grouped))
    return grouped


def get_link_photo(db_path: str | Path, photo_id: int) -> sqlite3.Row | None:
    """One photo row, for the endpoint that streams its bytes."""
    with connect(db_path) as conn:
        return conn.execute(
            "SELECT id, link_id, file_id, thumb_file_id, content_type, digest, kind, added_by, added_at "
            "FROM link_photos WHERE id = ?",
            (photo_id,),
        ).fetchone()


def add_uploaded_photo(
    db_path: str | Path,
    link_id: int,
    data: bytes,
    content_type: str,
    added_by: int | None = None,
    added_at: str | None = None,
) -> int | None:
    """Store an image uploaded from the Mini App, returning its new row id.

    Bytes rather than a Telegram file_id, and that is deliberate. The usual
    rule - store file_ids, never images - exists because Telegram already holds
    the picture and re-serves it for free. An upload has never been to
    Telegram, and the only way to give it a file_id would be for the bot to
    post the couple's photo into their own group, which is not what the group
    is for.

    Returns None when this exact image is already attached to this link, so a
    double tap on the picker is a no-op rather than a second copy. Identical
    bytes are the upload equivalent of an identical file_id.
    """
    digest = hashlib.sha256(data).hexdigest()
    added_at = added_at or utc_now_iso()
    with connect(db_path) as conn:
        if conn.execute("SELECT id FROM links WHERE id = ?", (link_id,)).fetchone() is None:
            logger.info("cannot attach a photo: link id=%s does not exist", link_id)
            return None
        if conn.execute(
            "SELECT id FROM link_photos WHERE link_id = ? AND digest = ?",
            (link_id, digest),
        ).fetchone() is not None:
            logger.info("link id=%s already has this uploaded photo", link_id)
            return None
        cursor = conn.execute(
            "INSERT INTO link_photos "
            "(link_id, file_id, thumb_file_id, image_data, content_type, digest, "
            " kind, added_by, added_at) "
            "VALUES (?, NULL, NULL, ?, ?, ?, ?, ?, ?) RETURNING id",
            (link_id, data, content_type, digest, PHOTO_VISIT, added_by, added_at),
        )
        photo_id = int(dict(cursor.fetchone())["id"])
    logger.info(
        "stored uploaded photo id=%s on link id=%s (%d bytes, %s) by user %s",
        photo_id,
        link_id,
        len(data),
        content_type,
        added_by,
    )
    return photo_id


def read_link_photo_bytes(db_path: str | Path, photo_id: int) -> tuple[bytes, str] | None:
    """The stored image for an uploaded photo, or None for a Telegram-backed one.

    Kept separate from the metadata reads so that listing photos never drags
    megabytes of image through the connection.
    """
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT image_data, content_type FROM link_photos WHERE id = ?",
            (photo_id,),
        ).fetchone()
    if row is None:
        return None
    data = dict(row)
    if data["image_data"] is None:
        return None
    return bytes(data["image_data"]), (data["content_type"] or "image/jpeg")


def _migrate_link_photos(conn) -> None:
    """Make link_photos.file_id nullable on a SQLite database that predates uploads.

    SQLite cannot drop a NOT NULL constraint in place, so the table is rebuilt.
    Guarded on the constraint actually being present, which makes this a no-op
    on every start after the first. Postgres does the same thing with a one-line
    ALTER in schema_postgres.sql.
    """
    columns = {row["name"]: row for row in conn.execute("PRAGMA table_info(link_photos)")}
    if not columns:
        return  # table absent; CREATE TABLE above will make the current shape
    for column, column_type in (
        ("image_data", "BLOB"),
        ("content_type", "TEXT"),
        ("digest", "TEXT"),
    ):
        if column not in columns:
            logger.info("migrating: adding link_photos.%s (%s)", column, column_type)
            conn.execute(f"ALTER TABLE link_photos ADD COLUMN {column} {column_type}")
    if not columns.get("file_id", {})["notnull"]:
        return
    logger.info("migrating: rebuilding link_photos so file_id may be NULL")
    conn.execute("ALTER TABLE link_photos RENAME TO link_photos_old")
    conn.executescript(
        """
        CREATE TABLE link_photos (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            link_id       INTEGER NOT NULL REFERENCES links(id) ON DELETE CASCADE,
            file_id       TEXT,
            thumb_file_id TEXT,
            image_data    BLOB,
            content_type  TEXT,
            digest        TEXT,
            kind          TEXT    NOT NULL,
            added_by      INTEGER,
            added_at      TEXT    NOT NULL
        );
        INSERT INTO link_photos
            (id, link_id, file_id, thumb_file_id, image_data, content_type,
             digest, kind, added_by, added_at)
        SELECT id, link_id, file_id, thumb_file_id, image_data, content_type,
               digest, kind, added_by, added_at
          FROM link_photos_old;
        DROP TABLE link_photos_old;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_link_photos_file
            ON link_photos (link_id, file_id);
        CREATE INDEX IF NOT EXISTS idx_link_photos_link
            ON link_photos (link_id, kind, id);
        """
    )


def delete_link_photo(db_path: str | Path, link_id: int, photo_id: int) -> bool:
    """Remove one photo from one link. True when a row was actually deleted.

    Scoped to the link as well as the photo so a mistyped id cannot reach
    someone else's row, and so the caller's 404 means the same thing the read
    endpoint's does.
    """
    with connect(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM link_photos WHERE id = ? AND link_id = ?",
            (photo_id, link_id),
        )
        deleted = cursor.rowcount > 0
    logger.info(
        "delete photo id=%s from link id=%s -> %s",
        photo_id,
        link_id,
        "removed" if deleted else "no such photo",
    )
    return deleted


def _backfill_link_photos(conn) -> None:
    """Copy legacy links.photo_file_id values into link_photos.

    Runs on every startup and is idempotent, but deliberately skips any link
    that already has photo rows rather than merging id by id. Once a link has
    been migrated its photos are managed in link_photos, and re-adding
    whatever the old column still holds would resurrect a photo that had been
    removed there.

    Everything migrated is 'intake': the old column was only ever written by
    link intake, since the Mini App could update done, rating and note alone.
    added_by is left NULL - the column recorded no author, and inventing one
    would be worse than an honest blank.
    """
    legacy = [
        dict(row)
        for row in conn.execute(
            "SELECT id, photo_file_id, added_at FROM links "
            "WHERE photo_file_id IS NOT NULL AND photo_file_id <> ''"
        )
    ]
    if not legacy:
        return
    already = {
        int(dict(row)["link_id"])
        for row in conn.execute("SELECT DISTINCT link_id FROM link_photos")
    }
    migrated = photos = 0
    for row in legacy:
        link_id = int(row["id"])
        if link_id in already:
            continue
        file_ids = split_file_ids(row["photo_file_id"])
        if not file_ids:
            continue
        for file_id in file_ids:
            conn.execute(
                "INSERT INTO link_photos "
                "(link_id, file_id, thumb_file_id, kind, added_by, added_at) "
                "VALUES (?, ?, NULL, ?, NULL, ?)",
                (link_id, file_id, PHOTO_INTAKE, row["added_at"] or utc_now_iso()),
            )
            photos += 1
        migrated += 1
    if migrated:
        logger.info(
            "migrated %d photo(s) from links.photo_file_id across %d link(s)",
            photos,
            migrated,
        )


def links_needing_caption_parse(db_path: str | Path) -> list[sqlite3.Row]:
    """Rows whose caption has never been parsed.

    parsed_at IS NULL is the whole cache check - a parsed row is never
    reconsidered, however sparse its result was.
    """
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, url, platform, title, caption FROM links "
            "WHERE parsed_at IS NULL AND url IS NOT NULL ORDER BY id"
        ).fetchall()
    logger.info("%d link(s) awaiting caption parse", len(rows))
    return rows


def links_needing_extraction_retry(db_path: str | Path) -> list[sqlite3.Row]:
    """Links yt-dlp could not read, and for which no screenshot exists.

    `caption` is written only by the extractor, so an empty one means yt-dlp
    returned nothing for this link. A link with screenshots is excluded because
    the vision path has already supplied its content - retrying the extractor
    there would spend time to learn nothing new.

    Only intake screenshots count. A visit photo says the couple went, not that
    the post was ever readable, so a link with holiday snaps and no caption is
    still worth retrying.
    """
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, url, canonical_url, platform, title, caption, parsed_at "
            "FROM links "
            "WHERE url IS NOT NULL "
            "  AND (caption IS NULL OR caption = '') "
            "  AND NOT EXISTS ("
            "        SELECT 1 FROM link_photos "
            "         WHERE link_photos.link_id = links.id AND kind = ?"
            "      ) "
            "ORDER BY id",
            (PHOTO_INTAKE,),
        ).fetchall()
    logger.info("%d link(s) eligible for an extraction retry", len(rows))
    return rows


def all_links_for_reparse(db_path: str | Path) -> list[sqlite3.Row]:
    """Every link with a post behind it, ignoring the parsed_at cache.

    Only for a deliberate re-parse after the prompt or storage rules change;
    it re-spends quota, which normal operation never does.

    Manual entries are excluded by `url IS NOT NULL`, the same filter the
    once-only path uses. Their fields were typed by hand, so a re-parse would
    spend a call per entry to overwrite what someone wrote with the model's
    guess at it - and a manual entry always has a title, so the parser would
    not even skip it for having nothing to read. Stamping parsed_at at creation
    keeps them out of the normal queue; this keeps them out of the forced one.
    """
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, url, platform, title, caption FROM links "
            "WHERE url IS NOT NULL ORDER BY id"
        ).fetchall()
    logger.info("%d link(s) selected for forced re-parse", len(rows))
    return rows


def links_missing_metadata(db_path: str | Path) -> list[sqlite3.Row]:
    """Rows stored before extraction ran, i.e. with no canonical URL yet."""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, url, platform FROM links "
            "WHERE canonical_url IS NULL AND url IS NOT NULL ORDER BY id"
        ).fetchall()
    logger.info("%d link(s) missing metadata", len(rows))
    return rows


def list_links(db_path: str | Path) -> list[sqlite3.Row]:
    """Every link, newest first. The Mini App splits these into To visit / Done
    client-side, so both are returned in one call."""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, url, canonical_url, platform, title, caption, location, "
            "geocode_hint, is_collection, region, category, subcategory, lat, lng, tags, "
            "added_by, added_at, done, done_at, done_by, "
            "rating, note, event_start, event_end, is_evergreen, "
            "parsed_at, geocode_status "
            "FROM links ORDER BY added_at DESC, id DESC"
        ).fetchall()
    logger.info("listed %d link(s)", len(rows))
    return rows


def get_link(db_path: str | Path, link_id: int) -> sqlite3.Row | None:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, url, canonical_url, platform, title, caption, location, "
            "geocode_hint, is_collection, region, category, subcategory, lat, lng, tags, "
            "added_by, added_at, done, done_at, done_by, "
            "rating, note, event_start, event_end, is_evergreen, "
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
    # Widened beyond the done-flow fields because a parsed title or location is
    # occasionally wrong, and correcting it in place beats re-sending the link
    # and hoping the model does better. Still an explicit set: an unknown key
    # must fail loudly rather than be silently dropped, and the column names
    # must never come from the request.
    allowed_columns = {
        "done",
        "rating",
        "note",
        "title",
        "location",
        "geocode_hint",
        "is_collection",
        "category",
        "subcategory",
        "tags",
    }
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

    Collections are skipped: a roundup of eight venues has no single point, so
    a lookup would spend a request to record a failure that is not one.

    `geocode_hint` is selected because it, not `location`, is what gets looked
    up when one is set - see `app.jobs.backfill_geocode`.
    """
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, url, platform, title, location, geocode_hint, region "
            "FROM links WHERE geocoded_at IS NULL AND NOT is_collection ORDER BY id"
        ).fetchall()
    logger.info("%d link(s) awaiting geocoding", len(rows))
    return rows


def links_for_regeocode(db_path: str | Path) -> list:
    """Every placeable link, ignoring the once-only marker.

    "Placeable" is the only filter, and it means the same thing here as it does
    for the once-only path: collections are excluded, because a roundup of
    eight venues has no single point and a forced run should not go back to
    recording that as a failure. A link with neither a location nor a hint is
    kept - the geocoder answers `no_location` without spending a request, which
    is a truthful outcome to re-record.

    `geocode_hint` is selected for the same reason as above: it wins over
    `location` when set, and omitting it here was how a --force run could
    overwrite coordinates that a hand-typed hint had just fixed.
    """
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, url, platform, title, location, geocode_hint, region "
            "FROM links WHERE NOT is_collection ORDER BY id"
        ).fetchall()
    logger.info("%d link(s) selected for forced re-geocoding", len(rows))
    return rows


# Long enough to cover any retry Telegram will attempt, short enough that the
# table stays small. Telegram gives up well inside this.
PROCESSED_UPDATE_RETENTION_DAYS = 7
# How many times one update may be handed to the handlers. Bounded because a
# release-on-failure loop would otherwise repeat for as long as Telegram keeps
# redelivering.
MAX_UPDATE_ATTEMPTS = 3


def claim_update(db_path: str | Path, update_id: int, max_attempts: int = MAX_UPDATE_ATTEMPTS) -> bool:
    """Claim a Telegram update, returning True if this caller should process it.

    The insert is the lock. Checking first and inserting second would leave a
    window in which a cold-start delivery and its retry both pass the check -
    which is precisely the case this exists to stop - so the claim is one
    statement, and losing the race is reported by it inserting nothing.

    A claim released by `release_update` after a mid-handler failure can be
    taken again, up to `max_attempts`, which is what makes delivery
    at-least-once. The re-claim is guarded on `failed` in its WHERE clause, so
    two retries arriving together still yield one winner.

    ON CONFLICT DO NOTHING ... RETURNING works on both engines: Postgres has
    had it for years, SQLite since 3.24 (and RETURNING since 3.35).
    """
    with connect(db_path) as conn:
        row = conn.execute(
            "INSERT INTO processed_updates (update_id, seen_at, attempts, failed) "
            "VALUES (?, ?, 1, ?) "
            "ON CONFLICT (update_id) DO NOTHING RETURNING update_id",
            (int(update_id), utc_now_iso(), False),
        ).fetchone()
        if row is not None:
            return True

        # Already claimed. Only a previously failed attempt may be retried, and
        # only while it has attempts left.
        retaken = conn.execute(
            "UPDATE processed_updates SET failed = ?, attempts = attempts + 1, seen_at = ? "
            "WHERE update_id = ? AND failed = ? AND attempts < ? "
            "RETURNING attempts",
            (False, utc_now_iso(), int(update_id), True, int(max_attempts)),
        ).fetchone()

    if retaken is not None:
        logger.info(
            "retrying update %s after an earlier failure (attempt %s)",
            update_id,
            dict(retaken)["attempts"],
        )
        return True

    logger.info("update %s already handled or out of attempts; skipping", update_id)
    return False


def release_update(db_path: str | Path, update_id: int) -> bool:
    """Mark a claim as failed so Telegram's retry may take it again.

    Returns whether the update still has attempts left. When it does not, the
    claim stays in place: an update that fails every time should stop being
    redelivered rather than repeat for as long as Telegram is willing to try.
    """
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE processed_updates SET failed = ? WHERE update_id = ?",
            (True, int(update_id)),
        )
        row = conn.execute(
            "SELECT attempts FROM processed_updates WHERE update_id = ?",
            (int(update_id),),
        ).fetchone()
    attempts = int(dict(row)["attempts"]) if row is not None else MAX_UPDATE_ATTEMPTS
    retryable = attempts < MAX_UPDATE_ATTEMPTS
    logger.warning(
        "released update %s after a failure (attempt %s of %s)%s",
        update_id,
        attempts,
        MAX_UPDATE_ATTEMPTS,
        "" if retryable else " - no attempts left, it will not be retried",
    )
    return retryable


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

    existing = {row["name"] for row in conn.execute("PRAGMA table_info(processed_updates)")}
    for column, ddl in (("attempts", "INTEGER NOT NULL DEFAULT 1"), ("failed", "INTEGER NOT NULL DEFAULT 0")):
        if column not in existing:
            logger.info("migrating: adding processed_updates.%s", column)
            conn.execute(f"ALTER TABLE processed_updates ADD COLUMN {column} {ddl}")

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
