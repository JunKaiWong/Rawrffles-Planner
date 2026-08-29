"""Copy the SQLite database into Postgres, and prove nothing was lost.

Run once when moving to a hosted database:

    DATABASE_URL=postgresql://... python -m app.jobs.migrate_to_postgres
    DATABASE_URL=postgresql://... python -m app.jobs.migrate_to_postgres --dry-run
    DATABASE_URL=postgresql://... python -m app.jobs.migrate_to_postgres --verify-only

The SQLite file is never modified - it is opened read-only and stays on disk as
the fallback. Re-running is safe: the target is emptied and rewritten inside one
transaction, so a failure part-way leaves the previous contents intact.

Verification is not a formality here. A silent partial copy would look like a
working deployment while quietly dropping links, so the job compares row counts
per table, then compares every links row field by field, and refuses to report
success unless both agree.
"""

import argparse
import logging
import sqlite3
from pathlib import Path

from app.config import load_settings
from app.db.database import POSTGRES_SCHEMA_PATH, init_db
from app.db.engine import connect, describe, is_postgres

logger = logging.getLogger(__name__)

# link_photos comes after links: it references them.
TABLES = ("links", "dates", "availability", "plans", "link_photos")

# Columns copied for each table, named explicitly so a schema drift between the
# two files fails loudly here rather than silently skipping data.
LINK_COLUMNS = (
    "id", "url", "canonical_url", "platform", "title", "caption", "location",
    "region", "category", "subcategory", "tags", "added_by", "added_at",
    "done", "done_at", "done_by", "rating", "note", "photo_file_id",
    "event_start", "event_end", "is_evergreen", "lat", "lng", "parsed_at",
)
TABLE_COLUMNS = {
    "links": LINK_COLUMNS,
    # photo_file_id above is the legacy column, still copied so nothing is
    # lost; these are the rows that replaced it.
    "link_photos": (
        "id", "link_id", "file_id", "thumb_file_id", "kind", "added_by", "added_at",
    ),
    "dates": ("id", "label", "date", "recurring"),
    "availability": ("id", "user_id", "day", "slot", "available"),
    "plans": ("id", "week_of", "summary", "created_at"),
}
BOOLEAN_COLUMNS = {
    "links": {"done", "is_evergreen"},
    "link_photos": set(),
    "dates": {"recurring"},
    "availability": {"available"},
    "plans": set(),
}


def _sqlite_rows(sqlite_path: Path, table: str) -> list[dict]:
    """Read a table from the SQLite file without modifying it."""
    uri = f"file:{sqlite_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        # A SQLite file written before link_photos existed simply has no such
        # table. Its photos are in links.photo_file_id, which is copied with
        # the links row and adopted by the backfill in init_db, so an absent
        # table here means "nothing extra to copy", not a failed migration.
        present = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if table not in present:
            logger.info("source has no %s table; skipping", table)
            return []
        columns = ", ".join(TABLE_COLUMNS[table])
        rows = [dict(r) for r in conn.execute(f"SELECT {columns} FROM {table} ORDER BY id")]
    finally:
        conn.close()
    return rows


def _normalise(table: str, row: dict) -> dict:
    """SQLite stores booleans as 0/1; Postgres returns True/False."""
    out = dict(row)
    for column in BOOLEAN_COLUMNS[table]:
        if out.get(column) is not None:
            out[column] = bool(out[column])
    return out


def copy_table(conn, table: str, rows: list[dict]) -> int:
    columns = TABLE_COLUMNS[table]
    placeholders = ", ".join(["?"] * len(columns))
    column_list = ", ".join(columns)
    for row in rows:
        values = tuple(_normalise(table, row)[c] for c in columns)
        conn.execute(
            f"INSERT INTO {table} ({column_list}) VALUES ({placeholders})", values
        )
    # Identity columns do not advance when ids are supplied explicitly, so the
    # next insert would collide with an existing id without this.
    if rows:
        conn.execute(
            f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
            f"(SELECT MAX(id) FROM {table}))"
        )
    return len(rows)


