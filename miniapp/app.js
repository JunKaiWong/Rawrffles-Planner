/*
 * Mini App front end.
 *
 * Runs in two environments from one code path:
 *
 *   - Inside Telegram: window.Telegram.WebApp exists and carries real,
 *     signed initData. That string is sent as-is to the API, which verifies
 *     its HMAC signature.
 *
 *   - In a normal browser (development): there is no real initData, so a stub
 *     stands in for the SDK and supplies a locally-signed token generated with
 *     `python -m scripts.make_init_data`. The token is supplied by the
 *     developer - via ?initData=... or the Set token button - and kept in
 *     localStorage. Nothing hands tokens out over the network, so this adds no
 *     attack surface: the API's verification is identical either way.
 *
 * Detection is based on initData being a non-empty string rather than on the
 * SDK object existing, because loading telegram-web-app.js in a plain browser
 * still defines window.Telegram.WebApp - just with initData empty.
 */

const API_BASE = "/api";
const TOKEN_STORAGE_KEY = "planner.devInitData";

// "telegram" is a post forwarded in from a channel: no URL to open, but a
// real source, unlike a manual entry which has none and gets no badge.
const PLATFORM_LABELS = { tiktok: "TikTok", instagram: "Instagram", telegram: "Telegram" };

// --- Telegram SDK, real or stubbed ---------------------------------------

function createStub(initData) {
  // Only the surface this app actually uses. Everything else is a no-op so
  // calls written for the real SDK stay valid here.
  return {
    isStub: true,
    initData: initData || "",
    initDataUnsafe: {},
    colorScheme: window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light",
    themeParams: {},
    ready() {},
    expand() {},
    close() {},
    // A plain browser has no Telegram chrome to sit under, so the insets are
    // genuinely zero here - not unknown.
    isExpanded: true,
    isFullscreen: false,
    safeAreaInset: { top: 0, right: 0, bottom: 0, left: 0 },
    contentSafeAreaInset: { top: 0, right: 0, bottom: 0, left: 0 },
    onEvent() {},
    offEvent() {},
    HapticFeedback: {
      impactOccurred() {},
      notificationOccurred() {},
    },
    showAlert(message) {
      window.alert(message);
    },
    showConfirm(message, callback) {
      callback(window.confirm(message));
    },
  };
}

function storageAvailable() {
  // Private windows and blocked-cookie settings make localStorage throw on
  // use, not on access, so the only reliable test is a real write.
  try {
    localStorage.setItem("planner.probe", "1");
    localStorage.removeItem("planner.probe");
    return true;
  } catch (err) {
    return false;
  }
}

function tokenFromQuery() {
  // NOT URLSearchParams: an initData token is itself a query string
  // ("query_id=...&user=...&auth_date=...&hash=..."), so parsing it as
  // parameters truncates it at the first & and yields an invalid token.
  // Everything after "initData=" is taken verbatim instead, which means the
  // token must be the last parameter in the URL.
  const marker = "initData=";
  const search = window.location.search;
  const at = search.indexOf(marker);
  if (at === -1) return "";
  const raw = search.slice(at + marker.length);
  if (!raw) return "";
  // Accept a token pasted raw *or* percent-encoded. A raw one is already in
  // its final form and must NOT be decoded - its %7B/%22 escapes are part of
  // the signed payload, and decoding them would break the signature.
  if (raw.includes("hash=")) return raw;
  try {
    const decoded = decodeURIComponent(raw);
    if (decoded.includes("hash=")) return decoded;
  } catch (err) {
    /* not valid encoding; fall through */
  }
  return raw;
}

function storeToken(token) {
  try {
    if (token) localStorage.setItem(TOKEN_STORAGE_KEY, token);
    else localStorage.removeItem(TOKEN_STORAGE_KEY);
    return true;
  } catch (err) {
    console.warn("[planner] could not persist dev token", err);
    return false;
  }
}

function readDevToken() {
  // A token in the URL wins and is persisted, so a shareable dev link works
  // once and then keeps working.
  const fromQuery = tokenFromQuery();
  if (fromQuery) {
    storeToken(fromQuery);
    return fromQuery;
  }
  try {
    return localStorage.getItem(TOKEN_STORAGE_KEY) || "";
  } catch (err) {
    console.warn("[planner] localStorage unavailable", err);
    return "";
  }
}

const realWebApp = window.Telegram && window.Telegram.WebApp;
const hasRealInitData =
  !!realWebApp && typeof realWebApp.initData === "string" && realWebApp.initData.length > 0;

const tg = hasRealInitData ? realWebApp : createStub(readDevToken());
const isDev = !hasRealInitData;

// --- State ----------------------------------------------------------------

// Mirrors the closed taxonomy in CLAUDE.md. Kept here only for chip ordering
// and labels; the API is the source of truth for a link's actual values.
const CATEGORY_LABELS = {
  food: "Food",
  activity: "Activity",
  place: "Place",
  other: "Other",
};

const state = {
  links: [],
  dates: [],
  tab: "todo",
  category: "all",
  subcategory: "all",
  tag: null,
  pending: null, // link the sheet is acting on, null while creating
  // "done" keeps marking-visited to two taps; "edit" and "create" show the
  // descriptive fields.
  sheetMode: "done",
  // Files picked before a manual entry exists. An upload needs a link id, so
  // they are held here and sent once the row has been created.
  pendingPhotos: [],
  taxonomy: null,
  entryCategory: null,
  entrySubcategory: null,
  saving: false,
  selecting: false,
  selected: new Set(),
  plan: null,
  planning: false,
  // Calendar: the month on screen, and its notes keyed by day.
  calMonth: null, // {year, month} with month 1-12
  calNotes: {},
  calDay: null,
  editingDate: null,
  settings: null,
  calDates: {},
};

const els = {
  list: document.getElementById("list"),
  status: document.getElementById("status"),
  subtitle: document.getElementById("subtitle"),
  categoryFilters: document.getElementById("category-filters"),
  subcategoryFilters: document.getElementById("subcategory-filters"),
  activeFilters: document.getElementById("active-filters"),
  countTodo: document.getElementById("count-todo"),
  countDayTrip: document.getElementById("count-daytrip"),
  countDone: document.getElementById("count-done"),
  tabs: document.querySelectorAll(".tab"),
  sheet: document.getElementById("sheet"),
  sheetPlace: document.getElementById("sheet-place"),
  doneForm: document.getElementById("done-form"),
  rating: document.getElementById("rating"),
  note: document.getElementById("note"),
  saveBtn: document.getElementById("save-btn"),
  entryDelete: document.getElementById("entry-delete"),
  sheetPhotos: document.getElementById("sheet-photos"),
  sheetTitle: document.getElementById("sheet-title"),
  entryFields: document.getElementById("entry-fields"),
  entryTitle: document.getElementById("entry-title"),
  entryLocation: document.getElementById("entry-location"),
  entryHint: document.getElementById("entry-hint"),
  entryCategory: document.getElementById("entry-category"),
  entrySubcategory: document.getElementById("entry-subcategory"),
  entrySubcategoryField: document.getElementById("entry-subcategory-field"),
  entryTags: document.getElementById("entry-tags"),
  entryDoneField: document.getElementById("entry-done-field"),
  entryDone: document.getElementById("entry-done"),
  entryCollectionField: document.getElementById("entry-collection-field"),
  entryCollection: document.getElementById("entry-collection"),
  addManual: document.getElementById("add-manual"),
  photoAdd: document.getElementById("photo-add"),
  photoInput: document.getElementById("photo-input"),
  viewer: document.getElementById("photo-viewer"),
  viewerImg: document.getElementById("photo-viewer-img"),
  dateBanner: document.getElementById("date-banner"),
  calendar: document.getElementById("calendar"),
  calGrid: document.getElementById("cal-grid"),
  calMonthLabel: document.getElementById("cal-month"),
  calPrev: document.getElementById("cal-prev"),
  calNext: document.getElementById("cal-next"),
  daySheet: document.getElementById("day-sheet"),
  dayForm: document.getElementById("day-form"),
  dayTitle: document.getElementById("day-title"),
  dayTheirs: document.getElementById("day-theirs"),
  dayEvents: document.getElementById("day-events"),
  dayNote: document.getElementById("day-note"),
  daySave: document.getElementById("day-save"),
  dayMilestones: document.getElementById("day-milestones"),
  datesList: document.getElementById("dates-list"),
  dateAdd: document.getElementById("date-add"),
  dateSheet: document.getElementById("date-sheet"),
  dateForm: document.getElementById("date-form"),
  dateSheetTitle: document.getElementById("date-sheet-title"),
  dateLabel: document.getElementById("date-label"),
  dateValue: document.getElementById("date-value"),
  dateRecurrence: document.getElementById("date-recurrence"),
  dateMilestones: document.getElementById("date-milestones"),
  dateSave: document.getElementById("date-save"),
  dateDelete: document.getElementById("date-delete"),
  settingsPanel: document.getElementById("settings"),
  setMaxStops: document.getElementById("set-max-stops"),
  setRadius: document.getElementById("set-radius"),
  setRegion: document.getElementById("set-region"),
  setStopsDefault: document.getElementById("set-stops-default"),
  setRadiusDefault: document.getElementById("set-radius-default"),
  setRegionDefault: document.getElementById("set-region-default"),
  settingsSave: document.getElementById("settings-save"),
  settingsStatus: document.getElementById("settings-status"),
  planAllBtn: document.getElementById("plan-all-btn"),
  selectBtn: document.getElementById("select-btn"),
  planFab: document.getElementById("plan-selected-fab"),
  selectedCount: document.getElementById("selected-count"),
  planSheet: document.getElementById("plan-sheet"),
  planBody: document.getElementById("plan-body"),
  planWeek: document.getElementById("plan-week"),
  planTitle: document.getElementById("plan-title"),
  postPlanBtn: document.getElementById("post-plan-btn"),
  devBanner: document.getElementById("dev-banner"),
  devBannerText: document.getElementById("dev-banner__text"),
  devTokenBtn: document.getElementById("dev-token-btn"),
  devTokenForm: document.getElementById("dev-token-form"),
  devTokenInput: document.getElementById("dev-token-input"),
  devTokenClear: document.getElementById("dev-token-clear"),
};

