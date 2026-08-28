-- Schema per CLAUDE.md. Idempotent: safe to run on every startup.
--
-- SQLite has no native BOOLEAN or DATE type. Booleans are INTEGER 0/1, and
-- dates/timestamps are TEXT in ISO-8601 (UTC) so they sort lexicographically
-- and compare correctly with date() / datetime().

CREATE TABLE IF NOT EXISTS links (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    url            TEXT    NOT NULL,          -- exactly as pasted, always kept
    canonical_url  TEXT,                      -- normalised dedup key, NULL if unresolved
    platform       TEXT    NOT NULL,          -- 'tiktok' | 'instagram'
    title          TEXT,                      -- from yt-dlp
    caption        TEXT,                      -- post description, from yt-dlp
    location       TEXT,                      -- rarely set by yt-dlp; LLM fills later
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
    is_evergreen   INTEGER NOT NULL DEFAULT 1,
    region         TEXT,                      -- country, e.g. 'Singapore'
    category       TEXT,                      -- closed set: food|activity|place|other
    subcategory    TEXT,                      -- closed set, per category
    lat            REAL,                      -- set by geocoding, not yet built
    lng            REAL,
    parsed_at      TEXT                       -- caption parse cache marker
);

-- Dedup lookups on intake, and the Mini App's To visit / Done split.
--
-- idx_links_canonical is created by database._migrate(), not here: on a
-- pre-existing database CREATE TABLE IF NOT EXISTS skips the table above, so
-- canonical_url does not exist yet at this point and indexing it would fail.
-- The migration adds the column first, then the index.
--
-- canonical_url is deliberately not UNIQUE: it is NULL whenever resolution
-- failed, and several NULLs must be allowed to coexist.
CREATE INDEX IF NOT EXISTS idx_links_url  ON links (url);
CREATE INDEX IF NOT EXISTS idx_links_done ON links (done);

CREATE TABLE IF NOT EXISTS dates (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    label     TEXT    NOT NULL,
    date      TEXT    NOT NULL,               -- ISO-8601 date
    recurring INTEGER NOT NULL DEFAULT 0
);

-- Also backs the shared calendar. That is prose per day ("gym then free after
-- 8"), not structured free/busy, so `note` carries the text, `slot` is always
-- 'day', and `available` is unused.
CREATE TABLE IF NOT EXISTS availability (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    day         TEXT    NOT NULL,
    slot        TEXT    NOT NULL,
    available   INTEGER NOT NULL DEFAULT 0,
    note          TEXT,
    author_name   TEXT,
    reminder_days TEXT
);

CREATE TABLE IF NOT EXISTS plans (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    week_of    TEXT NOT NULL,
    summary    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
