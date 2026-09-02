# Couple's Planner Bot

A private Telegram bot for two people. Paste TikTok/Instagram links into a shared
group chat; the bot extracts what they're about, categorises them, and stores
them in a browsable Mini App. It tracks anniversaries and important dates, and
proposes a date plan built from saved links.

**The primary goal is a tidy, filterable store of saved posts.** The planner is
secondary — it was deferred repeatedly while the store was built out, and that
was correct each time. It now works, but a thin plan is still a sign of too few
saved links rather than a bug in the planner.

## Current status

**Live in production.** Deployed on Render, database on Neon Postgres, both
users allowlisted, Mini App opening from the Telegram group.

| Feature | Status |
|---|---|
| Link intake (TikTok + Instagram, all URL shapes) | Done |
| Forwarded channel posts saved automatically | Done |
| Canonical URL resolution + dedup | Done |
| Extraction chain (yt-dlp → oEmbed → screenshots) | Done |
| Gemini parsing: title, location, dates, region, category, tags | Done |
| Screenshot/vision path, incl. multi-slide albums | Done |
| REST API with initData auth + user allowlist | Done |
| Mini App: To visit / Day trips / Done / Calendar / Settings | Done |
| Filters, rating + note | Done |
| Anniversary + monthsary countdowns, daily reminders | Done |
| Per-date reminder milestones, editable in the Mini App | Done |
| Geocoding (lat/lng via OneMap search) | Done |
| `plan_date()` — clustering, urgency tiers, grounded arrangement | Done |
| OneMap venue gap-filling when saved links leave a hole | Done |
| Shared calendar — month view, free-text notes, both users | Done |
| In-app settings: stops per plan, nearby radius, home region | Done |
| Manual entries — a place with no link behind it | Done |
| Edit dialog: title, location, category, tags, rating, note | Done |
| Separate geocode hint, re-geocoding on save | Done |
| Badge + count for links geocoding could not place | Done |
| Collections — roundups with no single location | Done |
| `link_photos` table, intake vs visit photos | Done |
| Screenshot previews on cards + full-size viewer | Done |
| Visit photos: Mini App upload and `+visit` in chat | Done |
| Webhook `update_id` dedup + bounded retry | Done |
| `/health` answering GET and HEAD, for the uptime keep-alive | Done |
| OneMap public-transport routing times | **Not built** |
| Automatic Friday plan run | Built, deliberately not scheduled |
| Same-venue grouping across posts | Deferred deliberately |

## Architecture

Two front ends, one backend:

- **Bot (chat)**: link capture, reminder pushes, plan delivery. Notifications
  and intake only — kept deliberately thin.
- **Mini App (Telegram WebApp)**: where browsing actually happens. Plain
  HTML/CSS/JS served from the same FastAPI service, so it's same-origin (no
  CORS). Uses `telegram-web-app.js` for identity and theming.

**Deployment:**

- **Render free tier** — one service hosting bot webhook, API, and Mini App.
  Sleeps after ~15 min idle with ~1 min cold start. Acceptable for two users.
- **Neon free Postgres** — permanent free tier, no card, scale-to-zero.
- **GitHub Actions** — runs the scheduled jobs. Required because a sleeping
  Render service cannot fire its own schedules. In webhook mode APScheduler is
  deliberately disabled so the two never double-post; under local polling
  APScheduler runs them instead.
- **Transport is webhook**, selected via `TELEGRAM_TRANSPORT`. Polling still
  works for local dev. Never run local polling while Render is live — two
  clients on one token split updates unpredictably.

## Tech stack

**Language: Python.** Chosen because `yt-dlp` is a native Python package, so
extraction is a library import rather than subprocess parsing. Local dev uses a
virtualenv at `venv/`.

- **Bot**: `python-telegram-bot`
- **API**: `fastapi` + `uvicorn`
- **Extraction**: `yt-dlp` (library), TikTok oEmbed endpoint
- **LLM**: `google-genai` against the Gemini free tier — the *only* provider.
  The older `google-generativeai` package is end-of-life (Nov 2025) — do not
  use it. Anthropic is **not** an alternate: a Claude Pro subscription does not
  include API access, which is billed separately, so there is no free Anthropic
  path for this project.