// --- API ------------------------------------------------------------------

async function api(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-Telegram-Init-Data": tg.initData || "",
      ...(options.headers || {}),
    },
  });

  if (response.status === 401) {
    // Three different situations reach this branch and they need different
    // advice. Saying "token invalid" when the page was simply opened outside
    // Telegram sends someone hunting for a broken token that never existed.
    let hint;
    if (!isDev) {
      hint =
        "Telegram sign-in was rejected. Your account may not be on the allowlist — " +
        "send /whoami in the group to check your id.";
    } else if (!tg.initData) {
      hint =
        "This page only works inside Telegram.\n\n" +
        "Open it from the group (the bot's menu button), where Telegram supplies " +
        "your identity automatically.\n\n" +
        "To use it in this browser instead, run:\n" +
        "  python -m scripts.make_init_data\n" +
        "and paste the result with Set token above.";
    } else {
      hint =
        "That dev token was rejected — it may have expired (they last 24h), or " +
        "the account it was signed for is not on the allowlist.\n" +
        "Generate a fresh one with: python -m scripts.make_init_data";
    }
    const error = new Error(hint);
    error.unauthorised = true;
    throw error;
  }
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    let payload = null;
    try {
      const body = await response.json();
      payload = body && body.detail;
      if (typeof payload === "string") detail = payload;
      else if (payload && payload.error) detail = payload.error;
    } catch (err) {
      /* response had no JSON body; keep the generic message */
    }
    const error = new Error(detail);
    // The planner returns structured detail (which links were unusable, and
    // why), so keep it rather than flattening to a sentence.
    error.detail = payload;
    throw error;
  }
  return response.status === 204 ? null : response.json();
}

const fetchLinks = () => api("/links");
const fetchDates = () => api("/dates");
const createDate = (body) => api("/dates", { method: "POST", body: JSON.stringify(body) });
const patchDate = (id, body) =>
  api(`/dates/${id}`, { method: "PATCH", body: JSON.stringify(body) });
const removeDate = (id) => api(`/dates/${id}`, { method: "DELETE" });
const fetchCalendar = (month) => api(`/calendar?month=${encodeURIComponent(month)}`);
const fetchDatesInMonth = (month) =>
  api(`/dates/in-month?month=${encodeURIComponent(month)}`);
const saveCalendarNote = (day, note, milestones) =>
  api(`/calendar/${day}`, {
    method: "PUT",
    body: JSON.stringify({ note, milestones: milestones || [] }),
  });
const fetchSettings = () => api("/settings");
const putSettings = (body) => api("/settings", { method: "PUT", body: JSON.stringify(body) });
// "Plan with all" means every eligible link is considered; the planner then
// builds the most coherent day from them rather than cramming all of them in.
const requestPlan = (linkIds) =>
  api("/plan", { method: "POST", body: JSON.stringify({ link_ids: linkIds || null }) });
const sendPlanToGroup = (planId) => api(`/plans/${planId}/post`, { method: "POST" });
const patchLink = (id, changes) =>
  api(`/links/${id}`, { method: "PATCH", body: JSON.stringify(changes) });
const createLink = (body) => api("/links", { method: "POST", body: JSON.stringify(body) });
const removeLink = (id) => api(`/links/${id}`, { method: "DELETE" });
const fetchTaxonomy = () => api("/taxonomy");
const deletePhoto = (linkId, photoId) =>
  api(`/links/${linkId}/photos/${photoId}`, { method: "DELETE" });

// --- Photos ---------------------------------------------------------------
//
// Photo bytes are behind the same initData header as every other endpoint, and
// an <img src> cannot send a header - so an <img> pointed straight at the API
// would render a broken icon. Each image is fetched with the header instead and
// handed to the <img> as a blob URL.
//
// The alternative, a signed token in the query string, was rejected: it puts a
// credential in a URL that ends up in logs and history, to save a fetch.
//
// Blob URLs are cached by path and never revoked. The cache holds a handful of
// images for one couple's saved links, and re-rendering the list on every
// filter change would otherwise re-download all of them.

const photoUrls = new Map(); // API path -> Promise<blob URL>

function photoBlobUrl(path) {
  if (!photoUrls.has(path)) {
    const pending = fetch(`${API_BASE}${path.replace(API_BASE, "")}`, {
      headers: { "X-Telegram-Init-Data": tg.initData || "" },
    })
      .then((response) => {
        if (!response.ok) throw new Error(`photo failed (${response.status})`);
        return response.blob();
      })
      .then((blob) => URL.createObjectURL(blob))
      .catch((err) => {
        // Drop the failed attempt so a later render can retry: a cold Render
        // service can time out the first request and answer the second.
        photoUrls.delete(path);
        throw err;
      });
    photoUrls.set(path, pending);
  }
  return photoUrls.get(path);
}

// innerHTML cannot carry a blob URL that does not exist yet, so images are
// rendered as empty placeholders and filled in afterwards.
function hydratePhotos(root) {
  (root || document).querySelectorAll("img[data-photo]").forEach((img) => {
    if (img.dataset.loaded === "1") return;
    img.dataset.loaded = "1";
    photoBlobUrl(img.dataset.photo)
      .then((url) => {
        img.src = url;
      })
      .catch(() => {
        // A photo that will not load must not leave a grey box implying one is
        // coming. Remove the frame and let the card stand on its text.
        img.dataset.loaded = "";
        const frame = img.closest(".thumbs") || img;
        frame.remove();
      });
  });
}

// Photos are stored as bytes in Postgres rather than as Telegram file_ids,
// because the bot must not post the couple's photos into their own group just
// to mint an id. Bytes cost database, and a modern phone camera produces 3-6MB
// files, so the image is downscaled here before it is sent. A long edge of
// 1600px is more than a card thumbnail or the full-size viewer can show on a
// phone, and it lands a typical photo in the low hundreds of KB.
const PHOTO_MAX_EDGE = 1600;
const PHOTO_QUALITY = 0.85;

async function downscale(file) {
  // Best effort. createImageBitmap and canvas.toBlob are widely supported, but
  // if either is missing or the file is not decodable, sending the original is
  // better than refusing the upload - the server's size cap is the backstop.
  try {
    if (!file.type.startsWith("image/")) return file;
    const bitmap = await createImageBitmap(file);
    const longest = Math.max(bitmap.width, bitmap.height);
    const scale = Math.min(1, PHOTO_MAX_EDGE / longest);
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bitmap.width * scale);
    canvas.height = Math.round(bitmap.height * scale);
    canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    bitmap.close?.();
    const blob = await new Promise((resolve) =>
      canvas.toBlob(resolve, "image/jpeg", PHOTO_QUALITY)
    );
    // Re-encoding a small screenshot can make it bigger; keep whichever wins.
    if (!blob || blob.size >= file.size) return file;
    return new File([blob], "photo.jpg", { type: "image/jpeg" });
  } catch (err) {
    console.warn("[planner] could not downscale; sending the original", err);
    return file;
  }
}

async function uploadPhoto(linkId, file) {
  // Not api(): that sets a JSON content type, and multipart needs the boundary
  // the browser generates.
  const body = new FormData();
  body.append("file", file);
  const response = await fetch(`${API_BASE}/links/${linkId}/photos`, {
    method: "POST",
    headers: { "X-Telegram-Init-Data": tg.initData || "" },
    body,
  });
  if (!response.ok) {
    let detail = `Upload failed (${response.status})`;
    try {
      const payload = await response.json();
      if (typeof payload.detail === "string") detail = payload.detail;
    } catch (err) {
      /* no JSON body; keep the generic message */
    }
    throw new Error(detail);
  }
  return response.json();
}

function photosOfKind(link, kind) {
  return (link.photos || []).filter((photo) => photo.kind === kind);
}

// What a card shows. A finished outing leads with the photos from the day; one
// still to visit leads with the screenshot, which is usually the menu or poster
// that made it worth saving.
function cardPhotos(link) {
  const visit = photosOfKind(link, "visit");
  if (link.done && visit.length) return visit;
  return photosOfKind(link, "intake");
}

function thumbsHtml(photos, limit) {
  if (!photos.length) return "";
  const shown = photos.slice(0, limit);
  const extra = photos.length - shown.length;
  const tiles = shown
    .map(
      (photo) =>
        `<button type="button" class="thumb" data-photo-id="${photo.id}" aria-label="Open photo">` +
        `<img data-photo="${escapeHtml(photo.thumb_url)}" alt="" /></button>`
    )
    .join("");
  const more = extra > 0 ? `<span class="thumbs__more">+${extra}</span>` : "";
  return `<div class="thumbs">${tiles}${more}</div>`;
}

// --- Rendering ------------------------------------------------------------

