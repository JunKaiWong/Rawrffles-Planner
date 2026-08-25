# Couple's Planner Bot

A private Telegram bot for two people. Paste TikTok/Instagram links into a shared
group chat, and the bot extracts what they're about, remembers them, and every
Saturday proposes a date plan built from that week's dumped links plus real
local-event data. It also tracks anniversaries/important dates and shared
availability.

## Why Telegram (not a native app)

Telegram gives us auth (user IDs), push delivery (`sendMessage`), and cross-device
sync for free — no APK builds, no Play Store fee, no notification permission
handling. The bot lives in one private group containing both users.

## Two surfaces, one backend

Plain bot chat is bad at "browse a list" and "mark something done" — those need
a real UI. So the project has two front ends sharing one backend/database:

- **Bot (chat)**: link capture (paste a URL, it gets stored), weekly AI plan
  delivery, reminder/anniversary pushes. Stays lightweight — notifications only.
- **Mini App (Telegram WebApp)**: a real web UI opened via a button in the chat
  (or the group's menu button). This is where the actual browsing happens —
  a list of all stored links split into **To visit** / **Done**, with a tap
  to toggle a link's status. Built as a static page (HTML/CSS/JS or React),
  hosted for free, using Telegram's WebApp JS SDK for identity — no separate
  login system needed. Talks to the same backend via normal REST calls.

## Core features

1. **Link intake** — any message in the group containing a TikTok or Instagram
   URL is caught by the bot, metadata extracted, and stored.
2. **AI planner — two modes**:
   - *Manual*: user multi-selects links in the Mini App, taps "Plan with these",
     gets a plan back immediately. Use tap-to-select checkboxes with a floating
     action button, **not drag-and-drop** — drag fights the webview's own scroll
     and swipe gestures on mobile and is a reliable source of bugs.
   - *Auto*: a Friday cron job picks the top-scoring unexpired links itself and
     posts a suggested Saturday plan to the group.
   Both paths call the same `plan_date(links)` function.
3. **Local event lookup** — supplements link-derived ideas with real event data
   (concerts, things happening nearby) rather than scraping social feeds.
4. **Reminders / countdowns** — daily cron checks a dates table and posts when
   an anniversary or important date is within N days.
5. **Shared availability** — lightweight scheduling: either inline-keyboard
   slot picking in chat, or (stretch goal) a Telegram Mini App with a real
   calendar UI.

## Explicitly out of scope

- No auto-scraping of TikTok/Instagram/Lemon8 feeds or hashtags. Only link
  extraction for URLs the users themselves paste. Do not add bulk scraping,
  proxy rotation, or feed-crawling code.
- No native Android app, no Play Store distribution.

## Tech stack (all free-tier)

**Language: Python.** Chosen deliberately — `yt-dlp` is a native Python package,
so link extraction is a library import rather than subprocess parsing.
Development uses a virtualenv at `venv/` (gitignored).

- **Bot**: `python-telegram-bot`
- **REST API** (serves the Mini App): `fastapi` + `uvicorn`
- **Link metadata**: `yt-dlp` used as a library for TikTok; lightweight
  public-URL metadata fetch for Instagram (captions/hashtags only, single-URL
  requests, no bulk crawling).
- **Scheduled jobs**: `apscheduler` (weekly plan, daily reminders)
- **AI planning calls**: `google-genai` against the Gemini free tier (primary).
  Note: the older `google-generativeai` package is end-of-life (Nov 2025) — do
  not use it. Anthropic API is an alternate if trial credits are available.
  Keep the LLM call behind a single `plan_date()` function so the provider can
  be swapped without touching the rest of the code.
  **Free tier is ~5 requests/minute** — pace backfills, retry on rate limit, and
  never split work across two calls that could be done in one.
- **Local events**: Ticketmaster Discovery API and/or Eventbrite API free tiers.
- **Database**: built-in `sqlite3` (two users, low volume). Swap to Postgres
  only if the hosting provider makes that easier.
- **Env vars**: `python-dotenv`
- **Hosting**: Railway, Render, or Fly.io free tier, using Telegram webhook mode.
  Cron jobs run as scheduled tasks on the same host (or Railway's cron feature).
- **Mini App frontend**: plain HTML/CSS/JS (or React if it grows) served as
  static files from the same host, or a free static host (Vercel/Netlify).
  Uses `telegram-web-app.js` (Telegram's WebApp SDK) for identity/theming.
  Backend exposes a small REST API (`GET /links`, `PATCH /links/:id`) that both
  the bot and the Mini App call.
- **Secrets**: `.env` file, never committed. `TELEGRAM_BOT_TOKEN`,
  `TELEGRAM_CHAT_ID`, `ALLOWED_USER_IDS`, `GEMINI_API_KEY`,
  `ANTHROPIC_API_KEY` (optional), `TICKETMASTER_API_KEY`.

## Project structure (target)

```
/app
  bot.py                    # webhook entrypoint, message routing
  api.py                    # FastAPI routes for the Mini App (links CRUD)
  auth.py                   # chat allowlist + initData validation middleware
  handlers/
    link_handler.py         # detects + extracts TikTok/IG links
    reminder_handler.py     # availability + reminder commands
  services/
    extractor.py            # wraps yt-dlp
    ai_planner.py           # plan_date() — single seam for the LLM call
    events.py               # Ticketmaster/Eventbrite lookups
  db/
    schema.sql
    database.py
  jobs/
    weekly_plan.py
    daily_reminders.py
/miniapp
  index.html                # link list UI (To visit / Done)
  app.js                    # fetch calls to /api, Telegram WebApp SDK init
  style.css
requirements.txt
.env.example
.gitignore                  # must contain .env and venv/
CLAUDE.md
```

## Location, clustering, and travel

Plans must be geographically coherent — the couple travels by MRT, so stops
should be near each other.

**Geocoding**: use OneMap (Singapore Land Authority) — free, locally accurate,
MRT-aware. Address search needs no authentication; routing requires a free
registered token. Store `lat` / `lng` on each link once resolved, so geocoding
happens once per link, never per plan.

Pipeline order (each step blocks the next):
1. Caption → structured JSON, including a `location` string.
2. `location` string → lat/lng via OneMap search.
3. **Cluster candidates in code**, not in the prompt. Compute proximity and
   hand the LLM a pre-grouped shortlist. Asking the model to "keep things close
   together" from raw addresses is unreliable; distance is deterministic.
4. Call OneMap public-transport routing for real MRT journey times between the
   chosen stops, and pass those into the prompt as facts.

### Grounding rule (important)

**The LLM arranges and explains; it never originates a place name.** Every venue
in a generated plan must trace back either to a saved link or to a real search
result. Do not let the model suggest restaurants or activities from its own
knowledge — it will produce closed venues and plausible-sounding places that
don't exist, and the failure is discovered in person.

When the saved links don't cover a gap (e.g. no food saved near the anchor
point), query OneMap's thematic layers for real nearby amenities and pass those
results into the prompt as the candidate set. If no real candidate exists, the
plan should say so rather than inventing one.

### Same-venue detection across posts (deferred — do not build yet)

Dedup currently works on canonical URL only, so two influencers posting about
the same restaurant — or the same venue on TikTok and Instagram — produce two
unrelated rows.

When this is addressed: **group, don't merge.** Two posts about one place is
useful signal (independent recommendations, and each post may carry different
details). Use a nullable `venue_group_id` so grouped links stay separate rows
and the Mini App can stack them ("Cafe De Paris — 2 saved posts").

Never auto-merge. Chain outlets ("Cafe De Paris, Orchard" vs "…, Tampines") are
different outings, and a silent merge destroys information without telling
anyone. Flag suspected matches for confirmation instead.

Detection should use fuzzy matching on extracted `title` + `location`, and
becomes far more reliable once geocoding provides coordinates — so build
geocoding first.

**Deferred deliberately**: a similarity threshold cannot be tuned against a
handful of links. Collect ~50 real links first and measure how often this
actually occurs before building anything.

## Database (starting schema)

- `links(id, url, platform, caption, tags, added_by, added_at, done boolean
  default false, done_at, done_by, rating integer, note text, photo_file_id text,
  event_start date, event_end date, is_evergreen boolean default true,
  location text, lat real, lng real,
  category text, subcategory text, tags text)`

### Time-sensitivity

Links are not timeless. A restaurant is evergreen; a pop-up market runs for one
weekend. `event_start` / `event_end` are nullable; `is_evergreen` is true when
there's no expiry.

**Extraction**: dates usually appear in the caption ("till 31 Aug", "this
weekend only"). At intake, call the LLM to parse the caption into structured
JSON `{title, location, event_start, event_end, is_evergreen}`. Do not attempt
this with regex. This is a small, cheap call, separate from `plan_date()`.

**TikTok photo/slideshow posts**: `yt-dlp` cannot extract these — it returns
"Unsupported URL" and only the canonical URL is recoverable. Do NOT solve this
by scraping slide image URLs from TikTok's page structure; that's fragile and
sends the model irrelevant slides.

Instead, the users screenshot the one slide that matters and send it to the
group with the post URL in the photo's caption. One message carries both. The
link handler already reads photo captions, so intake is unchanged. Store the
Telegram `file_id` in `photo_file_id` (free hosting, and it doubles as the
Mini App thumbnail), and pass the image to Gemini's vision model asking for the
same structured JSON the caption parser returns — same schema, same output
shape, one code path.

Optional later improvement: TikTok's public oEmbed endpoint
(`https://www.tiktok.com/oembed?url=`) is a single unauthenticated GET that
often returns caption text for photo posts. Nice-to-have, not load-bearing.

**Categorisation happens in the same Gemini call as caption parsing** — same
quota, no second pass. The primary goal of this app is a tidy, filterable store
of saved posts, so this field set matters more than the planner.

Use fixed categories plus free tags. Free-form category naming fragments fast
("Japanese" / "japanese food" / "Jap cuisine" become separate filter values), so
the model picks from a closed list:

- `category` — exactly one of: `food` | `activity` | `place` | `other`
- `subcategory` — exactly one, from a fixed list per category:
  - food: japanese, korean, chinese, local/hawker, western, thai, indian,
    cafe/dessert, other
  - activity: sports, hiking/nature, event/festival, arts/museum, workshop,
    nightlife, other
  - place: bar, staycation, shopping, scenic/view, other
- `tags` — free-form, 0–5, for detail that doesn't deserve a category
  ("halal", "rooftop", "cheap eats", "queue long")

The fixed pair drives filter UI; tags add richness. **Instruct the model to
return `other` rather than guess** when the caption is ambiguous — a confidently
wrong category silently hides links from filtered views.

This also simplifies planning later: "one food + one activity nearby" becomes a
database query rather than something the LLM must infer.

**Cache all parsed results.** Parse each link once at intake and store the
result. Never re-analyze on a planning run — that's the difference between
staying inside the Gemini free tier and exhausting it.

**Priority scoring happens in code, not in the prompt.** Compute an urgency
tier before calling the LLM:
- ends within 7 days → `urgent`
- ends within 30 days → `soon`
- `is_evergreen` → `flexible`

Pass the sorted, tiered list into the prompt and instruct the model to build the
plan around the urgent items and use flexible ones as filler. Never ask the LLM
to do the prioritization itself — it's inconsistent and hard to debug.

Expired links (`event_end` in the past, not done) should be filtered out of
planning input and visually de-emphasized in the Mini App, not deleted.

### The "Done" flow

Marking something done captures more than a status flip:
- `rating` (1–10, nullable) — feeds back into `plan_date()` so the AI learns what
  the couple actually enjoys. This is the highest-value field in the schema.
- `note` (free text) — practical detail, e.g. "go before 7pm or you queue".
- `photo_file_id` — **do not store image files**. When a photo is sent to the
  bot, Telegram returns a `file_id` string; store that string and hand it back
  to Telegram to re-serve the image. Zero storage cost.

UI: Mini App tap Done → rating + note → save. Photos attach by replying with an
image in the group chat, which the bot links to the most recently completed entry.
- `dates(id, label, date, recurring boolean)`  — anniversaries/important dates
- `availability(id, user_id, day, slot, available boolean)`
- `plans(id, week_of, summary, created_at)`  — generated Saturday plans, for history

## Conventions

- **This file can be wrong.** If a dependency, model name, or API named here is
  deprecated or no longer available, say so and ask before implementing it —
  don't follow stale guidance just because it's written down. Verify model
  strings and package versions against the live API rather than assuming.
- Keep the AI provider call isolated in one function (`plan_date()`); don't
  scatter provider-specific code across handlers.
- Every external API call (yt-dlp, Gemini, Ticketmaster) should fail gracefully
  — if metadata extraction fails for a link, still store the raw URL rather
  than dropping the message.
- No secrets in code or commits. `.env` is gitignored.
- Favor small, testable functions per handler over one large bot.js file.

## Manual setup done before coding (assume complete)

Bot created via @BotFather, privacy mode disabled, bot added to a private
two-person group, `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` already in `.env`.

## Security requirement

This is a private two-person app. Bots are publicly discoverable by username,
so access must be restricted in code, not just via BotFather settings.

**Chat allowlist (webhook)**: the very first thing the webhook handler does is
compare `update.message.chat.id` against `TELEGRAM_CHAT_ID`. Any other chat is
silently ignored with a 200 response — no reply, no error. This check must come
*before* any link parsing or LLM call, so an unauthorized group cannot consume
API quota.

**User allowlist (Mini App)**: the Mini App sends
`window.Telegram.WebApp.initData` with every API request. The backend MUST
verify its HMAC-SHA256 signature (keyed with the bot token) before trusting any
user ID from it — unvalidated `initData` is forgeable. After validating the
signature, additionally check the user ID against `ALLOWED_USER_IDS` (a
comma-separated env var holding the two users' numeric Telegram IDs). Signature
validation proves a real Telegram user; the allowlist proves it's one of ours.

Implement both as middleware that every route passes through; no route may skip
either check.

Also set `/setjoingroups` to Disable in BotFather (after the bot has joined the
group) so it cannot be added to new groups.

## First session goals

1. `/init`-style scaffold: repo structure above, `.env.example`, requirements.txt.
2. Wire up the Telegram webhook + a basic echo/ping to confirm the bot responds
   in the group. **Stop here and verify before building features** — this single
   milestone proves the token, privacy setting, webhook, and deployment are all
   correct. Debugging plumbing later, mid-feature, is far more expensive.
3. Implement link detection + yt-dlp extraction + DB storage.
4. Build the REST API (`GET /links`, `PATCH /links/:id`) and the Mini App list
   view (To visit / Done) against it — this is the feature the user actually
   asked for first, prioritize it before the AI planning call.
5. Implement `plan_date()` against Gemini free tier with a stub event list.
6. Wire the weekly and daily cron jobs.
7. Availability + reminders last — these are the lowest-risk, most mechanical
   pieces.