- **Database**: Postgres (Neon) in production; SQLite still supported for local
  dev via a small engine-compat module. Not an ORM.
- **Scheduling**: GitHub Actions in production, `apscheduler` locally
- **Geocoding**: OneMap — free, Singapore-accurate, MRT-aware. Search needs no
  auth; routing and the thematic layers need a free registered token.
- **Live event data**: none. See the note below before reaching for one.

### Secrets

`.env.planner` locally (gitignored), Render environment in production, and
GitHub Actions **repository** secrets (not environment secrets — the workflow
does not declare an environment) for the scheduled jobs.

`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `ALLOWED_USER_IDS`, `GEMINI_API_KEY`,
`DATABASE_URL`, `WEBHOOK_URL`, `WEBHOOK_SECRET`.

## Hard-won operational notes

Things that cost real debugging time. Read before touching related code.

- **Gemini free tier is ~20 requests/day/model**, not just ~5/minute. Quota is
  per-model, so a fallback chain (`gemini-3.6-flash` → `gemini-3.5-flash` →
  `gemini-flash-latest`) multiplies the daily budget. `gemini-3.7-flash` works
  but 503s frequently — bad default for unattended intake.
- **Verify model strings with a real call.** `models.list()` reports models that
  404 when actually invoked. Listed ≠ callable.
- **TikTok video extraction breaks intermittently.** "Unable to extract
  universal data for rehydration" is a TikTok-side change, not a stale yt-dlp —
  and it is intermittent, not total (the same URL can fail then succeed 20
  minutes later). TikTok serves different structures by region/client. The
  oEmbed fallback exists precisely for this.
- **oEmbed needs the full `www.tiktok.com` form.** Canonicalisation strips
  `www.`, and oEmbed 400s without it. It also 400s on photo posts, and 429s on
  bursts — pace it with a shared lock, not per-caller courtesy.
- **`WEBHOOK_SECRET` must be `A-Za-z0-9_-` only.** Telegram rejects base64
  characters (`+`, `/`, `=`). Generate with
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Render's
  `generateValue` produces base64 and cannot be constrained, so it is set
  manually. Validated at startup rather than failing opaquely at `setWebhook`.
- **`WEBHOOK_URL` chicken-and-egg**: the app refuses to start without it, but
  Render doesn't reveal the URL until a service exists. Prefer deriving it from
  `RENDER_EXTERNAL_URL` when unset.
- **Postgres vs SQLite row access**: `sqlite3.Row` supports positional indexing;
  psycopg dict rows do not. Select counts under a name and read them by name.
- **Copying rows with explicit IDs doesn't advance Postgres's identity
  counter** — call `setval` after a migration or the next insert collides.
- **Upgrading yt-dlp doesn't affect an already-running process.** Retry passes
  must run in a fresh subprocess or they test the old code and report failures
  the upgrade just fixed.
- **`localStorage` is per-origin**: `127.0.0.1` and `localhost` are different
  origins. Use one consistently.
- **Adding a group member can convert it to a supergroup**, changing
  `chat_id`. `/chatid` is deliberately not allowlisted so it still works when
  the allowlist is stale.
- **There is no usable free event API for Singapore.** Verified with real
  calls, not assumed: Eventbrite's public search (`/v3/events/search/`) returns
  404 — the path is gone, removed in Dec 2019 to stop competitors mining their
  catalogue, and what remains only fetches events by known id, venue or
  organisation. Ticketmaster Discovery is alive and needs a free key, but its
  documented coverage omits Singapore entirely. data.gov.sg is a catalogue of
  downloadable datasets with no "near this point" query. STB's Tourism
  Information Hub did not resolve in DNS at all. Do not re-attempt these
  without new evidence; gap-filling should use OneMap thematic layers, which
  cover venues rather than events. There is deliberately **no**
  `app/services/events.py`: this note is the whole of what that module would
  have contained, and a file holding only a comment is a decoy — the next
  person opens it expecting a service and finds a redirect to here.

## Extraction

**Fallback chain** — no user action should be required:

1. `yt-dlp` — best quality when it works. It scrapes, so it breaks periodically.
2. **TikTok oEmbed** (`https://www.tiktok.com/oembed?url=`) — a published API,
   not a scrape, so it survives what breaks yt-dlp. Returns the caption as
   `title` plus a `thumbnail_url`. Feed the caption to the parser *and* the
   thumbnail through the vision path — cover frames usually carry text overlays
   with the dish, price, or venue.