function escapeHtml(value) {
  // Captions are arbitrary user text from TikTok/Instagram, so everything goes
  // through here before touching innerHTML.
  return String(value ?? "").replace(/[&<>"']/g, (char) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char])
  );
}

function displayTitle(link) {
  if (link.title) return link.title;
  // Photo posts often have no metadata; show a readable fragment of the URL.
  try {
    const url = new URL(link.canonical_url || link.url);
    return `${url.hostname.replace(/^www\./, "")}${url.pathname}`;
  } catch (err) {
    return link.url;
  }
}

function formatDate(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

function isExpired(link) {
  // Expired items are de-emphasised rather than hidden or deleted.
  if (!link.event_end || link.done) return false;
  return new Date(link.event_end) < new Date(new Date().toDateString());
}

function cardHtml(link) {
  const title = escapeHtml(displayTitle(link));
  const platform = escapeHtml(PLATFORM_LABELS[link.platform] || link.platform);
  const caption = link.caption ? escapeHtml(link.caption.slice(0, 140)) : "";
  const expired = isExpired(link);

  // A manual entry has no platform, so it gets no source badge rather than an
  // empty one.
  const meta = link.platform
    ? [`<span class="badge badge--${escapeHtml(link.platform)}">${platform}</span>`]
    : [];
  // Geocoding ran and failed, which silently removes this from every plan.
  // A button, not a label: it opens the fix.
  if (link.needs_location) {
    meta.push(
      `<button type="button" class="badge badge--attention" data-fix-location="1">` +
        `Location not found · fix</button>`
    );
  }
  // Says why there is no location, rather than leaving a card that looks
  // half-filled in.
  if (link.is_collection) {
    meta.push(`<span class="badge badge--collection">Collection</span>`);
  }
  if (link.is_day_trip && link.region) {
    meta.push(`<span class="badge badge--daytrip">${escapeHtml(link.region)}</span>`);
  }
  if (link.location) meta.push(`<span class="meta__item">📍 ${escapeHtml(link.location)}</span>`);
  if (link.event_end) {
    const window_ = link.event_start ? `${link.event_start} → ${link.event_end}` : `until ${link.event_end}`;
    meta.push(`<span class="meta__item meta__item--dates">🗓 ${escapeHtml(window_)}</span>`);
  }
  if (link.added_at) meta.push(`<span class="meta__item">${escapeHtml(formatDate(link.added_at))}</span>`);
  if (expired) meta.push(`<span class="meta__item meta__item--warn">expired</span>`);

  if (link.category && link.category !== "other") {
    const sub = link.subcategory && link.subcategory !== "other" ? ` · ${link.subcategory}` : "";
    meta.push(
      `<span class="badge badge--category">${escapeHtml(CATEGORY_LABELS[link.category] || link.category)}${escapeHtml(sub)}</span>`
    );
  }

  const tagChips = (link.tags || [])
    .map(
      (tag) =>
        `<button type="button" class="tag${state.tag === tag ? " is-active" : ""}" data-tag="${escapeHtml(tag)}">${escapeHtml(tag)}</button>`
    )
    .join("");

  const doneDetails = [];
  if (link.rating) doneDetails.push(`<span class="chip chip--rating">${link.rating}/10</span>`);
  if (link.note) doneDetails.push(`<span class="chip">${escapeHtml(link.note)}</span>`);

  // In selection mode the whole card is the hit target, so the checkbox is an
  // indicator rather than a small thing to aim at on a phone.
  const selected = state.selected.has(link.id);
  const checkbox = state.selecting
    ? `<span class="card__check${selected ? " is-checked" : ""}" aria-hidden="true"></span>`
    : "";

  // Capped at three: a card is a glance, and an album belongs in the sheet.
  const thumbs = thumbsHtml(cardPhotos(link), 3);

  return `
    <article class="card${link.done ? " card--done" : ""}${expired ? " card--expired" : ""}${
      state.selecting ? " card--selectable" : ""
    }${selected ? " card--selected" : ""}${thumbs ? " card--has-photo" : ""}" data-id="${link.id}">
      ${checkbox}
      ${thumbs}
      <div class="card__body">
        <h2 class="card__title">${title}</h2>
        ${caption ? `<p class="card__caption">${caption}</p>` : ""}
        <div class="card__meta">${meta.join("")}</div>
        ${tagChips ? `<div class="card__tags">${tagChips}</div>` : ""}
        ${doneDetails.length ? `<div class="card__done-details">${doneDetails.join("")}</div>` : ""}
      </div>
      <div class="card__actions">
        ${
          // No post behind a manual entry, so no link to open. Rendering a
          // dead "Open" would be worse than rendering nothing.
          link.url
            ? `<a class="btn btn--ghost btn--sm" href="${escapeHtml(link.url)}" target="_blank" rel="noopener noreferrer">Open</a>`
            : ""
        }
        <button class="btn btn--ghost btn--sm" data-action="edit">Edit</button>
        <button class="btn btn--sm ${link.done ? "btn--ghost" : "btn--primary"}" data-action="${link.done ? "undone" : "done"}">
          ${link.done ? "Undo" : "Done"}
        </button>
      </div>
    </article>
  `;
}

function setStatus(message, kind = "info") {
  if (!message) {
    els.status.hidden = true;
    els.status.textContent = "";
    return;
  }
  els.status.hidden = false;
  els.status.className = `status status--${kind}`;
  els.status.textContent = message;
}

function matchesFilters(link) {
  if (state.category !== "all" && (link.category || "other") !== state.category) return false;
  if (state.subcategory !== "all" && (link.subcategory || "other") !== state.subcategory) return false;
  if (state.tag && !(link.tags || []).includes(state.tag)) return false;
  return true;
}

function chipHtml(label, count, active, dataset) {
  const attrs = Object.entries(dataset)
    .map(([key, value]) => `data-${key}="${escapeHtml(value)}"`)
    .join(" ");
  return `<button type="button" class="chip-btn${active ? " is-active" : ""}" ${attrs}>
    ${escapeHtml(label)}<span class="chip-btn__count">${count}</span>
  </button>`;
}

function renderFilters(tabLinks) {
  // Counts come from the current tab's links, before category/tag filtering,
  // so switching chips never shows a count that cannot be reached.
  const counts = {};
  tabLinks.forEach((link) => {
    const category = link.category || "other";
    counts[category] = (counts[category] || 0) + 1;
  });

  const categories = Object.keys(CATEGORY_LABELS).filter((c) => counts[c]);
  els.categoryFilters.innerHTML =
    chipHtml("All", tabLinks.length, state.category === "all", { filter: "category", value: "all" }) +
    categories
      .map((c) => chipHtml(CATEGORY_LABELS[c], counts[c], state.category === c, { filter: "category", value: c }))
      .join("");

  // Subcategories only make sense once a category is chosen.
  if (state.category === "all") {
    els.subcategoryFilters.hidden = true;
    els.subcategoryFilters.innerHTML = "";
  } else {
    const subCounts = {};
    tabLinks
      .filter((link) => (link.category || "other") === state.category)
      .forEach((link) => {
        const sub = link.subcategory || "other";
        subCounts[sub] = (subCounts[sub] || 0) + 1;
      });
    const subs = Object.keys(subCounts).sort();
    els.subcategoryFilters.hidden = subs.length <= 1;
    els.subcategoryFilters.innerHTML =
      chipHtml("All", tabLinks.filter((l) => (l.category || "other") === state.category).length,
        state.subcategory === "all", { filter: "subcategory", value: "all" }) +
      subs
        .map((s) => chipHtml(s, subCounts[s], state.subcategory === s, { filter: "subcategory", value: s }))
        .join("");
  }

  if (state.tag) {
    els.activeFilters.hidden = false;
    els.activeFilters.innerHTML =
      `<button type="button" class="chip-btn chip-btn--clear" data-filter="tag" data-value="">
        tag: ${escapeHtml(state.tag)} ✕
      </button>`;
  } else {
    els.activeFilters.hidden = true;
    els.activeFilters.innerHTML = "";
  }
}

function ordinal(n) {
  if (n % 100 >= 10 && n % 100 <= 20) return `${n}th`;
  return `${n}${{ 1: "st", 2: "nd", 3: "rd" }[n % 10] || "th"}`;
}

function describeWhen(days) {
  if (days === 0) return "today";
  if (days === 1) return "tomorrow";
  if (days < 7) return `in ${days} days`;
  if (days < 14) return "next week";
  return `in ${days} days`;
}

function describeCount(entry) {
  if (!entry.count) return "";
  return entry.recurrence === "monthly"
    ? ` · ${entry.count} months`
    : ` · ${ordinal(entry.count)}`;
}

function bannerEntries() {
  // Two slots, not one. A monthsary recurs every month, so it is nearly always
  // sooner than the anniversary - showing only the nearest date would hide the
  // yearly one permanently, which is backwards.
  const monthly = state.dates.find((d) => d.recurrence === "monthly");
  const other = state.dates.find((d) => d.recurrence !== "monthly");
  return [other, monthly].filter(Boolean).sort((a, b) => a.days_until - b.days_until);
}

function renderDateBanner() {
  const entries = bannerEntries();
  if (!entries.length) {
    els.dateBanner.hidden = true;
    els.dateBanner.innerHTML = "";
    return;
  }
  els.dateBanner.hidden = false;
  els.dateBanner.className = "date-banner";
  els.dateBanner.innerHTML = entries
    .map(
      (e) => `
    <div class="date-banner__row${e.days_until === 0 ? " date-banner__row--today" : ""}">
      <span class="date-banner__label">${escapeHtml(e.label)}${escapeHtml(describeCount(e))}</span>
      <span class="date-banner__when">${escapeHtml(describeWhen(e.days_until))}</span>
    </div>`
    )
    .join("");
}

function render() {
  renderDateBanner();

  // The calendar replaces the list entirely: filters, counts and the plan
  // buttons are all about links and mean nothing here.
  const onCalendar = state.tab === "calendar";
  const onSettings = state.tab === "settings";
  const onLinks = !onCalendar && !onSettings;
  els.calendar.hidden = !onCalendar;
  els.settingsPanel.hidden = !onSettings;
  els.list.hidden = !onLinks;
  document.querySelector(".filters").hidden = !onLinks;
  document.querySelector(".actions").hidden = !onLinks;
  if (onCalendar) {
    setStatus("");
    renderDatesList();
    return;
  }
  if (onSettings) {
    setStatus("");
    return;
  }

  const done = state.links.filter((link) => link.done);
  const outstanding = state.links.filter((link) => !link.done);
  // Day trips are split out of "To visit" so the main list stays the set the
  // Saturday planner will actually cluster. is_day_trip is computed by the
  // API, so the rule is not duplicated here.
  const dayTrips = outstanding.filter((link) => link.is_day_trip);
  const todo = outstanding.filter((link) => !link.is_day_trip);

  els.countTodo.textContent = todo.length;
  els.countDayTrip.textContent = dayTrips.length;
  els.countDone.textContent = done.length;
  // Links the planner will drop for want of coordinates. Counted across every
  // tab, not just the visible one: the whole problem is that they are easy to
  // miss.
  const unplaceable = state.links.filter((link) => link.needs_location).length;
  els.subtitle.textContent =
    `${state.links.length} saved · ${done.length} visited` +
    (unplaceable ? ` · ${unplaceable} need a location` : "");

  const byTab = { todo, daytrip: dayTrips, done };
  const tabLinks = byTab[state.tab] || todo;
  renderFilters(tabLinks);

  const visible = tabLinks.filter(matchesFilters);
  els.list.innerHTML = visible.map(cardHtml).join("");
  hydratePhotos(els.list);

  const filtered = visible.length !== tabLinks.length || state.category !== "all" || state.tag;
  const emptyMessage = filtered
    ? "Nothing matches these filters."
    : {
        todo: "Nothing to visit yet. Paste a TikTok or Instagram link in the group.",
        daytrip: "No day trips saved. Links outside Singapore show up here.",
        done: "Nothing marked done yet.",
      }[state.tab];

  setStatus(visible.length === 0 ? emptyMessage : "", "empty");
}

// --- Done sheet -----------------------------------------------------------

function buildRatingButtons() {
  els.rating.innerHTML = Array.from({ length: 10 }, (_, index) => {
    const value = index + 1;
    return `<button type="button" class="rating__btn" data-value="${value}">${value}</button>`;
  }).join("");
}

function selectedRating() {
  const active = els.rating.querySelector(".rating__btn.is-active");
  return active ? Number(active.dataset.value) : null;
}

function renderSheetPhotos() {
  const link = state.pending;
  // While creating, the entry has no id yet, so the picked files are shown
  // from local object URLs and uploaded once the row exists.
  if (!link) {
    els.sheetPhotos.innerHTML = state.pendingPhotos.length
      ? state.pendingPhotos
          .map(
            (file, index) =>
              `<span class="thumb thumb--pending"><img src="${URL.createObjectURL(file)}" alt="" />` +
              `<button type="button" class="thumb__remove" data-drop-pending="${index}" ` +
              `aria-label="Remove photo">&times;</button></span>`
          )
          .join("")
      : `<p class="field__hint">Photos upload when you save.</p>`;
    return;
  }
  // Marking something done is about the visit, so only the memories show.
  // Editing is about correcting the entry, and a screenshot of the wrong post
  // is exactly the kind of thing that needs removing - so edit mode shows both.
  const shown =
    state.sheetMode === "edit"
      ? [...photosOfKind(link, "intake"), ...photosOfKind(link, "visit")]
      : photosOfKind(link, "visit");
  els.sheetPhotos.innerHTML = shown.length
    ? shown
        .map(
          (photo) =>
            `<span class="thumb-wrap">` +
            `<button type="button" class="thumb" data-photo-id="${photo.id}" aria-label="Open photo">` +
            `<img data-photo="${escapeHtml(photo.thumb_url)}" alt="" /></button>` +
            `<button type="button" class="thumb__remove" data-delete-photo="${photo.id}" ` +
            `aria-label="Delete ${photo.kind === "intake" ? "screenshot" : "photo"}">&times;</button>` +
            (photo.kind === "intake"
              ? `<span class="thumb__kind">screenshot</span>`
              : "") +
            `</span>`
        )
        .join("")
    : `<p class="field__hint">${
        state.sheetMode === "edit" ? "No photos yet." : "No photos from this visit yet."
      }</p>`;
  hydratePhotos(els.sheetPhotos);
}

// --- Category pickers -----------------------------------------------------

function renderCategoryPicker() {
  const categories = (state.taxonomy && state.taxonomy.categories) || [];
  els.entryCategory.innerHTML = categories
    .map(
      (name) =>
        `<button type="button" class="chip-btn${state.entryCategory === name ? " is-active" : ""}" ` +
        `data-entry-category="${escapeHtml(name)}">${escapeHtml(CATEGORY_LABELS[name] || name)}</button>`
    )
    .join("");
  renderSubcategoryPicker();
}

function renderSubcategoryPicker() {
  const subs =
    (state.taxonomy &&
      state.taxonomy.subcategories &&
      state.taxonomy.subcategories[state.entryCategory]) ||
    [];
  // A subcategory only means anything under a category, so the row disappears
  // rather than offering choices that would be rejected on save.
  els.entrySubcategoryField.hidden = subs.length === 0;
  els.entrySubcategory.innerHTML = subs
    .map(
      (name) =>
        `<button type="button" class="chip-btn${state.entrySubcategory === name ? " is-active" : ""}" ` +
        `data-entry-subcategory="${escapeHtml(name)}">${escapeHtml(name)}</button>`
    )
    .join("");
}

// A collection has no location, so the two location fields stop asking for
// one. Disabled rather than hidden: the values are kept, and un-ticking the box
// brings them straight back rather than looking like they were discarded.
function syncCollectionFields() {
  const isCollection = els.entryCollection.checked;
  els.entryLocation.disabled = isCollection;
  els.entryHint.disabled = isCollection;
  els.entryLocation.closest(".field").classList.toggle("field--muted", isCollection);
  els.entryHint.closest(".field").classList.toggle("field--muted", isCollection);
}

function setRating(value) {
  els.rating.querySelectorAll(".rating__btn").forEach((btn) => {
    btn.classList.toggle("is-active", Number(btn.dataset.value) === value);
  });
}

/**
 * One sheet, three modes.
 *   done   - mark visited: rating, note, photos only
 *   edit   - everything, on an entry that already exists
 *   create - everything, on a place with no link behind it
 */
function openSheet(link, mode = "done") {
  state.pending = link;
  state.sheetMode = mode;
  state.pendingPhotos = [];

  const editing = mode !== "done";
  els.entryFields.hidden = !editing;
  els.entryDoneField.hidden = mode !== "create" && mode !== "edit";
  els.entryCollectionField.hidden = els.entryDoneField.hidden;
  // Edit only. "create" has nothing to delete yet, and the done sheet is a
  // deliberate two-tap flow that should not grow a destructive button.
  els.entryDelete.hidden = mode !== "edit" || !link;
  els.entryDelete.disabled = false;
  els.entryDelete.textContent = "Delete";
  els.sheetTitle.textContent =
    mode === "create" ? "Add a place" : mode === "edit" ? "Edit" : "Mark as done";
  els.sheetPlace.textContent = link ? displayTitle(link) : "Somewhere with no link";

  els.entryTitle.value = (link && link.title) || "";
  els.entryLocation.value = (link && link.location) || "";
  els.entryHint.value = (link && link.geocode_hint) || "";
  els.entryTags.value = ((link && link.tags) || []).join(", ");
  els.note.value = (link && link.note) || "";
  state.entryCategory = (link && link.category) || null;
  state.entrySubcategory = (link && link.subcategory) || null;
  renderCategoryPicker();
  setRating(link ? link.rating : null);
  // A place added by hand is usually one already visited, so this starts on.
  els.entryDone.checked = link ? !!link.done : true;
  els.entryCollection.checked = link ? !!link.is_collection : false;
  syncCollectionFields();

  renderSheetPhotos();
  els.photoInput.value = "";
  els.sheet.hidden = false;
  if (!state.taxonomy && editing) loadTaxonomy();
}

async function loadTaxonomy() {
  try {
    state.taxonomy = await fetchTaxonomy();
    renderCategoryPicker();
  } catch (err) {
    // The categories are closed, so guessing them client-side is not an
    // option; say so rather than silently offering none.
    setStatus(`Could not load categories: ${err.message}`, "error");
  }
}

function openEditFor(linkId, focusHint = false) {
  const link = state.links.find((item) => item.id === Number(linkId));
  if (!link) return;
  openSheet(link, "edit");
  if (focusHint) {
    els.entryHint.focus();
    els.entryHint.scrollIntoView({ block: "center" });
  }
}

// Photos upload as soon as they are picked rather than on Save. Saving is a
// PATCH of rating and note; an upload goes to Telegram and can take seconds on
// a phone, and burying that inside Save would make Save look stuck.
async function addPhotos(files) {
  const link = state.pending;
  if (!files.length) return;
  if (!link) {
    // Creating: no id to attach to yet. Hold them and upload after the save.
    state.pendingPhotos.push(...files);
    renderSheetPhotos();
    els.photoInput.value = "";
    return;
  }
  els.photoAdd.disabled = true;
  const original = els.photoAdd.textContent;
  let added = 0;
  try {
    for (const [index, file] of files.entries()) {
      els.photoAdd.textContent =
        files.length > 1 ? `Uploading ${index + 1}/${files.length}…` : "Uploading…";
      const photo = await uploadPhoto(link.id, await downscale(file));
      link.photos = [...(link.photos || []), photo];
      added += 1;
      renderSheetPhotos();
    }
    setStatus("");
    tg.HapticFeedback.notificationOccurred("success");
  } catch (err) {
    // Say how many made it: stopping at the third of five is not a total
    // failure, and re-picking all five would duplicate the first two.
    setStatus(added ? `${err.message} (${added} uploaded first)` : err.message, "error");
  } finally {
    els.photoAdd.disabled = false;
    els.photoAdd.textContent = original;
    els.photoInput.value = "";
  }
}

function closeSheet() {
  els.sheet.hidden = true;
  state.pending = null;
  // Files picked but never saved must not reappear on the next entry.
  state.pendingPhotos = [];
  state.sheetMode = "done";
}

// --- Photo viewer ---------------------------------------------------------

function openPhoto(photoId) {
  const photo = state.links
    .flatMap((link) => link.photos || [])
    .find((candidate) => candidate.id === Number(photoId));
  if (!photo) return;
  // The viewer asks for the full size; the card only ever loaded a thumbnail.
  els.viewerImg.removeAttribute("src");
  els.viewerImg.dataset.photo = photo.url;
  els.viewerImg.dataset.loaded = "";
  els.viewer.hidden = false;
  hydratePhotos(els.viewer);
}

function closePhoto() {
  els.viewer.hidden = true;
  els.viewerImg.removeAttribute("src");
}

// --- Events ---------------------------------------------------------------

function onFilterClick(event) {
  const button = event.target.closest(".chip-btn");
  if (!button) return;
  const { filter, value } = button.dataset;
  if (filter === "category") {
    state.category = value;
    // A subcategory from the previous category would match nothing.
    state.subcategory = "all";
  } else if (filter === "subcategory") {
    state.subcategory = value;
  } else if (filter === "tag") {
    state.tag = value || null;
  }
  render();
}

els.categoryFilters.addEventListener("click", onFilterClick);
els.subcategoryFilters.addEventListener("click", onFilterClick);
els.activeFilters.addEventListener("click", onFilterClick);

els.tabs.forEach((tab) => {
  tab.addEventListener("click", () => {
    state.tab = tab.dataset.tab;
    // Filters are per-view: carrying them across tabs strands the user on an
    // empty list with no obvious cause.
    state.category = "all";
    state.subcategory = "all";
    state.tag = null;
    if (state.selecting) setSelecting(false);
    // Load the month the first time the calendar is opened, not on every
    // visit to another tab.
    if (state.tab === "calendar" && !state.calMonth) loadCalendar();
    if (state.tab === "settings" && !state.settings) loadSettingsPanel();
    els.tabs.forEach((other) => {
      const active = other === tab;
      other.classList.toggle("is-active", active);
      other.setAttribute("aria-selected", String(active));
    });
    render();
  });
});

// --- Settings ---------------------------------------------------------------

async function loadSettingsPanel() {
  try {
    const s = await fetchSettings();
    state.settings = s;
    els.setMaxStops.value = s.max_stops;
    els.setRadius.value = s.cluster_radius_metres;
    els.setRegion.value = s.home_region;
    const d = s.defaults || {};
    els.setStopsDefault.textContent = `default ${d.max_stops}`;
    els.setRadiusDefault.textContent = `default ${d.cluster_radius_metres}`;
    els.setRegionDefault.textContent = `default ${d.home_region}`;
    els.settingsStatus.textContent = "";
  } catch (err) {
    els.settingsStatus.textContent = err.message;
  }
}

els.settingsSave.addEventListener("click", async () => {
  els.settingsSave.disabled = true;
  els.settingsStatus.textContent = "Saving…";
  try {
    const saved = await putSettings({
      max_stops: Number(els.setMaxStops.value),
      cluster_radius_metres: Number(els.setRadius.value),
      home_region: els.setRegion.value.trim(),
    });
    state.settings = saved;
    els.settingsStatus.textContent = "Saved";
    tg.HapticFeedback.notificationOccurred("success");
    // The home region decides what counts as a day trip, so the lists are
    // stale the moment it changes.
    await load();
  } catch (err) {
    els.settingsStatus.textContent = err.message;
  } finally {
    els.settingsSave.disabled = false;
  }
});

// --- Reminder milestones ----------------------------------------------------

// Offered everywhere a reminder can be set. Mirrors AVAILABLE_MILESTONES on the
// server, which validates the choice; this list only decides what is offered.
const MILESTONE_CHOICES = [
  { days: 30, label: "30d" },
  { days: 14, label: "14d" },
  { days: 7, label: "7d" },
  { days: 3, label: "3d" },
  { days: 1, label: "1d" },
  { days: 0, label: "on the day" },
];

function renderMilestonePicker(container, selected) {
  const chosen = new Set(selected || []);
  container.innerHTML = MILESTONE_CHOICES.map(
    (m) =>
      `<button type="button" class="chip-btn${chosen.has(m.days) ? " is-active" : ""}"
        data-milestone="${m.days}">${escapeHtml(m.label)}</button>`
  ).join("");
}

function readMilestonePicker(container) {
  return [...container.querySelectorAll(".chip-btn.is-active")].map((b) =>
    Number(b.dataset.milestone)
  );
}

function wireMilestonePicker(container) {
  container.addEventListener("click", (event) => {
    const chip = event.target.closest("[data-milestone]");
    if (!chip) return;
    chip.classList.toggle("is-active");
  });
}

wireMilestonePicker(document.getElementById("date-milestones"));
wireMilestonePicker(document.getElementById("day-milestones"));

function describeMilestones(days) {
  if (!days || !days.length) return "no reminders";
  return days
    .slice()
    .sort((a, b) => b - a)
    .map((d) => (d === 0 ? "on the day" : `${d}d`))
    .join(", ");
}


// --- Dates editor -----------------------------------------------------------

const RECURRENCE_CHOICES = [
  { value: "once", label: "Once" },
  { value: "monthly", label: "Monthly" },
  { value: "yearly", label: "Yearly" },
];

function renderRecurrencePicker(selected) {
  els.dateRecurrence.innerHTML = RECURRENCE_CHOICES.map(
    (r) =>
      `<button type="button" class="chip-btn${r.value === selected ? " is-active" : ""}"
        data-recurrence="${r.value}">${r.label}</button>`
  ).join("");
}

els.dateRecurrence.addEventListener("click", (event) => {
  const chip = event.target.closest("[data-recurrence]");
  if (!chip) return;
  // Exactly one, unlike milestones which are a set.
  els.dateRecurrence.querySelectorAll(".chip-btn").forEach((b) => b.classList.remove("is-active"));
  chip.classList.add("is-active");
});

function selectedRecurrence() {
  const active = els.dateRecurrence.querySelector(".chip-btn.is-active");
  return active ? active.dataset.recurrence : "once";
}

function renderDatesList() {
  if (!state.dates.length) {
    els.datesList.innerHTML =
      '<p class="dates__empty">No dates yet. Add an anniversary or a monthsary.</p>';
    return;
  }
  els.datesList.innerHTML = state.dates
    .map(
      (d) => `
    <button type="button" class="date-row" data-date-id="${d.id}">
      <span class="date-row__main">
        <span class="date-row__label">${escapeHtml(d.label)}${escapeHtml(describeCount(d))}</span>
        <span class="date-row__meta">${escapeHtml(d.recurrence)} · ${escapeHtml(
        describeWhen(d.days_until)
      )} · ${escapeHtml(describeMilestones(d.milestones))}</span>
      </span>
      <span class="date-row__when">${escapeHtml(d.occurs_on)}</span>
    </button>`
    )
    .join("");
}

function openDateSheet(entry) {
  state.editingDate = entry || null;
  els.dateSheetTitle.textContent = entry ? "Edit date" : "Add a date";
  els.dateLabel.value = entry ? entry.label : "";
  els.dateValue.value = entry ? entry.date : "";
  renderRecurrencePicker(entry ? entry.recurrence : "once");
  renderMilestonePicker(els.dateMilestones, entry ? entry.milestones : [7, 1, 0]);
  els.dateDelete.hidden = !entry;
  els.dateSave.disabled = false;
  els.dateSave.textContent = "Save";
  els.dateSheet.hidden = false;
}

function closeDateSheet() {
  els.dateSheet.hidden = true;
  state.editingDate = null;
}

els.dateAdd.addEventListener("click", () => openDateSheet(null));
els.datesList.addEventListener("click", (event) => {
  const row = event.target.closest("[data-date-id]");
  if (!row) return;
  const entry = state.dates.find((d) => d.id === Number(row.dataset.dateId));
  if (entry) openDateSheet(entry);
});
els.dateSheet.addEventListener("click", (event) => {
  if (event.target.dataset.closeDate) closeDateSheet();
});

els.dateForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const label = els.dateLabel.value.trim();
  const when = els.dateValue.value;
  if (!label || !when) {
    setStatus("A date needs a name and a day.", "error");
    return;
  }
  const body = {
    label,
    date: when,
    recurrence: selectedRecurrence(),
    milestones: readMilestonePicker(els.dateMilestones),
  };
  els.dateSave.disabled = true;
  els.dateSave.textContent = "Saving…";
  try {
    if (state.editingDate) await patchDate(state.editingDate.id, body);
    else await createDate(body);
    closeDateSheet();
    await reloadDates();
    if (state.calMonth) await loadCalendar();
    tg.HapticFeedback.impactOccurred("light");
  } catch (err) {
    els.dateSave.disabled = false;
    els.dateSave.textContent = "Save";
    setStatus(err.message, "error");
  }
});

