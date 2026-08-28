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
    const hint = isDev
      ? "Dev token missing, expired, or invalid. Generate a fresh one with:\n" +
        "python -m scripts.make_init_data"
      : "Telegram sign-in was rejected. Your account may not be on the allowlist.";
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

function renderDateBanner() {
  // Only the nearest date. A list of every anniversary would push the links -
  // the reason the app exists - below the fold.
  const next = state.dates[0];
  if (!next) {
    els.dateBanner.hidden = true;
    els.dateBanner.innerHTML = "";
    return;
  }
  const years = next.years ? ` · ${ordinal(next.years)}` : "";
  els.dateBanner.hidden = false;
  els.dateBanner.className = `date-banner${next.days_until === 0 ? " date-banner--today" : ""}`;
  els.dateBanner.innerHTML = `
    <span class="date-banner__label">${escapeHtml(next.label)}${escapeHtml(years)}</span>
    <span class="date-banner__when">${escapeHtml(describeWhen(next.days_until))}</span>
  `;
}

function render() {
  renderDateBanner();
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
    els.tabs.forEach((other) => {
      const active = other === tab;
      other.classList.toggle("is-active", active);
      other.setAttribute("aria-selected", String(active));
    });
    render();
  });
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