3. Only if both fail, ask the user for a screenshot.

**Forwarded channel posts are a third intake route.** A post forwarded into
the group from a Telegram channel is saved automatically. It has a caption and
usually a photo, which is exactly what the parser already reads, so it goes
through the *same* single Gemini call and the same closed taxonomy. What it
lacks is a URL: nothing to canonicalise, nothing for yt-dlp, nothing for the
Mini App to open. So it is stored the way a manual entry is, with `url` NULL,
and `platform` set to `telegram` so the card can still say where it came from.

- **Only channel forwards.** A forward from a person or a group is far more
  likely to be conversation, and auto-saving every forwarded photo would fill
  the list with things nobody chose to keep.
- **Links win.** If the forwarded caption contains a TikTok or Instagram URL it
  goes down the link path instead, which canonicalises and de-duplicates it
  properly. The forward branch is only reached when there is no link.
- **De-duplicated on the original post.** `canonical_url` holds
  `tg://channel/<chat_id>/<message_id>` — the identity of the post in its
  source channel, so the same thing forwarded twice, from either phone, lands
  on one row. For a forwarded album the *lowest* slide id is used, so arrival
  order cannot change the key.
- A forwarded album is one entry and one Gemini call, buffered by
  `media_group_id` like any other.
- **A URL in the caption is kept, but only to be tapped.** Channel posts sign
  off with "More info: bit.ly/…", so the first URL found goes in `info_url` and
  the card offers it as Open. It is deliberately *not* `url`: nothing extracts
  from it, canonicalises it, or de-duplicates on it, and `url IS NOT NULL` is
  precisely what tells the caption-parse, extraction-retry and metadata-backfill
  jobs that a row has a post worth re-reading — which a shortener is not.
  Telegram's caption entities are read first, because a "More info" hyperlink
  can hide its URL behind display text where no amount of reading the caption
  would find it; a conservative regex is the fallback.

**Photo and carousel posts** (TikTok slideshows, Instagram `/p/` posts) cannot
be extracted at all. Do NOT scrape slide image URLs — fragile, and it sends the
model irrelevant slides. Instead the users screenshot the informative slide and
send it to the group with the post URL in the photo's caption.

- Albums arrive as separate messages sharing a `media_group_id`, buffered with a
  debounce and sent to Gemini in **one** call.
- A screenshot for an already-saved link **enriches** that row rather than being
  rejected as a duplicate — the real workflow is paste URL now, screenshot
  later.
- `+photo` in the caption overrides the skip for links that already have one.
- Store Telegram `file_id`s, never image files. Telegram re-serves them free.

**Two kinds of photo, never conflated.** `link_photos.kind` separates them and
every read filters on it:

- `intake` — a screenshot of the post. This is *model input*: Gemini reads it
  for the menu, the price, the poster's dates. It is also what a To visit card
  previews, because a menu photo says more at a glance than 140 characters of
  caption.
- `visit` — a photo from the day. A memory, never shown to the model, and
  never evidence that a post was readable.

A caption carrying `+visit` files that message's photos as memories: they are
not downloaded, not parsed, and do not count as the link having been enriched,
so `+photo` still behaves as before afterwards. The same `file_id` is never
stored twice for one link, whatever kind it arrives as — the same image cannot
be both a menu the parser read and a souvenir.

**Report the outcome, not the stage.** A link whose yt-dlp attempt failed but
whose vision parse succeeded has NOT failed. Conflating the two produces scary
warnings on working links, which trains the user to ignore warnings.

## Parsing and categorisation

One Gemini call per link produces everything: `title`, `location`, `region`,
`event_start`, `event_end`, `is_evergreen`, `category`, `subcategory`, `tags`.
Never split into two calls — quota is the binding constraint.

**Fixed categories, free tags.** Free-form category naming fragments fast
("Japanese" / "japanese food" / "Jap cuisine"), so the model picks from a closed
list, enforced in code (invalid value → `other`, logged):