els.dateDelete.addEventListener("click", async () => {
  if (!state.editingDate) return;
  const entry = state.editingDate;
  els.dateDelete.disabled = true;
  try {
    await removeDate(entry.id);
    closeDateSheet();
    await reloadDates();
    if (state.calMonth) await loadCalendar();
  } catch (err) {
    setStatus(err.message, "error");
  } finally {
    els.dateDelete.disabled = false;
  }
});

async function reloadDates() {
  try {
    state.dates = await fetchDates();
  } catch (err) {
    setStatus(err.message, "error");
    return;
  }
  renderDatesList();
  renderDateBanner();
}

// --- Shared calendar --------------------------------------------------------

const MONTH_NAMES = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

function pad(n) {
  return String(n).padStart(2, "0");
}

function monthKey({ year, month }) {
  return `${year}-${pad(month)}`;
}

function shiftMonth({ year, month }, delta) {
  const m = month - 1 + delta;
  return { year: year + Math.floor(m / 12), month: ((m % 12) + 12) % 12 + 1 };
}

async function loadCalendar() {
  if (!state.calMonth) {
    const now = new Date();
    state.calMonth = { year: now.getFullYear(), month: now.getMonth() + 1 };
  }
  const key = monthKey(state.calMonth);
  try {
    // Anniversaries are shown alongside notes but are not notes: they come
    // from the Dates section, which stays their only editor.
    const [notes, dates] = await Promise.all([
      fetchCalendar(key),
      fetchDatesInMonth(key).catch(() => []),
    ]);
    state.calNotes = {};
    notes.forEach((n) => {
      (state.calNotes[n.day] = state.calNotes[n.day] || []).push(n);
    });
    state.calDates = {};
    dates.forEach((d) => {
      (state.calDates[d.day] = state.calDates[d.day] || []).push(d);
    });
    setStatus("");
  } catch (err) {
    setStatus(err.message, "error");
    state.calNotes = {};
    state.calDates = {};
  }
  renderCalendar();
}

