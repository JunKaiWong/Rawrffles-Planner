-- Schema per CLAUDE.md. Idempotent: safe to run on every startup.
--
-- SQLite has no native BOOLEAN or DATE type. Booleans are INTEGER 0/1, and
-- dates/timestamps are TEXT in ISO-8601 (UTC) so they sort lexicographically
-- and compare correctly with date() / datetime().

CREATE TABLE IF NOT EXISTS links (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    -- The pasted URL, always kept exactly as sent. NULL for a manual entry:
    -- a place tried without a link is a real row with no post behind it, and
    -- an empty string would be a URL that happens to be blank rather than an
    -- honest absence. Anything that reads a post - caption parsing, extraction
    -- retries, metadata backfill - filters on url IS NOT NULL.
    url            TEXT,
    canonical_url  TEXT,                      -- normalised dedup key, NULL if unresolved
    platform       TEXT,                      -- 'tiktok'|'instagram'|'telegram', NULL when manual
    -- A URL found inside the content rather than the address of the content.
    -- Forwarded channel posts routinely end with "More info: bit.ly/..." and
    -- that link is worth keeping as something to tap, but it is NOT this row's
    -- post: nothing extracts from it, canonicalises it, or de-duplicates on
    -- it. Kept separate from `url` for exactly that reason - `url IS NOT NULL`
    -- is what tells the caption-parse, extraction-retry and metadata-backfill
    -- jobs that a row has a post they can re-read, and a shortener is not one.
    info_url       TEXT,
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
    -- A roundup rather than a place: "August markets & fairs" lists eight
    -- events at eight venues, so there is no single point to geocode. Marking
    -- it says "no location expected", which suppresses the needs-a-location
    -- badge and keeps it out of planning. It stays browsable - the content is
    -- still worth having, it just is not somewhere you can go.
    is_collection  INTEGER NOT NULL DEFAULT 0,
    -- Lookup-only address, never shown. `location` holds what a human needs to
    -- find the place (outlet name, unit number, street), and that is exactly
    -- what makes OneMap fail: it does not understand "#02-38" and matches
    -- "Singapore" against half the island. The hint carries a postal code or a
    -- bare street address instead, so display quality and lookup accuracy stop
    -- fighting each other. Set by hand when automatic geocoding fails.
    geocode_hint   TEXT,
    -- Set by geocoding. NULL means unresolved, and geocoded_at/geocode_status
    -- (added by database._migrate) record whether it was ever attempted.
    lat            REAL,
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

-- Values the couple can change from the Mini App. Stored rather than compiled
-- in, so changing one needs no redeploy; the constants in the source are only
-- the defaults for a key nobody has set.
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Webhook updates already handled. Telegram redelivers when it does not get a
-- prompt 200, and a slow cold start makes that routine, so the id is claimed
-- before the update is processed.
CREATE TABLE IF NOT EXISTS processed_updates (
    update_id INTEGER PRIMARY KEY,
    seen_at   TEXT    NOT NULL,
    -- See the Postgres schema: a released claim plus an attempt count, so a
    -- failing update is retried a few times and then left alone.
    attempts  INTEGER NOT NULL DEFAULT 1,
    failed    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS plans (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    week_of    TEXT NOT NULL,
    summary    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Photos attached to a link, replacing the comma-separated links.photo_file_id
-- column. That shortcut only worked while nothing rendered more than one image;
-- the Mini App now shows them, so they need rows.
--
-- `kind` is the point of the table, not decoration:
--   'intake' - a screenshot of the post itself, sent so the vision path can
--              read a menu or a poster. Gemini reads these AS DATA.
--   'visit'  - a photo taken on the day. A memory, never model input.
-- Conflating the two would feed holiday snaps to the parser and offer the
-- couple a screenshot of a menu as a souvenir, so the column is NOT NULL and
-- every read filters on it.
--
-- A photo has exactly one source, and which one depends on how it arrived:
--
--   file_id      - the image is held by Telegram, because it was sent to the
--                  group. Every intake screenshot, and any visit photo sent
--                  with a +visit caption. Telegram re-serves these for free,
--                  so no bytes are stored here.
--   image_data   - the image was uploaded from the Mini App and has never
--                  touched Telegram. Storing bytes contradicts the usual rule
--                  precisely because that rule's premise (Telegram already
--                  holds it) does not apply. The bot deliberately does NOT
--                  relay uploads to the group to obtain a file_id: it posts
--                  only notifications and never content the couple did not
--                  send themselves.
--
-- Exactly one of the two is set. file_id is therefore nullable.
--
-- file_id is the largest size Telegram offers (what the parser wants);
-- thumb_file_id is a smaller size for card previews, NULL when none was
-- offered, in which case callers fall back to file_id. An uploaded photo has
-- no thumbnail: the Mini App downscales it in a canvas before sending, so the
-- stored image is already preview-sized.
--
-- digest is the SHA-256 of an uploaded image, and exists only so that picking
-- the same photo twice is a no-op, the way an identical file_id already is.
CREATE TABLE IF NOT EXISTS link_photos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    link_id       INTEGER NOT NULL REFERENCES links(id) ON DELETE CASCADE,
    file_id       TEXT,
    thumb_file_id TEXT,
    image_data    BLOB,
    content_type  TEXT,
    digest        TEXT,
    kind          TEXT    NOT NULL,       -- 'intake' | 'visit'
    added_by      INTEGER,                -- NULL for rows migrated from photo_file_id
    added_at      TEXT    NOT NULL
);

-- One row per image per link. Re-sending the same screenshot is a no-op rather
-- than a second copy, which is what the old split-and-compare did in Python.
CREATE UNIQUE INDEX IF NOT EXISTS idx_link_photos_file ON link_photos (link_id, file_id);
CREATE INDEX IF NOT EXISTS idx_link_photos_link ON link_photos (link_id, kind, id);