- `category` — one of: `food` | `activity` | `place` | `other`
- `subcategory` — one from a fixed list per category:
  - food: japanese, korean, chinese, local/hawker, western, thai, indian,
    cafe/dessert, other
  - activity: sports, hiking/nature, event/festival, arts/museum, workshop,
    nightlife, wellness/spa, other
  - place: bar, staycation, shopping, scenic/view, other
- `tags` — free-form, 0–5 ("halal", "rooftop", "cheap eats", "queue long")

**Return `other` rather than guess.** A confidently wrong category silently
hides links from filtered views. `other/other` is an honest shrug, not content.

**A manual entry is never parsed.** Its fields were typed directly, so there
is nothing to extract and no quota to spend. `parsed_at` is stamped at creation
for exactly that reason: leaving it NULL would queue the row for a Gemini call
to rediscover what someone had just written by hand. Everything that reads a
post — caption parsing, extraction retries, metadata backfill — additionally
filters on `url IS NOT NULL`.

**Cache everything.** `parsed_at` marks a link as asked-about, including
caption-less posts skipped without a model call. A planning run must never
re-spend quota. Failed parses leave `parsed_at` NULL so they stay retryable.

**Region**: non-Singapore links get flagged and shown under **Day trips**,
excluded from Saturday clustering but never hidden or deleted. Unknown region
counts as home — an unparsed link falling into the normal planner is a visible
failure; one silently hidden is not.

## Time-sensitivity and priority

`event_start` / `event_end` nullable; `is_evergreen` true when there's no expiry.

**Priority scoring happens in code, not in the prompt:**
- ends within 7 days → `urgent`
- ends within 30 days → `soon`
- evergreen → `flexible`

Pass the sorted, tiered list to the model and tell it to build around urgent
items. Never ask the LLM to prioritise — inconsistent and hard to debug.

Expired links are filtered from planning input and dimmed in the Mini App, not
deleted.

## Geocoding

OneMap's search endpoint (no auth) turns each link's `location` string into
`lat`/`lng`. It runs **once per link, not once per plan**: `geocoded_at` marks
the attempt and `geocode_status` records why it failed, because NULL
coordinates plus a status is not the same thing as never having tried.

**Display and lookup are separate columns.** `location` holds what a human
needs in order to find the place — outlet name, unit number, street — and that
is exactly what makes OneMap miss: it does not understand `#02-38`, and
"Singapore" matches half the island. Shortening the address to help the lookup
would degrade the thing the couple actually reads. So `geocode_hint` carries a
postal code or bare street, is used for lookup only, and is never displayed.
Saving one re-runs geocoding; so does changing `location`. Both are editable in
the Mini App, because the person who knows where the place actually is is the
one holding the phone.

**Some entries have no location to find.** A roundup — "August 2026 Markets &
Fairs" listing eight events at eight venues — has no single point, so badging it
nags about something that cannot be fixed. `is_collection` says "no location
expected": no lookup is attempted, the badge is suppressed, and the planner
excludes it explicitly with "a collection, not a single place" rather than the
misleading "no coordinates". It stays browsable, because the content is still
worth having; it just is not somewhere you can go.

Marking one does not erase its `geocode_status` — un-marking restores the badge,
and a lookup runs again, because a wrongly-marked entry should recover rather
than stay silently unplannable.

**A failed lookup is visible.** `geocode_status` of `ambiguous` or `not_found`
means the planner drops the link silently, so the card carries a badge and the
header a count. The badge is a button: it opens the edit dialog with the hint
field focused, because that field is the answer to what the badge is
complaining about.

Non-Singapore locations fail gracefully and keep their day-trip flag rather
than taking the row down with them. Vague locations ("McDonald's") have no
single correct answer, so they are handled rather than crashed on — a link
without coordinates is unplannable, not broken, and stays browsable.

## Planning

`plan_date()` clusters candidates by proximity **in code** and computes urgency
tiers **in code**, then hands the pre-grouped shortlist to Gemini to arrange.
Deterministic work stays deterministic: clustering and prioritising inside the
prompt is inconsistent and hard to debug.