function renderCalendar() {
  const { year, month } = state.calMonth;
  els.calMonthLabel.textContent = `${MONTH_NAMES[month - 1]} ${year}`;

  const first = new Date(year, month - 1, 1);
  // Monday-first, which is how a week is read here.
  const leading = (first.getDay() + 6) % 7;
  const days = new Date(year, month, 0).getDate();
  const todayKey = (() => {
    const t = new Date();
    return `${t.getFullYear()}-${pad(t.getMonth() + 1)}-${pad(t.getDate())}`;
  })();

  const cells = [];
  for (let i = 0; i < leading; i++) {
    cells.push('<div class="cal-cell cal-cell--empty"></div>');
  }
  for (let d = 1; d <= days; d++) {
    const key = `${year}-${pad(month)}-${pad(d)}`;
    const notes = state.calNotes[key] || [];
    const dates = state.calDates[key] || [];
    const mine = notes.find((n) => n.is_mine);
    const theirs = notes.find((n) => !n.is_mine);
    // A dot per author, and a heart for a stored date, which is a different
    // kind of thing from a note someone typed.
    const dots =
      (mine ? '<span class="dot dot--mine"></span>' : "") +
      (theirs ? '<span class="dot dot--theirs"></span>' : "") +
      (dates.length ? '<span class="cal-cell__event" aria-hidden="true">♥</span>' : "");
    const preview = dates.length
      ? `<span class="cal-cell__preview cal-cell__preview--event">${escapeHtml(dates[0].label)}</span>`
      : notes.length
      ? `<span class="cal-cell__preview">${escapeHtml((mine || theirs).note)}</span>`
      : "";
    cells.push(`
      <button type="button" class="cal-cell${key === todayKey ? " cal-cell--today" : ""}${
      notes.length ? " cal-cell--has" : ""
    }${dates.length ? " cal-cell--event" : ""}" data-day="${key}">
        <span class="cal-cell__num">${d}</span>
        <span class="cal-cell__dots">${dots}</span>
        ${preview}
      </button>`);
  }
  els.calGrid.innerHTML = cells.join("");
}

