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


def init_db(db_path: str | Path) -> None:
    """Create tables if absent. Safe to call on every startup."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("initialising database at %s", path.resolve())
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    with connect(path) as conn:
        conn.executescript(schema)
        tables = [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
    logger.info("database ready, tables: %s", ", ".join(tables))


def find_link_by_url(db_path: str | Path, url: str) -> sqlite3.Row | None:
    """Return an existing row for this exact URL, if any."""
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, url, platform, added_by, added_at FROM links WHERE url = ?",
            (url,),
        ).fetchone()
    logger.debug("lookup url=%s -> %s", url, "hit" if row else "miss")
    return row


def insert_link(
    db_path: str | Path,
    url: str,
    platform: str,
    added_by: int,
    added_at: str | None = None,
) -> int:
    """Insert a link and return its new id.

    Only the intake columns are written; caption/tags/event dates stay NULL
    until the extraction step exists.
    """
    added_at = added_at or utc_now_iso()
    with connect(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO links (url, platform, added_by, added_at) VALUES (?, ?, ?, ?)",
            (url, platform, added_by, added_at),
        )
        link_id = int(cursor.lastrowid)
    logger.info(
        "stored link id=%s platform=%s added_by=%s url=%s",
        link_id,
        platform,
        added_by,
        url,
    )
    return link_id


def count_links(db_path: str | Path) -> int:
    with connect(db_path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM links").fetchone()[0])