**Grounding rule — the LLM arranges and explains; it never originates a place
name.** Every venue must trace back to a saved link or a real search result.
The model will otherwise produce closed venues and plausible-sounding places
that don't exist, and the failure is discovered in person, on the day. This is
enforced structurally rather than by asking the model nicely: stops are
rendered from database rows, and the surrounding prose is scrubbed for proper
nouns that aren't in the candidate set.

When saved links don't cover a gap, OneMap's thematic layers supply real nearby
amenities as extra candidates, marked as suggestions so they read differently
from something the couple actually chose. If no real candidate exists, say so.

Two modes, one function: *manual* (Mini App multi-select → "Plan with these",
or "Plan with all") and *auto* (a Friday job — written, not scheduled).
Selection uses tap-to-select checkboxes, **not drag-and-drop** — drag fights
the webview's own scroll and swipe gestures on mobile.

A planning run makes several calls against a ~20/day/model ceiling. Budget for
it. With few saved links, plans will legitimately be thin — that's a data
shortage, not a bug.

## Reminders

Milestone-based announcements — not a rolling window. A reminder arriving every
morning for a month gets muted, and a muted reminder is a deleted feature. The
default milestones are **30/14/7/3/1/0 days** out, and `reminder_days` lets any
single date override that from the Mini App.

Dates recur yearly (anniversaries), monthly (monthsaries), or not at all — a
boolean could not express a monthsary, so `recurrence` carries the rule and the
older `recurring` column is retained only as its source. Recurring dates roll
forward once passed and carry a count ("Together (6th)"). Past one-offs
disappear. 29 February is observed on 28 February in common years so it stays
in its own month.

The Mini App banner shows the nearest anniversary **and** the nearest
monthsary. A monthsary is almost always the closer of the two, so showing only
the nearest date hid the anniversary permanently. Dates load in parallel with
links and fail independently — a dates problem must not leave the user staring
at an empty list.

## Shared calendar

A month view in the Mini App, backed by the `availability` table. Entries are
**plain text notes, not structured free/busy**: "gym then free after 8" carries
more than a busy flag does. So `note` holds the text, `slot` is always `day`,
and `available` goes unused. Both users see each other's entries, attributed by
`author_name`.

Anniversaries and monthsaries are drawn into the same grid, marked distinctly
from typed notes and **read-only** — the Dates section stays the single source
of truth, and the same date editable in two places is a divergence waiting to
happen.

## The "Done" flow

- `rating` (1–10) — feeds back into `plan_date()` so it learns what they enjoy.
  The highest-value field in the schema.
- `note` — practical detail ("go before 7pm or you queue")
- photos — rows in `link_photos` with `kind='visit'`, never image bytes

Photos reach a link two ways, both landing in the same rows: the Mini App's
done sheet, and a `+visit` caption in the group. They upload as they are picked
rather than on Save, because downscaling and sending takes a moment on a phone
and would otherwise make Save look stuck.

**An upload posts nothing to the group.** An earlier version relayed each photo
through the bot to obtain a `file_id`, since sending it somewhere is the only
way the Bot API mints one. That put the couple's own photos into their chat as a
side effect of how storage worked, and it was wrong: the bot carries intake and
notifications, never content they did not send themselves.

So an uploaded photo is stored as **bytes**, in `link_photos.image_data`. This
looks like a violation of "store file_ids, never images" and is not: that rule's
premise is that Telegram already holds the picture and re-serves it free. An
upload has never been near Telegram, so there is nothing to point at.

Bytes cost database, and Neon's free tier is 0.5GB, so the Mini App downscales
in a canvas before sending — 1600px on the long edge, JPEG quality 0.85. A
3000×2000 photo lands at about 27KB. The server's 4MB cap is a backstop for the
path where a browser cannot downscale, not the normal case.

## Database

`url` and `platform` are nullable. `platform` is `tiktok` | `instagram` |
`telegram` (a forwarded channel post) | NULL (a manual entry). Only the first
two imply a URL; everything that re-reads a post filters on `url IS NOT NULL`
rather than on the platform.

A **manual entry** — a place tried without a post behind it — is a row in
`links` rather than a table of its own: it needs
the same fields, the same geocoding, and the planner must not be able to tell
the difference. NULL is the honest value there; an empty string would be a URL
that happens to be blank. The Mini App renders no "Open" button when there is
no URL, and no source badge when there is no platform.