function openDaySheet(day) {
  state.calDay = day;
  const notes = state.calNotes[day] || [];
  const mine = notes.find((n) => n.is_mine);
  const theirs = notes.find((n) => !n.is_mine);

  const parsed = new Date(`${day}T00:00:00`);
  els.dayTitle.textContent = parsed.toLocaleDateString(undefined, {
    weekday: "long", day: "numeric", month: "long",
  });

  const dates = state.calDates[day] || [];
  els.dayEvents.hidden = dates.length === 0;
  els.dayEvents.innerHTML = dates
    .map(
      (d) => `<span class="day-event">♥ ${escapeHtml(d.label)}${
        d.count ? escapeHtml(d.recurrence === "monthly" ? ` · ${d.count} months` : ` · ${ordinal(d.count)}`) : ""
      }<span class="day-event__hint">edit in Dates</span></span>`
    )
    .join("");

  if (theirs) {
    els.dayTheirs.hidden = false;
    els.dayTheirs.innerHTML = `
      <span class="day-theirs__who"><span class="dot dot--theirs"></span>${escapeHtml(
        theirs.author_name || "Them"
      )}</span>
      <span class="day-theirs__note">${escapeHtml(theirs.note)}</span>`;
  } else {
    els.dayTheirs.hidden = true;
    els.dayTheirs.innerHTML = "";
  }

  els.dayNote.value = mine ? mine.note : "";
  renderMilestonePicker(els.dayMilestones, mine ? mine.milestones : []);
  els.daySave.disabled = false;
  els.daySave.textContent = "Save";
  els.daySheet.hidden = false;
  els.dayNote.focus();
}

function closeDaySheet() {
  els.daySheet.hidden = true;
  state.calDay = null;
}

els.calPrev.addEventListener("click", () => {
  state.calMonth = shiftMonth(state.calMonth, -1);
  loadCalendar();
});
els.calNext.addEventListener("click", () => {
  state.calMonth = shiftMonth(state.calMonth, 1);
  loadCalendar();
});
els.calGrid.addEventListener("click", (event) => {
  const cell = event.target.closest(".cal-cell[data-day]");
  if (cell) openDaySheet(cell.dataset.day);
});
els.daySheet.addEventListener("click", (event) => {
  if (event.target.dataset.closeDay) closeDaySheet();
});

els.dayForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.calDay) return;
  const day = state.calDay;
  els.daySave.disabled = true;
  els.daySave.textContent = "Saving…";
  try {
    await saveCalendarNote(day, els.dayNote.value.trim(), readMilestonePicker(els.dayMilestones));
    tg.HapticFeedback.impactOccurred("light");
    closeDaySheet();
    await loadCalendar();
  } catch (err) {
    els.daySave.disabled = false;
    els.daySave.textContent = "Save";
    setStatus(err.message, "error");
  }
});

// --- Planning ---------------------------------------------------------------

function setSelecting(on) {
  state.selecting = on;
  if (!on) state.selected.clear();
  els.selectBtn.textContent = on ? "Cancel" : "Select";
  els.selectBtn.classList.toggle("btn--primary", on);
  els.selectBtn.classList.toggle("btn--ghost", !on);
  updateFab();
  render();
}

function updateFab() {
  const count = state.selected.size;
  els.selectedCount.textContent = count;
  els.planFab.hidden = !state.selecting || count === 0;
}

function toggleSelected(id) {
  const chosen = !state.selected.has(id);
  if (chosen) state.selected.add(id);
  else state.selected.delete(id);

  // Update just this card. A full re-render on every tap rebuilds the whole
  // list, which flickers and throws away the element the finger is still on.
  const card = els.list.querySelector(`.card[data-id="${id}"]`);
  if (card) {
    card.classList.toggle("card--selected", chosen);
    const check = card.querySelector(".card__check");
    if (check) check.classList.toggle("is-checked", chosen);
  }
  updateFab();
}

