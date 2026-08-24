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

const state = {
  links: [],
  tab: "todo",
  pending: null, // link awaiting the done sheet
  saving: false,
};

const els = {
  list: document.getElementById("list"),
  status: document.getElementById("status"),
  subtitle: document.getElementById("subtitle"),
  countTodo: document.getElementById("count-todo"),
  countDone: document.getElementById("count-done"),
  tabs: document.querySelectorAll(".tab"),
  sheet: document.getElementById("sheet"),
  sheetPlace: document.getElementById("sheet-place"),
  doneForm: document.getElementById("done-form"),
  rating: document.getElementById("rating"),
  note: document.getElementById("note"),
  saveBtn: document.getElementById("save-btn"),
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
    try {
      const body = await response.json();
      if (body && body.detail) detail = typeof body.detail === "string" ? body.detail : detail;
    } catch (err) {
      /* response had no JSON body; keep the generic message */
    }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

const fetchLinks = () => api("/links");
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
  if (link.location) meta.push(`<span class="meta__item">📍 ${escapeHtml(link.location)}</span>`);
  if (link.added_at) meta.push(`<span class="meta__item">${escapeHtml(formatDate(link.added_at))}</span>`);
  if (expired) meta.push(`<span class="meta__item meta__item--warn">expired</span>`);

  const doneDetails = [];
  if (link.rating) doneDetails.push(`<span class="chip chip--rating">${link.rating}/10</span>`);
  if (link.note) doneDetails.push(`<span class="chip">${escapeHtml(link.note)}</span>`);

  return `
    <article class="card${link.done ? " card--done" : ""}${expired ? " card--expired" : ""}" data-id="${link.id}">
      <div class="card__body">
        <h2 class="card__title">${title}</h2>
        ${caption ? `<p class="card__caption">${caption}</p>` : ""}
        <div class="card__meta">${meta.join("")}</div>
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

function render() {
  const todo = state.links.filter((link) => !link.done);
  const done = state.links.filter((link) => link.done);
  els.countTodo.textContent = todo.length;
  els.countDone.textContent = done.length;
  els.subtitle.textContent = `${state.links.length} saved · ${done.length} visited`;

  const visible = state.tab === "todo" ? todo : done;
  els.list.innerHTML = visible.map(cardHtml).join("");

  if (visible.length === 0) {
    setStatus(
      state.tab === "todo"
        ? "Nothing to visit yet. Paste a TikTok or Instagram link in the group."
        : "Nothing marked done yet.",
      "empty"
    );
  } else {
    setStatus("");
  }
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

els.tabs.forEach((tab) => {
  tab.addEventListener("click", () => {
    state.tab = tab.dataset.tab;
    els.tabs.forEach((other) => {
      const active = other === tab;
      other.classList.toggle("is-active", active);
      other.setAttribute("aria-selected", String(active));
    });
    render();
  });
});

els.list.addEventListener("click", async (event) => {
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
    state.links = await fetchLinks();
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
