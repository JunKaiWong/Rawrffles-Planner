-- Schema per CLAUDE.md. Idempotent: safe to run on every startup.
--
-- SQLite has no native BOOLEAN or DATE type. Booleans are INTEGER 0/1, and
-- dates/timestamps are TEXT in ISO-8601 (UTC) so they sort lexicographically
-- and compare correctly with date() / datetime().

CREATE TABLE IF NOT EXISTS links (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    url            TEXT    NOT NULL,
    platform       TEXT    NOT NULL,          -- 'tiktok' | 'instagram'
    caption        TEXT,                      -- filled by extraction later
    tags           TEXT,                      -- filled by extraction later
    added_by       INTEGER NOT NULL,          -- Telegram user id
    added_at       TEXT    NOT NULL,          -- ISO-8601 UTC
    done           INTEGER NOT NULL DEFAULT 0,
    done_at        TEXT,
    done_by        INTEGER,
    rating         INTEGER,                   -- 1-10, feeds back into plan_date()
    note           TEXT,
    photo_file_id  TEXT,                      -- Telegram file_id, never image bytes
    event_start    TEXT,                      -- NULL when evergreen
    event_end      TEXT,
    is_evergreen   INTEGER NOT NULL DEFAULT 1
);

-- Dedup lookups on intake, and the Mini App's To visit / Done split.
CREATE INDEX IF NOT EXISTS idx_links_url  ON links (url);
CREATE INDEX IF NOT EXISTS idx_links_done ON links (done);

CREATE TABLE IF NOT EXISTS dates (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    label     TEXT    NOT NULL,
    date      TEXT    NOT NULL,               -- ISO-8601 date
    recurring INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS availability (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id   INTEGER NOT NULL,
    day       TEXT    NOT NULL,
    slot      TEXT    NOT NULL,
    available INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS plans (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    week_of    TEXT NOT NULL,
    summary    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