function planHtml(plan) {
  const parts = [];
  (plan.warnings || []).forEach((w) => {
    parts.push(`<p class="plan__note plan__note--warn">${escapeHtml(w)}</p>`);
  });
  if (plan.summary) parts.push(`<p class="plan__summary">${escapeHtml(plan.summary)}</p>`);

  // A suggested stop is a real place found nearby, but one they have not
  // vetted. It is marked at every level - badge, accent, and a line saying so -
  // because mistaking it for something they saved is the whole risk.
  parts.push(
    plan.stops
      .map((stop, i) => {
        const suggested = stop.source && stop.source !== "saved";
        return `
      <div class="plan__stop${suggested ? " plan__stop--suggested" : ""}">
        <span class="plan__index${suggested ? " plan__index--suggested" : ""}">${i + 1}</span>
        <div class="plan__detail">
          ${stop.when ? `<span class="plan__when">${escapeHtml(stop.when)}</span>` : ""}
          <span class="plan__title">
            ${escapeHtml(stop.title)}${suggested ? '<span class="plan__badge">suggested</span>' : ""}
          </span>
          ${stop.location ? `<span class="plan__where">${escapeHtml(stop.location)}</span>` : ""}
          ${stop.why ? `<span class="plan__why">${escapeHtml(stop.why)}</span>` : ""}
          ${
            stop.url
              ? `<a class="plan__link" href="${escapeHtml(stop.url)}" target="_blank" rel="noopener noreferrer">Open post</a>`
              : `<span class="plan__origin">Found nearby — not one of your saved links</span>`
          }
        </div>
      </div>`;
      })
      .join("")
  );

  // Spread and exclusions are stated plainly: a hand-picked selection can be
  // spread across the island, and a link that was chosen but unusable would
  // otherwise look like it had been lost.
  if (plan.spread_metres >= 3000) {
    parts.push(
      `<p class="plan__note">These stops are about ${Math.round(plan.spread_metres / 1000)} km apart at their widest — expect real travel between them.</p>`
    );
  }
  const excluded = Object.entries(plan.excluded || {});
  if (excluded.length) {
    parts.push(
      `<p class="plan__note">Not included: ${excluded
        .map(([id, reason]) => `#${escapeHtml(id)} (${escapeHtml(reason)})`)
        .join(", ")}</p>`
    );
  }
  return parts.join("");
}

function openPlanSheet(plan) {
  state.plan = plan;
  els.planWeek.textContent = `Saturday ${plan.week_of}`;
  els.planBody.innerHTML = planHtml(plan);
  els.postPlanBtn.disabled = false;
  els.postPlanBtn.textContent = "Post to group";
  els.planSheet.hidden = false;
}

function closePlanSheet() {
  els.planSheet.hidden = true;
  state.plan = null;
}

async function buildPlan(linkIds) {
  if (state.planning) return;
  state.planning = true;
  const button = linkIds ? els.planFab : els.planAllBtn;
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Planning…";
  setStatus("Building a plan… this takes a few seconds.", "info");
  try {
    const plan = await requestPlan(linkIds);
    setStatus("");
    openPlanSheet(plan);
    if (state.selecting) setSelecting(false);
    tg.HapticFeedback.notificationOccurred("success");
  } catch (err) {
    const excluded = err.detail && err.detail.excluded;
    const extra = excluded
      ? " " + Object.entries(excluded).map(([id, r]) => `#${id}: ${r}`).join(", ")
      : "";
    setStatus(`${err.message}${extra}`, "error");
  } finally {
    state.planning = false;
    button.disabled = false;
    button.textContent = original;
    updateFab();
  }
}

els.planAllBtn.addEventListener("click", () => buildPlan(null));

els.selectBtn.addEventListener("click", () => setSelecting(!state.selecting));
els.planFab.addEventListener("click", () => buildPlan([...state.selected]));
els.planSheet.addEventListener("click", (event) => {
  if (event.target.dataset.closePlan) closePlanSheet();
});

els.postPlanBtn.addEventListener("click", async () => {
  if (!state.plan || !state.plan.id) return;
  els.postPlanBtn.disabled = true;
  els.postPlanBtn.textContent = "Posting…";
  try {
    await sendPlanToGroup(state.plan.id);
    els.postPlanBtn.textContent = "Posted to group";
    tg.HapticFeedback.notificationOccurred("success");
  } catch (err) {
    els.postPlanBtn.disabled = false;
    els.postPlanBtn.textContent = "Post to group";
    setStatus(err.message, "error");
  }
});

els.list.addEventListener("click", async (event) => {
  // Selection mode takes over the whole card, so an accidental tap cannot
  // mark something done while picking places.
  if (state.selecting) {
    const card = event.target.closest(".card");
    if (card) {
      event.preventDefault();
      toggleSelected(Number(card.dataset.id));
    }
    return;
  }

  // The badge on an unplaceable entry opens the fix, with the hint focused:
  // the field is the answer to the thing the badge is complaining about.
  const fixButton = event.target.closest("button[data-fix-location]");
  if (fixButton) {
    const card = fixButton.closest(".card");
    if (card) openEditFor(card.dataset.id, true);
    return;
  }

  // A thumbnail opens the photo rather than doing anything to the card.
  const thumbButton = event.target.closest("button[data-photo-id]");
  if (thumbButton) {
    openPhoto(thumbButton.dataset.photoId);
    return;
  }

  // Tapping a tag on a card filters by it.
  const tagButton = event.target.closest("button[data-tag]");
  if (tagButton) {
    state.tag = state.tag === tagButton.dataset.tag ? null : tagButton.dataset.tag;
    render();
    return;
  }

  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const card = button.closest(".card");
  const link = state.links.find((item) => item.id === Number(card.dataset.id));
  if (!link) return;

  if (button.dataset.action === "done") {
    openSheet(link, "done");
    return;
  }

  if (button.dataset.action === "edit") {
    openSheet(link, "edit");
    return;
  }

  // Un-marking needs no extra input, so it applies immediately.
  button.disabled = true;
  try {
    const updated = await patchLink(link.id, { done: false });
    Object.assign(link, updated);
    tg.HapticFeedback.impactOccurred("light");
    render();
  } catch (err) {
    setStatus(err.message, "error");
  } finally {
    button.disabled = false;
  }
});

els.rating.addEventListener("click", (event) => {
  const button = event.target.closest(".rating__btn");
  if (!button) return;
  const alreadyActive = button.classList.contains("is-active");
  els.rating.querySelectorAll(".rating__btn").forEach((btn) => btn.classList.remove("is-active"));
  // Tapping the active value clears it, so a rating can be left unset.
  if (!alreadyActive) button.classList.add("is-active");
});

els.sheet.addEventListener("click", (event) => {
  if (event.target.dataset.close) closeSheet();
});

els.photoAdd.addEventListener("click", () => els.photoInput.click());
els.photoInput.addEventListener("change", () => addPhotos([...els.photoInput.files]));

els.addManual.addEventListener("click", () => openSheet(null, "create"));

els.entryCollection.addEventListener("change", syncCollectionFields);

// Telegram's own confirm dialog, so it matches the client's theme and sits
// where the user expects. showConfirm is Bot API 6.2; an older client falls
// back to the browser's, which is uglier but equally blocking - the one thing
// that must not happen is a destructive action proceeding unasked because the
// method was missing.
// "3 photos, the rating and the note" - an Oxford-comma-free list, because
// this is prose in a dialog rather than data.
function listWords(items) {
  if (items.length <= 1) return items.join("");
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

function confirmAction(message) {
  return new Promise((resolve) => {
    if (typeof tg.showConfirm === "function") {
      try {
        tg.showConfirm(message, (ok) => resolve(!!ok));
        return;
      } catch (err) {
        console.warn("[planner] showConfirm unavailable, falling back", err);
      }
    }
    resolve(window.confirm(message));
  });
}

// A deleted link's photos are gone from the server, so the cached blob URLs
// for them are pointing at nothing. Revoking releases the bytes the browser is
// still holding; dropping the entries stops a recycled id serving a stale image.
function forgetPhotos(linkId) {
  const prefix = `/links/${linkId}/photos/`;
  [...photoUrls.keys()]
    .filter((path) => path.startsWith(prefix))
    .forEach((path) => {
      Promise.resolve(photoUrls.get(path))
        .then((url) => URL.revokeObjectURL(url))
        .catch(() => {});
      photoUrls.delete(path);
    });
}

els.entryDelete.addEventListener("click", async () => {
  const link = state.pending;
  if (!link) return;

  // Name what goes with it. "Are you sure?" tells someone nothing they did not
  // already know; the photo count is the part that is easy to forget, because
  // an uploaded photo exists nowhere else - not on Telegram, not in the chat.
  const photoCount = (link.photos || []).length;
  const alsoGone = [];
  if (photoCount) alsoGone.push(`${photoCount} photo${photoCount === 1 ? "" : "s"}`);
  if (link.rating) alsoGone.push("the rating");
  if (link.note) alsoGone.push("the note");
  const message =
    `Delete "${displayTitle(link)}"?` +
    (alsoGone.length ? `\n\nThis also deletes ${listWords(alsoGone)}.` : "") +
    "\n\nThis cannot be undone.";

  if (!(await confirmAction(message))) return;

  els.entryDelete.disabled = true;
  els.entryDelete.textContent = "Deleting…";
  try {
    await removeLink(link.id);
    forgetPhotos(link.id);
    state.links = state.links.filter((item) => item.id !== link.id);
    state.selected.delete(link.id);
    closeSheet();
    tg.HapticFeedback.impactOccurred("medium");
    render();
    // After render, not before: rendering an empty tab sets its own status
    // ("Nothing marked done yet."), which would otherwise swallow this.
    setStatus("Entry deleted.", "info");
  } catch (err) {
    els.entryDelete.disabled = false;
    els.entryDelete.textContent = "Delete";
    setStatus(err.message, "error");
  }
});

els.entryCategory.addEventListener("click", (event) => {
  const chip = event.target.closest("button[data-entry-category]");
  if (!chip) return;
  const value = chip.dataset.entryCategory;
  state.entryCategory = state.entryCategory === value ? null : value;
  // The old subcategory belongs to the old category and would be rejected.
  state.entrySubcategory = null;
  renderCategoryPicker();
});

els.entrySubcategory.addEventListener("click", (event) => {
  const chip = event.target.closest("button[data-entry-subcategory]");
  if (!chip) return;
  const value = chip.dataset.entrySubcategory;
  state.entrySubcategory = state.entrySubcategory === value ? null : value;
  renderSubcategoryPicker();
});

// Thumbnails inside the sheet open the viewer; the × removes one.
els.sheetPhotos.addEventListener("click", async (event) => {
  const dropping = event.target.closest("button[data-drop-pending]");
  if (dropping) {
    state.pendingPhotos.splice(Number(dropping.dataset.dropPending), 1);
    renderSheetPhotos();
    return;
  }
  const removing = event.target.closest("button[data-delete-photo]");
  if (removing) {
    const link = state.pending;
    if (!link) return;
    const photoId = Number(removing.dataset.deletePhoto);
    removing.disabled = true;
    try {
      await deletePhoto(link.id, photoId);
      link.photos = (link.photos || []).filter((photo) => photo.id !== photoId);
      renderSheetPhotos();
      render();
    } catch (err) {
      removing.disabled = false;
      setStatus(err.message, "error");
    }
    return;
  }
  const thumb = event.target.closest("button[data-photo-id]");
  if (thumb) openPhoto(thumb.dataset.photoId);
});

els.viewer.addEventListener("click", closePhoto);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !els.viewer.hidden) closePhoto();
});

