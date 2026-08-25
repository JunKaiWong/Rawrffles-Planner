"""One database interface over SQLite and Postgres.

The app started on SQLite and stays able to run on it: local development needs
no server, and the file is the fallback if the hosted database is unreachable.
Deployment needs Postgres, because Render's free tier has no persistent disk -
a SQLite file there is erased on every redeploy.

Rather than adopt an ORM for four tables, this module keeps the existing SQL and
smooths over the two differences that actually matter:

  * placeholders - SQLite uses "?", psycopg uses "%s";
  * row access - both are made to yield mappings, so callers can keep using
    row["column"].

Everything else is kept identical at the SQL level on purpose: INSERT ...
RETURNING id works on both (SQLite has supported it since 3.35), and booleans
are passed as Python bools, which SQLite stores as 0/1 and Postgres stores
natively.
"""

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

POSTGRES_PREFIXES = ("postgres://", "postgresql://")


def is_postgres(dsn: str | Path | None) -> bool:
    return isinstance(dsn, str) and dsn.startswith(POSTGRES_PREFIXES)


def _to_postgres_sql(sql: str) -> str:
    """Translate "?" placeholders to "%s".

    Safe here because none of this project's SQL contains a literal "?" inside
    a string; a query that did would need rewriting by hand.
    """
    return sql.replace("?", "%s")


class _PostgresCursor:
    """Cursor wrapper that accepts SQLite-style SQL."""

    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, sql: str, params: tuple | list = ()):
        self._cursor.execute(_to_postgres_sql(sql), params)
        return self

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def __iter__(self):
        return iter(self._cursor)

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount


class _PostgresConnection:
    """Connection wrapper presenting the sqlite3 surface this app uses."""

    def __init__(self, connection):
        self._connection = connection

    def execute(self, sql: str, params: tuple | list = ()):
        cursor = self._connection.cursor()
        return _PostgresCursor(cursor).execute(sql, params)

    def executescript(self, script: str) -> None:
        # psycopg can run several statements in one execute call.
        with self._connection.cursor() as cursor:
            cursor.execute(script)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()


@contextmanager
def connect(dsn: str | Path) -> Iterator[Any]:
    """Open a connection, commit on success, roll back and log on failure.

    Accepts a Postgres URL or a path to a SQLite file.
    """
    if is_postgres(dsn):
        import psycopg
        from psycopg.rows import dict_row

        raw = psycopg.connect(str(dsn), row_factory=dict_row)
        conn = _PostgresConnection(raw)
    else:
        raw = sqlite3.connect(dsn)
        raw.row_factory = sqlite3.Row
        conn = raw

    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("database operation failed, rolled back")
        raise
    finally:
        conn.close()


def describe(dsn: str | Path) -> str:
    """A log-safe description of the target: never prints the password."""
    if is_postgres(dsn):
        text = str(dsn)
        host = text.split("@")[-1].split("/")[0] if "@" in text else "?"
        return f"postgres://{host}"
    return str(dsn)