def verify(sqlite_path: Path, dsn: str) -> bool:
    """Compare counts for every table, then every links row field by field."""
    ok = True
    with connect(dsn) as conn:
        for table in TABLES:
            source = _sqlite_rows(sqlite_path, table)
            target_count = int(
                dict(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone())["n"]
            )
            match = len(source) == target_count
            ok = ok and match
            logger.info(
                "%-12s sqlite=%-4d postgres=%-4d %s",
                table,
                len(source),
                target_count,
                "OK" if match else "MISMATCH",
            )

        # Row counts alone would not catch a column copied as NULL.
        source_links = {r["id"]: _normalise("links", r) for r in _sqlite_rows(sqlite_path, "links")}
        target_links = {
            dict(r)["id"]: dict(r)
            for r in conn.execute(
                f"SELECT {', '.join(LINK_COLUMNS)} FROM links ORDER BY id"
            )
        }

    missing = set(source_links) - set(target_links)
    if missing:
        logger.error("links missing from postgres: %s", sorted(missing))
        return False

    mismatches = 0
    for link_id, source_row in source_links.items():
        target_row = target_links[link_id]
        for column in LINK_COLUMNS:
            if source_row.get(column) != target_row.get(column):
                logger.error(
                    "id=%s column %s differs: sqlite=%r postgres=%r",
                    link_id,
                    column,
                    source_row.get(column),
                    target_row.get(column),
                )
                mismatches += 1
    if mismatches:
        logger.error("%d field mismatch(es) found", mismatches)
        return False

    # Spot-check the fields that matter most to the user, so the log says
    # plainly that screenshots, categories and done status survived.
    with_photos = sum(1 for r in source_links.values() if r.get("photo_file_id"))
    with_category = sum(1 for r in source_links.values() if r.get("category"))
    done_count = sum(1 for r in source_links.values() if r.get("done"))
    logger.info(
        "content check: %d link(s), %d with screenshots, %d categorised, %d done "
        "- all present in postgres",
        len(source_links),
        with_photos,
        with_category,
        done_count,
    )
    return ok


def migrate(sqlite_path: Path, dsn: str, dry_run: bool = False) -> None:
    if not sqlite_path.exists():
        raise SystemExit(f"SQLite file not found: {sqlite_path}")
    if not is_postgres(dsn):
        raise SystemExit("DATABASE_URL must be a postgres:// or postgresql:// URL")

    counts = {t: len(_sqlite_rows(sqlite_path, t)) for t in TABLES}
    logger.info("source %s: %s", sqlite_path, counts)

    if dry_run:
        logger.info("[dry-run] would copy the above into %s", describe(dsn))
        return

    init_db(dsn)
    with connect(dsn) as conn:
        # One transaction: a failure part-way leaves the target as it was.
        for table in reversed(TABLES):
            conn.execute(f"DELETE FROM {table}")
        for table in TABLES:
            copied = copy_table(conn, table, _sqlite_rows(sqlite_path, table))
            logger.info("copied %d row(s) into %s", copied, table)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report only")
    parser.add_argument(
        "--verify-only", action="store_true", help="compare an existing copy"
    )
    args = parser.parse_args()

    from app.bot import setup_logging

    setup_logging()
    settings = load_settings()
    dsn = settings.db_path
    if not is_postgres(dsn):
        raise SystemExit("Set DATABASE_URL to the Postgres connection string first")

    sqlite_path = Path(settings.sqlite_path)
    logger.info("migrating %s -> %s", sqlite_path, describe(dsn))

    if not args.verify_only:
        migrate(sqlite_path, dsn, dry_run=args.dry_run)
    if args.dry_run:
        return

    logger.info("verifying...")
    if verify(sqlite_path, dsn):
        logger.info("migration verified: postgres matches sqlite exactly")
    else:
        raise SystemExit("VERIFICATION FAILED - do not switch over yet")


if __name__ == "__main__":
    main()