function entryFieldsFromForm() {
  return {
    title: els.entryTitle.value.trim(),
    location: els.entryLocation.value.trim() || null,
    geocode_hint: els.entryHint.value.trim() || null,
    category: state.entryCategory,
    subcategory: state.entrySubcategory,
    is_collection: els.entryCollection.checked,
    // Split here rather than server-side: the column is comma-separated, and
    // the API rejects a tag containing one.
    tags: els.entryTags.value
      .split(",")
      .map((tag) => tag.trim())
      .filter(Boolean),
  };
}

els.doneForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.saving) return;
  const mode = state.sheetMode;
  const link = state.pending;
  if (!link && mode !== "create") return;

  const rating = selectedRating();
  const note = els.note.value.trim();

  state.saving = true;
  els.saveBtn.disabled = true;
  els.saveBtn.textContent = "Saving…";
  try {
    if (mode === "create") {
      const fields = entryFieldsFromForm();
      if (!fields.title) {
        setStatus("A name is needed — everything else is optional.", "error");
        return;
      }
      const created = await createLink({
        ...fields,
        note: note || null,
        rating,
        done: els.entryDone.checked,
      });
      // Photos could not be uploaded before the row existed; send them now.
      for (const file of state.pendingPhotos) {
        try {
          created.photos = [
            ...(created.photos || []),
            await uploadPhoto(created.id, await downscale(file)),
          ];
        } catch (err) {
          setStatus(`Saved, but a photo did not upload: ${err.message}`, "error");
        }
      }
      state.links = [created, ...state.links];
    } else {
      const changes = { note: note || null };
      if (rating !== null) changes.rating = rating;
      if (mode === "done") {
        changes.done = true;
      } else {
        Object.assign(changes, entryFieldsFromForm(), { done: els.entryDone.checked });
        if (!changes.title) {
          setStatus("A name is needed.", "error");
          return;
        }
      }
      const updated = await patchLink(link.id, changes);
      Object.assign(link, updated);
    }
    tg.HapticFeedback.notificationOccurred("success");
    closeSheet();
    render();
  } catch (err) {
    setStatus(err.message, "error");
  } finally {
    state.saving = false;
    els.saveBtn.disabled = false;
    els.saveBtn.textContent = "Save";
  }
});

// --- Dev banner -----------------------------------------------------------

function openTokenEditor() {
  els.devTokenForm.hidden = false;
  els.devTokenInput.value = tg.initData || "";
  els.devTokenInput.focus();
  els.devTokenInput.select();
}

function applyToken(token) {
  const persisted = storeToken(token);
  tg.initData = token;
  if (token && !persisted) {
    // Loud, because the symptom otherwise looks like "it just forgets".
    setStatus(
      "Token accepted for this page, but the browser refused to save it. " +
        "It will be gone on refresh - check whether site data is blocked for " +
        window.location.origin + ".",
      "error"
    );
  }
  els.devTokenForm.hidden = true;
  updateDevBannerText();
  load();
}

function updateDevBannerText() {
  if (!isDev) return;
  // The origin is spelled out because tokens are stored per origin:
  // http://localhost:8000 and http://127.0.0.1:8000 are different origins and
  // do not share localStorage, which looks exactly like "it did not save".
  const origin = window.location.origin;
  if (!storageAvailable()) {
    els.devBannerText.textContent = `Site data is blocked for ${origin}, so the token cannot persist.`;
    return;
  }
  els.devBannerText.textContent = tg.initData
    ? `Outside Telegram · token saved for ${origin}`
    : `Outside Telegram · no token saved for ${origin}`;
}

function setupDevBanner() {
  if (!isDev) return;
  els.devBanner.hidden = false;
  els.devTokenBtn.addEventListener("click", openTokenEditor);
  els.devTokenForm.addEventListener("submit", (event) => {
    event.preventDefault();
    applyToken(els.devTokenInput.value.trim());
  });
  els.devTokenClear.addEventListener("click", () => applyToken(""));
  updateDevBannerText();

  console.info(
    "[planner] dev mode · origin=%s · token=%s · storage=%s",
    window.location.origin,
    tg.initData ? `present (${tg.initData.length} chars)` : "none",
    storageAvailable() ? "writable" : "BLOCKED"
  );
}

// --- Boot -----------------------------------------------------------------

async function load() {
  setStatus("Loading…", "info");
  try {
    // Both in parallel; a failure to load dates must not hide the links.
    const [links, dates] = await Promise.all([
      fetchLinks(),
      fetchDates().catch((err) => {
        console.warn("[planner] could not load dates", err);
        return [];
      }),
    ]);
    state.links = links;
    state.dates = dates;
    render();
  } catch (err) {
    setStatus(err.message, "error");
    els.list.innerHTML = "";
    // Open the inline editor rather than a blocking prompt, and only when the
    // token is actually missing - a rejected token needs a different message.
    if (err.unauthorised && isDev && !tg.initData) openTokenEditor();
  }
  updateDevBannerText();
}

// --- Safe areas -----------------------------------------------------------
//
// Telegram's header - the Close button and the chevron - can sit *over* the
// web view rather than above it, which is why the countdown banner was landing
// underneath it. There are two insets and they stack:
//
//   safeAreaInset         the device's own: notch, rounded corners, home bar
//   contentSafeAreaInset  Telegram's chrome inside that
//
// Both are read rather than assumed. A fixed margin would be wrong on any
// device whose chrome differs from the one it was measured on, and would rot
// the moment Telegram changes its header.

// Bot API 8.0 introduced both properties, and also introduced the fullscreen
// mode that makes the chrome overlap in the first place. A client too old to
// report them is also too old to overlap, so zero is the correct fallback
// there rather than a guess. This constant covers the remaining case: a client
// that says it is fullscreen but does not report the inset.
const FALLBACK_HEADER_PX = 56;

function insetOf(name) {
  const value = tg[name];
  if (!value || typeof value !== "object") return null;
  const read = (side) => (Number.isFinite(value[side]) ? Math.max(0, value[side]) : 0);
  return { top: read("top"), right: read("right"), bottom: read("bottom"), left: read("left") };
}

function applySafeArea() {
  const root = document.documentElement;
  const device = insetOf("safeAreaInset");
  const content = insetOf("contentSafeAreaInset");

  // Nothing to go on. Leave the CSS alone so the env() fallbacks in the
  // stylesheet - the browser's own device insets - stay in effect.
  if (!device && !content) {
    if (tg.isFullscreen) {
      root.style.setProperty("--safe-top", `calc(env(safe-area-inset-top, 0px) + ${FALLBACK_HEADER_PX}px)`);
    }
    return;
  }

  const zero = { top: 0, right: 0, bottom: 0, left: 0 };
  const d = device || zero;
  let c = content || zero;
  // Fullscreen with no reported chrome inset: the header is over the content
  // and we have no measurement, so fall back rather than render underneath it.
  if (!content && tg.isFullscreen) c = { ...zero, top: FALLBACK_HEADER_PX };

  const sides = { top: d.top + c.top, right: d.right + c.right, bottom: d.bottom + c.bottom, left: d.left + c.left };
  Object.entries(sides).forEach(([side, px]) => {
    // max() with the browser's own value rather than a bare override: a client
    // that reports 0 for a device inset the browser knows about - a home
    // indicator under the web view, say - must not talk us out of it. Telegram's
    // number wins whenever it is the larger one, which is the case that matters.
    root.style.setProperty(
      `--safe-${side}`,
      `max(env(safe-area-inset-${side}, 0px), ${px}px)`
    );
  });
  console.info("[planner] safe area", sides, "expanded=", tg.isExpanded, "fullscreen=", tg.isFullscreen);
}

function watchSafeArea() {
  if (typeof tg.onEvent !== "function") return;
  // viewportChanged fires on expand/collapse, which changes how much chrome
  // there is; the other three are the direct notifications.
  ["safeAreaChanged", "contentSafeAreaChanged", "viewportChanged", "fullscreenChanged"].forEach(
    (event) => {
      try {
        tg.onEvent(event, applySafeArea);
      } catch (err) {
        // An older client rejects an event name it does not know. The others
        // still register, and the initial read has already happened.
        console.warn(`[planner] no ${event} event on this client`, err);
      }
    }
  );
}

function applyTelegramTheme() {
  // Telegram exposes its palette as CSS variables; adopt them when present so
  // the app matches the client's theme.
  const params = tg.themeParams || {};
  const root = document.documentElement;
  const map = {
    bg_color: "--tg-bg",
    text_color: "--tg-text",
    hint_color: "--tg-hint",
    button_color: "--tg-button",
    button_text_color: "--tg-button-text",
    secondary_bg_color: "--tg-secondary-bg",
  };
  Object.entries(map).forEach(([source, variable]) => {
    if (params[source]) root.style.setProperty(variable, params[source]);
  });
  if (tg.colorScheme) root.dataset.theme = tg.colorScheme;
}

buildRatingButtons();
setupDevBanner();
applyTelegramTheme();
tg.ready();
tg.expand();
// After expand(): expanding changes the viewport and therefore the insets, and
// the event handler catches the value the client settles on.
applySafeArea();
watchSafeArea();
load();