`links(id, url, info_url, canonical_url, platform, caption, title, tags, added_by,
added_at, parsed_at, done, done_at, done_by, rating, note, photo_file_id,
event_start, event_end, is_evergreen, location, geocode_hint, is_collection,
region, lat, lng, geocoded_at, geocode_status, category, subcategory)`

`link_photos(id, link_id, file_id, thumb_file_id, image_data, content_type,
digest, kind, added_by, added_at)` — `kind` is `intake` | `visit`.

A photo has exactly one source, and which one depends on how it arrived.
`file_id` means Telegram holds the image because it was sent to the group: every
intake screenshot, and any `+visit` photo. `image_data` means it was uploaded
from the Mini App and has never touched Telegram. `file_id` is therefore
nullable, and reads check for stored bytes first.

`file_id` is the largest size Telegram offers (what the parser reads);
`thumb_file_id` a smaller one for card previews, NULL when none was offered, in
which case reads fall back to `file_id`. An uploaded photo has no thumbnail —
it was downscaled before it arrived, so it is already preview-sized. `digest` is
the SHA-256 of an uploaded image and exists only so picking the same photo twice
is a no-op, the way an identical `file_id` already is.

`dates(id, label, date, recurring, recurrence, reminder_days)` ·
`availability(id, user_id, day, slot, available, note, author_name,
reminder_days)` · `settings(key, value, updated_at)` ·
`processed_updates(update_id, seen_at, attempts, failed)` ·
`plans(id, week_of, summary, created_at)`

Note: `links.photo_file_id` is retained but **dead**. It held a comma-separated
list of file_ids until `link_photos` replaced it; the column is left in place
rather than dropped, the way `dates.recurring` was, so the migration's source is
still readable. `init_db` copies any value it finds into `link_photos` as
`intake`, and deliberately skips a link that already has photo rows — adopting
the old column again would resurrect a photo that had been removed. Nothing
writes to it any more, and a value put there now would be invisible.

Serving a photo has to be a proxy (`GET /api/links/{id}/photos/{photo_id}`):
turning a `file_id` into an image needs the bot token, and a URL carrying that
token would expose every file the bot has ever seen. The consequence is that
image bytes sit behind the same `initData` header as everything else, and an
`<img src>` cannot send a header — so the Mini App fetches each photo and
renders a blob URL. A signed token in the query string was rejected: it puts a
credential in a URL that ends up in logs and history, to save a fetch.

## Security

Private two-person app; bots are publicly discoverable by username, so access is
restricted in code, not just BotFather settings.

- **Chat allowlist**: first thing the webhook handler does is compare
  `chat.id` against `TELEGRAM_CHAT_ID`. Anything else gets a silent 200 — no
  reply, no error. Must run *before* any parsing or LLM call so an unauthorised
  chat cannot consume quota.
- **Webhook secret**: required in webhook mode, not optional. The endpoint is
  public and carries no `initData`, so the secret token is the only proof an
  update came from Telegram.
- **Mini App auth**: verify the `initData` HMAC-SHA256 signature (keyed with the
  bot token) before trusting any user ID, then check it against
  `ALLOWED_USER_IDS`. Signature proves a real Telegram user; the allowlist
  proves it's one of ours. Implemented as middleware every route passes through,
  with a fail-closed helper that 500s if reached without it.
- Constant-time comparison; identical `{"detail": "unauthorised"}` for every
  rejection so a caller can't learn which check failed.
- `/setjoingroups` disabled in BotFather.

## Remaining work

### 1. OneMap public-transport routing

Plans order stops by straight-line proximity without knowing how long the
journey between them actually takes. OneMap's routing endpoint returns real MRT
times, and the token client it needs is already built and caching. Pass those
times into the prompt **as facts** rather than letting the model estimate
travel — a fabricated "15 minutes away" is the same class of error as a
fabricated venue.

Skipped deliberately when the planner was first built, to keep that version's
call budget small.

### 2. Scheduling the automatic plan

`app/jobs/weekly_plan.py` is written and runs, but nothing fires it — the
Friday cron was left unwired on purpose so plans stay on demand while there are
few saved links. Adding it to `.github/workflows/scheduled-jobs.yml` is the
entire change. The open question is not how, but whether: an unattended weekly
run spends several calls against the ~20/day/model ceiling whether or not
anyone reads the result.

