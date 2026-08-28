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

const PLATFORM_LABELS = { tiktok: "TikTok", instagram: "Instagram" };

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
  pending: null, // link awaiting the done sheet
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
const saveCalendarNote = (day, note, milestones) =>
  api(`/calendar/${day}`, {
    method: "PUT",
    body: JSON.stringify({ note, milestones: milestones || [] }),
  });
const requestPlan = (linkIds) =>
  api("/plan", { method: "POST", body: JSON.stringify({ link_ids: linkIds || null }) });
const sendPlanToGroup = (planId) => api(`/plans/${planId}/post`, { method: "POST" });
const patchLink = (id, changes) =>
  api(`/links/${id}`, { method: "PATCH", body: JSON.stringify(changes) });

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

  const meta = [`<span class="badge badge--${escapeHtml(link.platform)}">${platform}</span>`];
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

  return `
    <article class="card${link.done ? " card--done" : ""}${expired ? " card--expired" : ""}${
      state.selecting ? " card--selectable" : ""
    }${selected ? " card--selected" : ""}" data-id="${link.id}">
      ${checkbox}
      <div class="card__body">
        <h2 class="card__title">${title}</h2>
        ${caption ? `<p class="card__caption">${caption}</p>` : ""}
        <div class="card__meta">${meta.join("")}</div>
        ${tagChips ? `<div class="card__tags">${tagChips}</div>` : ""}
        ${doneDetails.length ? `<div class="card__done-details">${doneDetails.join("")}</div>` : ""}
      </div>
      <div class="card__actions">
        <a class="btn btn--ghost btn--sm" href="${escapeHtml(link.url)}" target="_blank" rel="noopener noreferrer">Open</a>
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
  els.calendar.hidden = !onCalendar;
  els.list.hidden = onCalendar;
  document.querySelector(".filters").hidden = onCalendar;
  document.querySelector(".actions").hidden = onCalendar;
  if (onCalendar) {
    setStatus("");
    renderDatesList();
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
  els.subtitle.textContent = `${state.links.length} saved · ${done.length} visited`;

  const byTab = { todo, daytrip: dayTrips, done };
  const tabLinks = byTab[state.tab] || todo;
  renderFilters(tabLinks);

  const visible = tabLinks.filter(matchesFilters);
  els.list.innerHTML = visible.map(cardHtml).join("");

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

function openSheet(link) {
  state.pending = link;
  els.sheetPlace.textContent = displayTitle(link);
  els.note.value = link.note || "";
  els.rating.querySelectorAll(".rating__btn").forEach((btn) => {
    btn.classList.toggle("is-active", Number(btn.dataset.value) === link.rating);
  });
  els.sheet.hidden = false;
}

function closeSheet() {
  els.sheet.hidden = true;
  state.pending = null;
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
    els.tabs.forEach((other) => {
      const active = other === tab;
      other.classList.toggle("is-active", active);
      other.setAttribute("aria-selected", String(active));
    });
    render();
  });
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
  try {
    const notes = await fetchCalendar(monthKey(state.calMonth));
    // Grouped by day so a cell can show both people at once.
    state.calNotes = {};
    notes.forEach((n) => {
      (state.calNotes[n.day] = state.calNotes[n.day] || []).push(n);
    });
    setStatus("");
  } catch (err) {
    setStatus(err.message, "error");
    state.calNotes = {};
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
    const mine = notes.find((n) => n.is_mine);
    const theirs = notes.find((n) => !n.is_mine);
    // A dot per author, plus the first note as a hint of what the day holds.
    const dots =
      (mine ? '<span class="dot dot--mine"></span>' : "") +
      (theirs ? '<span class="dot dot--theirs"></span>' : "");
    const preview = notes.length
      ? `<span class="cal-cell__preview">${escapeHtml((mine || theirs).note)}</span>`
      : "";
    cells.push(`
      <button type="button" class="cal-cell${key === todayKey ? " cal-cell--today" : ""}${
      notes.length ? " cal-cell--has" : ""
    }" data-day="${key}">
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
    openSheet(link);
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

els.doneForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.pending || state.saving) return;

  const link = state.pending;
  const changes = { done: true };
  const rating = selectedRating();
  if (rating !== null) changes.rating = rating;
  const note = els.note.value.trim();
  if (note) changes.note = note;

  state.saving = true;
  els.saveBtn.disabled = true;
  els.saveBtn.textContent = "Saving…";
  try {
    const updated = await patchLink(link.id, changes);
    Object.assign(link, updated);
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
load();
