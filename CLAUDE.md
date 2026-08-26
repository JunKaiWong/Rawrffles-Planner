# Couple's Planner Bot

A private Telegram bot for two people. Paste TikTok/Instagram links into a shared
group chat; the bot extracts what they're about, categorises them, and stores
them in a browsable Mini App. It tracks anniversaries and important dates, and
(not yet built) proposes a Saturday date plan from saved links.

**The primary goal is a tidy, filterable store of saved posts.** The planner is
secondary — it has been deferred repeatedly and that was correct each time.

## Current status

**Live in production.** Deployed on Render, database on Neon Postgres, both
users allowlisted, Mini App opening from the Telegram group.

| Feature | Status |
|---|---|
| Link intake (TikTok + Instagram, all URL shapes) | Done |
| Canonical URL resolution + dedup | Done |
| Extraction chain (yt-dlp → oEmbed → screenshots) | Done |
| Gemini parsing: title, location, dates, region, category, tags | Done |
| Screenshot/vision path, incl. multi-slide albums | Done |
| REST API with initData auth + user allowlist | Done |
| Mini App: To visit / Done / Day trips, filters, rating + note | Done |
| Anniversary countdowns + daily reminders | Done |
| Geocoding (lat/lng) | **Not built** |
| `plan_date()` — the planner | **Not built** |
| Shared availability calendar | **Not built** |
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
  cover venues rather than events.

## Extraction

**Fallback chain** — no user action should be required:

1. `yt-dlp` — best quality when it works. It scrapes, so it breaks periodically.
2. **TikTok oEmbed** (`https://www.tiktok.com/oembed?url=`) — a published API,
   not a scrape, so it survives what breaks yt-dlp. Returns the caption as
   `title` plus a `thumbnail_url`. Feed the caption to the parser *and* the
   thumbnail through the vision path — cover frames usually carry text overlays
   with the dish, price, or venue.
3. Only if both fail, ask the user for a screenshot.

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

## Reminders

Milestone-based announcements at **30/14/7/3/1/0 days** out — not a rolling
window. A reminder arriving every morning for a month gets muted, and a muted
reminder is a deleted feature.

Recurring dates roll to next year once passed and carry an anniversary count
("Together (6th)"). Past one-offs disappear. 29 February is observed on 28
February in common years so it stays in its own month.

The Mini App shows only the nearest date as a banner above the list. Dates load
in parallel with links and fail independently — a dates problem must not leave
the user staring at an empty list.

## The "Done" flow

- `rating` (1–10) — feeds back into `plan_date()` so it learns what they enjoy.
  The highest-value field in the schema.
- `note` — practical detail ("go before 7pm or you queue")
- `photo_file_id` — Telegram file_id list, never image bytes

## Database

`links(id, url, canonical_url, platform, caption, title, tags, added_by,
added_at, parsed_at, source, done, done_at, done_by, rating, note,
photo_file_id, event_start, event_end, is_evergreen, location, region, lat, lng,
category, subcategory)`

`dates(id, label, date, recurring)` · `availability(id, user_id, day, slot,
available)` · `plans(id, week_of, summary, created_at)`

Note: `photo_file_id` currently holds a comma-separated list — a denormalised
shortcut. A `link_photos` table is the clean version; migrate before the Mini
App renders multiple images per link.

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

### 1. Geocoding (blocks the planner)

OneMap search (no auth) turns each link's `location` string into `lat`/`lng`,
stored once per link. Non-Singapore locations must fail gracefully and keep
their day-trip flag. Vague locations ("McDonald's") have no single answer —
handle rather than crash.

### 2. `plan_date()`

Cluster candidates by proximity **in code**, compute urgency tiers **in code**,
then hand the pre-grouped shortlist to Gemini to arrange.

**Grounding rule — the LLM arranges and explains; it never originates a place
name.** Every venue must trace back to a saved link or a real search result.
The model will otherwise produce closed venues and plausible-sounding places
that don't exist, and the failure is discovered in person. When links don't
cover a gap, query OneMap's thematic layers for real nearby amenities and pass
those as the candidate set. If no real candidate exists, say so.

Then call OneMap public-transport routing for real MRT journey times and pass
those into the prompt as facts.

Two modes, one function: *manual* (multi-select in the Mini App → "Plan with
these") and *auto* (Friday job picks top-scoring unexpired links). Use
tap-to-select checkboxes, **not drag-and-drop** — drag fights the webview's own
scroll and swipe gestures on mobile.

A planning run makes several calls against a ~20/day/model ceiling. Budget for
it. With few saved links, plans will legitimately be thin — that's a data
shortage, not a bug.

### 3. Same-venue grouping (deferred)

Dedup works on canonical URL only, so two influencers posting about the same
restaurant produce unrelated rows.

**Group, don't merge.** Two posts about one place is useful signal, and each may
carry different details. Use a nullable `venue_group_id` so rows stay separate
and the Mini App can stack them. Never auto-merge — chain outlets ("Cafe De
Paris, Orchard" vs "…, Tampines") are different outings, and a silent merge
destroys information without telling anyone.

Needs geocoding first, and needs ~50 real links before a similarity threshold
can be tuned. Do not build against a handful of rows.

### 4. Shared availability

Lightweight: inline-keyboard slot picking, or a Mini App calendar view.

## Out of scope

- No auto-scraping of TikTok/Instagram/Lemon8 feeds or hashtags. Only links the
  users paste themselves. No bulk scraping, proxy rotation, or feed crawling.
- No native Android app, no Play Store distribution.
- No paid hosting tiers without asking first — free-tier constraints are a
  deliberate design input, not an obstacle to engineer around.

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