### 3. Same-venue grouping (deferred)

Dedup works on canonical URL only, so two influencers posting about the same
restaurant produce unrelated rows.

**Group, don't merge.** Two posts about one place is useful signal, and each may
carry different details. Use a nullable `venue_group_id` so rows stay separate
and the Mini App can stack them. Never auto-merge — chain outlets ("Cafe De
Paris, Orchard" vs "…, Tampines") are different outings, and a silent merge
destroys information without telling anyone.

Geocoding now exists, so the remaining blocker is data: a similarity threshold
needs ~50 real links before it can be tuned. Do not build against a handful of
rows.

## Out of scope

- No auto-scraping of TikTok/Instagram/Lemon8 feeds or hashtags. Only content
  the users put in the group themselves: a link they paste, or a post they
  forward in. No bulk scraping, proxy rotation, or feed crawling — and in
  particular, nothing that reads a channel the bot was not forwarded from.
- No native Android app, no Play Store distribution.
- No paid hosting tiers without asking first — free-tier constraints are a
  deliberate design input, not an obstacle to engineer around.

## Settings belong in the Mini App, not in the source

The two people using this app are not developers and will not edit Python to
change how it behaves. Anything they might reasonably want different — reminder
milestones, how many stops a plan holds, what counts as "nearby", which
categories exist — should be **editable in the Mini App**, stored in the
database, with the constant in the source acting only as the default for a
value nobody has set yet.

A hardcoded constant is a decision taken away from them. Reach for a settings
row before reaching for a module-level constant, and when a constant genuinely
is the right answer — a protocol requirement, a provider's rate limit, a
security boundary — say so explicitly rather than leaving it to be discovered.

**Flag it at the time.** When implementing something that arguably belongs in
settings and hardcoding it anyway, say which value, why it is hardcoded, and
what it would take to expose it. Silent hardcoding is how an app becomes one
only its author can change.

Constants introduced with photos, and why each stayed one:

- `MAX_UPLOAD_BYTES` (4MB, `app/api.py`) — a guard on the database rather than
  a preference. Uploaded photos are stored as bytes and Neon's free tier is
  0.5GB, so an unbounded upload is the one thing that could actually fill it.
- `PHOTO_MAX_EDGE` / `PHOTO_QUALITY` (1600px, 0.85, `miniapp/app.js`) — how far
  an upload is downscaled before sending. Chosen against what the viewer can
  show on a phone and the storage budget above, not as a taste call.
- `PREVIEW_MIN_WIDTH` (320px, `app/handlers/link_handler.py`) — which of
  Telegram's photo sizes to keep as the card thumbnail. A rendering detail with
  no user-visible meaning; exposing it would be a setting nobody could reason
  about.
- Three thumbnails per card (`thumbsHtml` in `miniapp/app.js`) — the weakest of
  the three. It is a taste call about card density, not a constraint. If the
  couple ever wants more, it belongs in settings alongside stops-per-plan.

## The Mini App is a phone app first

It is opened from a Telegram group on a phone, so a layout that only works on a
desktop browser is broken, not merely imperfect. Two failures are worth naming
because both were shipped once:

- **A row that cannot shrink.** `grid-template-columns: repeat(10, 1fr)` looks
  responsive and is not: a grid item's default `min-width` is `auto`, so ten
  rating buttons refused to go below their own content width and the row
  overflowed the sheet. Use `minmax(0, 1fr)`, and where ten across would leave
  targets too small to hit with a thumb, wrap to two rows instead.
- **Inputs under 16px.** iOS — which includes Telegram's webview there — zooms
  the page when a focused input's text is smaller than 16px, and does not zoom
  back out. The page is then wider than the screen and scrolls sideways, which
  reads as a broken layout rather than as a zoom.

- **Telegram's chrome can sit over the web view, not above it.** The Close
  button and header overlapped the countdown banner until the safe areas were
  read. Two insets stack and both must be applied: `safeAreaInset` is the
  device's (notch, home indicator) and `contentSafeAreaInset` is Telegram's own
  chrome inside it. They are exposed as `--safe-top` / `--safe-right` /
  `--safe-bottom` / `--safe-left`, refreshed on `safeAreaChanged`,
  `contentSafeAreaChanged`, `viewportChanged` and `fullscreenChanged`, and
  every fixed or sticky element consumes them. Never substitute a measured
  constant: it would be wrong on the next device and would rot the moment
  Telegram changes its header. The CSS `env(safe-area-inset-*)` values are the
  floor, so a client reporting zero for an inset the browser knows about cannot
  talk the app out of it.

- **A phone cannot force a reload, so stale assets are invisible and
  permanent — and headers alone will not fix it.** Telegram's webview has no
  reload button and no per-origin cache clear, so a deploy can appear to have
  changed nothing. The files also go stale independently, which is worse than a
  stale page: a fresh `index.html` carrying a new element plus a cached
  `app.js` that knows nothing about it leaves that element in the DOM with its
  initial `hidden` attribute and nothing to remove it. **The signature is that
  everything the new JavaScript *adds* is missing at once** — a button, a
  label, an anchor — while the same build is perfect on a desktop.

  `Cache-Control: no-cache` was tried first and **was not enough**: the webview
  kept serving its own copy, cleared cache included. A client that ignores
  revalidation cannot be talked round by another header. So the URL changes
  instead. `/miniapp/` is served by a route rather than by `StaticFiles`, and
  it rewrites index.html's two asset references to `app.js?v=<hash>` and
  `style.css?v=<hash>`, the hash being eight hex characters of that file's
  SHA-256. A changed file is a URL no cache has ever seen; an unchanged one is
  the URL it already holds. Requests carrying `?v=` are answered `immutable`
  for a year, since that URL can only ever mean those bytes; a bare request
  still gets `no-cache`.

  **Diagnose in this order.** When something works on a desktop and not on the
  phone, first establish *which build the phone has*: tap the subtitle five
  times for a readout (build, Telegram version and platform, viewport, insets,
  the measured state of the element in question, `color-mix` support, user
  agent) and compare its build with `/health`. Only once those agree is it
  worth looking at layout. Three rounds of this were spent on layout theories
  for what was a delivery problem.

- **Scroll chaining is a touch behaviour and does not reproduce on a desktop.**
  A flick that reaches the end of a sheet keeps going and scrolls the list
  behind it, so closing the sheet reveals the list somewhere else entirely.
  `overscroll-behavior: contain` on the scrolling panel is the fix; a mouse
  wheel will not demonstrate either the bug or the fix, so verify it on a
  phone. For an overlay that does not scroll at all — the backdrop, the photo
  viewer — `overscroll-behavior` does nothing, because it only applies to
  scroll containers; `touch-action: none` is what refuses the gesture there.

- **`hidden` loses to `display`.** An element with `display: flex` of its own
  stays on screen when the attribute is set, silently. This caused three
  separate bugs — a photo viewer painting over the list, and the links list
  rendering behind both the Calendar and Settings tabs — each patched with
  another `.thing[hidden] { display: none }` until there were ten of them. One
  `[hidden] { display: none !important }` replaces the lot. Setting `.hidden`
  in JavaScript is the app's only way of showing and hiding, so this rule has
  to hold without exception.

Check changes at 320px as well as 390px, and check the dialogs, not just the
list: the done, date, day and plan sheets are all easy to leave overflowing
because they are usually opened on a desktop while developing. The screenshots
in the README are from a desktop browser and prove nothing about this.

## Conventions

- **This file can be wrong.** If a dependency, model name, or API named here is
  deprecated or unavailable, say so and ask before implementing — don't follow
  stale guidance because it's written down. Verify against the live API.
- Keep the LLM call isolated in one function per purpose so providers can be
  swapped without touching handlers.
- Every external call must fail gracefully — a failed extraction still stores
  the raw URL rather than dropping the message.
- **Make failure modes legible.** When something can't work, say which thing and
  why, name the constraint, and suggest the fix. Silent failure and opaque
  errors both cost more than they save.
- **Give the human an override.** Automation that makes judgment calls needs a
  documented escape hatch (`+photo`, manual date edit, `GEMINI_MODEL`).
- No secrets in code or commits.
- Small, testable functions over large modules.