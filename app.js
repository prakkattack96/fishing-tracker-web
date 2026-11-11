const SPECIES = [
  "Largemouth Bass", "Smallmouth Bass", "Spotted Bass", "Striped Bass",
  "White Bass", "Hybrid Striped Bass", "Walleye", "Sauger", "Northern Pike",
  "Muskellunge", "Chain Pickerel", "Bluegill", "Redear Sunfish", "Pumpkinseed",
  "Green Sunfish", "Rock Bass", "Black Crappie", "White Crappie",
  "Yellow Perch", "White Perch", "Channel Catfish", "Blue Catfish",
  "Flathead Catfish", "Yellow Bullhead", "Brown Bullhead", "Brook Trout",
  "Brown Trout", "Rainbow Trout", "Lake Trout", "Cutthroat Trout",
  "Kokanee Salmon", "Chinook Salmon", "Coho Salmon", "Lake Whitefish",
  "Common Carp", "Grass Carp", "Freshwater Drum", "Bowfin", "Longnose Gar",
  "Shortnose Gar", "American Shad", "Burbot", "Paddlefish", "White Sucker",
  "Other"
];

const BAITS = [
  "Nightcrawler", "Redworm", "Mealworm", "Waxworm", "Minnow", "Shiner",
  "Leech", "Crayfish", "Cricket", "Grasshopper", "Salmon Eggs", "Dough Bait",
  "Cut Bait", "Inline Spinner", "Spinnerbait", "Underspin", "Spoon", "Jig",
  "Swim Jig", "Bladed Jig / Chatterbait", "Tube Jig", "Ned Rig",
  "Soft Plastic Worm", "Creature Bait", "Soft Plastic Craw",
  "Soft Plastic Grub", "Soft Plastic Swimbait", "Tube Bait", "Jerkbait",
  "Crankbait", "Squarebill Crankbait", "Lipless Crankbait", "Topwater Popper",
  "Walking Bait", "Buzzbait", "Hollow-Body Frog", "Fly - Dry Fly",
  "Fly - Nymph", "Fly - Streamer", "Drop Shot Rig", "Texas Rig",
  "Carolina Rig", "Wacky Rig", "Other"
];

const PHASES = [
  "New Moon", "Waxing Crescent", "First Quarter", "Waxing Gibbous",
  "Full Moon", "Waning Gibbous", "Last Quarter", "Waning Crescent"
];
const MAX_PHOTO_BYTES = 8 * 1024 * 1024;
const DB_NAME = "fishing-tracker-web";
const DB_VERSION = 1;
const STORE = "catches";
const CONFIG = window.FISHING_TRACKER_CONFIG || {};
const state = {
  db: null,
  client: null,
  user: null,
  session: null,
  catches: [],
  photo: null,
  photoUrl: null,
  installPrompt: null,
  syncBusy: false
};

const $ = (selector) => document.querySelector(selector);
const catchForm = $("#catch-form");
const catchMessage = $("#catch-message");

function uuid() {
  return crypto.randomUUID();
}

function openLocalDatabase() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const database = request.result;
      if (!database.objectStoreNames.contains(STORE)) {
        database.createObjectStore(STORE, { keyPath: "id" });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function localTransaction(mode, callback) {
  return new Promise((resolve, reject) => {
    const transaction = state.db.transaction(STORE, mode);
    const store = transaction.objectStore(STORE);
    let result;
    try {
      result = callback(store);
    } catch (error) {
      reject(error);
      return;
    }
    transaction.oncomplete = () => resolve(result);
    transaction.onerror = () => reject(transaction.error);
    transaction.onabort = () => reject(transaction.error || new Error("Local storage transaction failed."));
  });
}

async function localGetAll() {
  return new Promise((resolve, reject) => {
    const request = state.db.transaction(STORE, "readonly").objectStore(STORE).getAll();
    request.onsuccess = () => resolve(request.result || []);
    request.onerror = () => reject(request.error);
  });
}

async function localPut(record) {
  await localTransaction("readwrite", (store) => store.put(record));
}

async function localDelete(id) {
  await localTransaction("readwrite", (store) => store.delete(id));
}

function configIsReady() {
  return typeof CONFIG.supabaseUrl === "string"
    && CONFIG.supabaseUrl.startsWith("https://")
    && typeof CONFIG.supabaseAnonKey === "string"
    && CONFIG.supabaseAnonKey.length > 10
    && !CONFIG.supabaseUrl.includes("YOUR-PROJECT");
}

function apiUrl(path) {
  return `${CONFIG.supabaseUrl.replace(/\/$/, "")}${path}`;
}

function authHeaders(token = state.session?.access_token || CONFIG.supabaseAnonKey) {
  return {
    apikey: CONFIG.supabaseAnonKey,
    Authorization: `Bearer ${token}`,
    "Content-Type": "application/json"
  };
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const text = await response.text();
  let result = null;
  if (text) {
    try {
      result = JSON.parse(text);
    } catch {
      result = text;
    }
  }
  if (!response.ok) {
    const detail = Array.isArray(result?.detail)
      ? result.detail.map((item) => item?.msg).filter(Boolean).join("; ")
      : result?.detail;
    const message = detail || result?.msg || result?.message || result?.error_description || result?.error || result?.details || `Request failed (${response.status}).`;
    throw new Error(typeof message === "string" ? message : `Request failed (${response.status}).`);
  }
  return result;
}

class SupabaseRestClient {
  async signIn(email, password) {
    const data = await fetchJson(apiUrl("/auth/v1/token?grant_type=password"), {
      method: "POST",
      headers: authHeaders(CONFIG.supabaseAnonKey),
      body: JSON.stringify({ email, password })
    });
    await this.setSession(data);
    return data;
  }

  async signUp(email, password) {
    const data = await fetchJson(apiUrl("/auth/v1/signup"), {
      method: "POST",
      headers: authHeaders(CONFIG.supabaseAnonKey),
      body: JSON.stringify({ email, password })
    });
    if (data.access_token) await this.setSession(data);
    return data;
  }

  async setSession(session) {
    state.session = session;
    state.user = session.user;
    const expiresAt = session.expires_at || Math.floor(Date.now() / 1000) + (session.expires_in || 3600);
    const stored = { ...session, expires_at: expiresAt };
    localStorage.setItem("fishing-tracker-session", JSON.stringify(stored));
    if (this.onSession) await this.onSession();
  }

  async restoreSession() {
    const raw = localStorage.getItem("fishing-tracker-session");
    if (!raw) return;
    const saved = JSON.parse(raw);
    if (saved.expires_at && saved.expires_at < Math.floor(Date.now() / 1000) + 60 && saved.refresh_token) {
      try {
        const refreshed = await fetchJson(apiUrl("/auth/v1/token?grant_type=refresh_token"), {
          method: "POST",
          headers: authHeaders(CONFIG.supabaseAnonKey),
          body: JSON.stringify({ refresh_token: saved.refresh_token })
        });
        await this.setSession(refreshed);
        return;
      } catch {
        state.session = saved;
        state.user = saved.user;
        if (this.onSession) await this.onSession();
        return;
      }
    }
    state.session = saved;
    state.user = saved.user;
    if (this.onSession) await this.onSession();
  }

  async signOut() {
    if (state.session?.access_token && navigator.onLine) {
      try {
        await fetch(apiUrl("/auth/v1/logout"), {
          method: "POST",
          headers: authHeaders()
        });
      } catch {
        // Clear this device's session even if Supabase cannot be reached.
      }
    }
    state.session = null;
    state.user = null;
    localStorage.removeItem("fishing-tracker-session");
    await this.onSession?.();
  }

  async request(path, options = {}) {
    if (!state.session?.access_token) throw new Error("Sign in to sync your catches.");
    if (state.session.expires_at && state.session.expires_at < Math.floor(Date.now() / 1000) + 60) {
      if (!navigator.onLine || !state.session.refresh_token) {
        throw new Error("Your session expired. Reconnect and sign in to sync.");
      }
      const refreshed = await fetchJson(apiUrl("/auth/v1/token?grant_type=refresh_token"), {
        method: "POST",
        headers: authHeaders(CONFIG.supabaseAnonKey),
        body: JSON.stringify({ refresh_token: state.session.refresh_token })
      });
      await this.setSession(refreshed);
    }
    return fetchJson(apiUrl(path), {
      ...options,
      headers: {
        ...authHeaders(),
        ...(options.headers || {})
      }
    });
  }

  async uploadPhoto(record) {
    if (!record.photo_blob) return record;
    const ext = ({ "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp" })[record.photo_type];
    if (!ext) throw new Error("This photo type is not supported.");
    const path = `${state.user.id}/${record.id}.${ext}`;
    const response = await fetch(apiUrl(`/storage/v1/object/catch-photos/${path}`), {
      method: "POST",
      headers: {
        apikey: CONFIG.supabaseAnonKey,
        Authorization: `Bearer ${state.session.access_token}`,
        "Content-Type": record.photo_type,
        "x-upsert": "true"
      },
      body: record.photo_blob
    });
    if (!response.ok) throw new Error(`Photo upload failed (${response.status}).`);
    return { ...record, photo_path: path, photo_blob: null };
  }

  async signedPhotoUrl(path) {
    if (!path || !state.session?.access_token) return null;
    const encodedPath = path.split("/").map(encodeURIComponent).join("/");
    const response = await this.request(`/storage/v1/object/sign/catch-photos/${encodedPath}`, {
      method: "POST",
      body: JSON.stringify({ expiresIn: 3600 })
    });
    const signed = response.signedURL || response.signedUrl;
    return signed?.startsWith("http")
      ? signed
      : signed
        ? apiUrl(`/storage/v1${signed}`)
        : null;
  }

  async listCatches() {
    const pageSize = 1000;
    const all = [];
    for (let offset = 0; ; offset += pageSize) {
      const page = await this.request(
        `/rest/v1/catches?select=*&user_id=eq.${encodeURIComponent(state.user.id)}&order=updated_at.asc&limit=${pageSize}&offset=${offset}`
      );
      all.push(...page);
      if (page.length < pageSize) return all;
    }
  }

  async upsertCatches(records) {
    if (!records.length) return;
    await this.request("/rest/v1/catches?on_conflict=id", {
      method: "POST",
      headers: { Prefer: "resolution=merge-duplicates,return=minimal" },
      body: JSON.stringify(records.map((record) => ({
        id: record.id,
        user_id: state.user.id,
        caught_at: record.caught_at,
        species: record.species,
        bait: record.bait,
        notes: record.notes || "",
        photo_path: record.photo_path || null,
        updated_at: record.updated_at,
        deleted_at: record.deleted_at || null
      })))
    });
  }
}

async function updateSessionUi() {
  $("#account-panel").hidden = Boolean(state.user);
  $("#sign-out-button").hidden = !state.user;
  if (state.user) {
    $("#sync-detail").textContent = `Signed in as ${state.user.email || "angler"}`;
    $("#auth-message").textContent = "";
    $("#connection-status").textContent = navigator.onLine ? "Syncing…" : "Offline";
    await claimLocalCatchesIfRequested();
    await loadForCurrentUser();
  } else {
    $("#sync-detail").textContent = "";
    state.catches = (await localGetAll()).filter((record) => record.user_id === null);
    await renderCatches();
  }
  updateConnectionStatus();
}

async function claimLocalCatchesIfRequested() {
  const all = await localGetAll();
  const guestRecords = all.filter((record) => record.user_id === null);
  if (!guestRecords.length || !navigator.onLine) return;
  const shouldImport = confirm(
    `There ${guestRecords.length === 1 ? "is" : "are"} ${guestRecords.length} catch${guestRecords.length === 1 ? "" : "es"} saved on this device before sign-in. Add ${guestRecords.length === 1 ? "it" : "them"} to ${state.user.email || "your"} shared account?`
  );
  if (!shouldImport) return;
  for (const record of guestRecords) {
    await localPut({
      ...record,
      user_id: state.user.id,
      updated_at: new Date().toISOString(),
      dirty: true
    });
  }
}

async function loadForCurrentUser() {
  const all = await localGetAll();
  const own = all.filter((record) => record.user_id === state.user.id);
  state.catches = own;
  renderCatches();
  renderInsights();
  if (navigator.onLine) await syncNow();
}

function setStatus(message, mode = "") {
  const element = $("#connection-status");
  element.textContent = message;
  element.classList.toggle("online", mode === "online");
  element.classList.toggle("offline", mode === "offline");
}

function updateConnectionStatus() {
  if (!navigator.onLine) {
    setStatus("Offline · saved here", "offline");
    return;
  }
  if (!configIsReady()) {
    setStatus("Setup needed", "offline");
    return;
  }
  if (!state.user) {
    setStatus("Not signed in", "offline");
    return;
  }
  setStatus(state.syncBusy ? "Syncing…" : "Synced", "online");
}

function renderCatches() {
  const active = state.catches
    .filter((record) => !record.deleted_at)
    .sort((left, right) => right.caught_at.localeCompare(left.caught_at));
  $("#catch-count").textContent = String(active.length);
  $("#catch-list").replaceChildren();
  $("#empty-state").hidden = active.length > 0;
  const baitOptions = [...new Set([...BAITS, ...active.map((record) => record.bait)])]
    .sort((a, b) => a.localeCompare(b));
  fillDatalist($("#species-options"), [...new Set([...SPECIES, ...active.map((record) => record.species)])].sort((a, b) => a.localeCompare(b)));
  fillDatalist($("#bait-options"), baitOptions);
  const selectedBait = $("#insights-bait").value;
  $("#insights-bait").replaceChildren(new Option("All lures", ""));
  for (const bait of baitOptions) $("#insights-bait").add(new Option(bait, bait));
  $("#insights-bait").value = baitOptions.includes(selectedBait) ? selectedBait : "";

  for (const record of active) {
    const card = document.createElement("article");
    card.className = "catch-card panel";
    if (record.photo_url) {
      const image = document.createElement("img");
      image.className = "catch-photo";
      image.src = record.photo_url;
      image.alt = `${record.species} catch`;
      image.loading = "lazy";
      card.append(image);
    } else {
      const placeholder = document.createElement("div");
      placeholder.className = "catch-photo catch-photo-placeholder";
      placeholder.setAttribute("aria-hidden", "true");
      placeholder.textContent = "✧";
      card.append(placeholder);
    }
    const details = document.createElement("div");
    details.className = "catch-details";
    const title = document.createElement("h3");
    title.textContent = record.species;
    const date = document.createElement("p");
    date.className = "catch-meta";
    date.textContent = formatDateTime(record.caught_at);
    const bait = document.createElement("p");
    bait.textContent = `On ${record.bait}`;
    details.append(title, date, bait);
    if (record.notes) {
      const notes = document.createElement("p");
      notes.textContent = record.notes;
      details.append(notes);
    }
    const actions = document.createElement("div");
    actions.className = "catch-actions";
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "button button-outline";
    edit.textContent = "Edit";
    edit.addEventListener("click", () => editCatch(record));
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "button button-danger";
    remove.textContent = "Delete";
    remove.addEventListener("click", () => deleteCatch(record));
    actions.append(edit, remove);
    card.append(details, actions);
    $("#catch-list").append(card);
  }
  renderInsights();
}

function fillDatalist(list, values) {
  list.replaceChildren(...values.map((value) => {
    const option = document.createElement("option");
    option.value = value;
    return option;
  }));
}

function formatDateTime(value) {
  const date = new Date(value);
  return new Intl.DateTimeFormat(undefined, {
    year: "numeric", month: "short", day: "numeric",
    hour: "numeric", minute: "2-digit"
  }).format(date);
}

function moonPhase(value) {
  const date = new Date(value);
  const julianDay = date.getTime() / 86400000 + 2440587.5;
  const phase = ((julianDay - 2451550.2597) / 29.530588853 % 1 + 1) % 1;
  return PHASES[Math.floor(phase * 8 + 0.5) % 8];
}

function renderBars(container, counts, emptyMessage, details = null) {
  container.replaceChildren();
  const rows = [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "chart-empty";
    empty.textContent = emptyMessage;
    container.append(empty);
    return;
  }
  const largest = Math.max(...rows.map((row) => row[1]));
  for (const [label, count] of rows) {
    const row = document.createElement("div");
    row.className = "bar-row";
    const info = document.createElement("div");
    info.className = "bar-info";
    const name = document.createElement("span");
    name.className = "bar-label";
    name.title = label;
    name.textContent = label;
    info.append(name);
    const breakdown = details?.get(label);
    if (breakdown?.size) {
      const lureList = document.createElement("span");
      lureList.className = "lure-breakdown";
      lureList.setAttribute("aria-label", "Caught on");
      const lures = [...breakdown.entries()]
        .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
      for (const [bait, baitCount] of lures) {
        const lure = document.createElement("span");
        lure.className = "lure-tag";
        lure.textContent = `${bait} ×${baitCount}`;
        lureList.append(lure);
      }
      info.append(lureList);
    }
    const track = document.createElement("div");
    track.className = "bar-track";
    track.setAttribute("aria-hidden", "true");
    const fill = document.createElement("div");
    fill.className = `bar-fill${count === largest ? " top" : ""}`;
    fill.style.width = `${Math.max(3, count / largest * 100)}%`;
    track.append(fill);
    const value = document.createElement("span");
    value.className = "bar-value";
    value.setAttribute("aria-label", `${count} ${count === 1 ? "catch" : "catches"}`);
    value.textContent = String(count);
    row.setAttribute("aria-label", `${label}: ${count} ${count === 1 ? "catch" : "catches"}`);
    row.append(info, track, value);
    container.append(row);
  }
}

function renderInsights() {
  const lure = $("#insights-bait").value;
  const records = state.catches.filter((record) =>
    !record.deleted_at && (!lure || record.bait.toLocaleLowerCase() === lure.toLocaleLowerCase())
  );
  const speciesCounts = new Map();
  const speciesLures = new Map();
  const phaseCounts = new Map(PHASES.map((phase) => [phase, 0]));
  for (const record of records) {
    speciesCounts.set(record.species, (speciesCounts.get(record.species) || 0) + 1);
    if (!lure) {
      if (!speciesLures.has(record.species)) speciesLures.set(record.species, new Map());
      const lures = speciesLures.get(record.species);
      lures.set(record.bait, (lures.get(record.bait) || 0) + 1);
    }
    const phase = moonPhase(record.caught_at);
    phaseCounts.set(phase, phaseCounts.get(phase) + 1);
  }
  const topSpecies = [...speciesCounts.entries()].sort((a, b) => b[1] - a[1])[0];
  const topPhase = [...phaseCounts.entries()].sort((a, b) => b[1] - a[1])[0];
  $("#insights-summary").textContent = records.length
    ? `${records.length} ${records.length === 1 ? "catch" : "catches"}${lure ? ` on ${lure}` : ""} · Most common: ${topSpecies[0]} (${topSpecies[1]}) · Most catches: ${topPhase[0]} (${topPhase[1]})`
    : "Log a catch to see patterns.";
  renderBars($("#species-chart"), speciesCounts, "No catches for this lure yet.", speciesLures);
  renderBars($("#moon-chart"), records.length ? phaseCounts : new Map(), "Log a catch to compare phases.");
}

function setLocalDateTime(date = new Date()) {
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  $("#catch-date").value = local.toISOString().slice(0, 10);
  const hour = date.getHours();
  const displayHour = hour % 12 || 12;
  const halfHour = date.getMinutes() < 30 ? "00" : "30";
  $("#catch-time").value = `${displayHour}:${halfHour}`;
  $("#catch-ampm").value = hour < 12 ? "AM" : "PM";
}

function localDateTimeToIso(dateValue, timeValue, ampm) {
  const [hourPart, minutePart] = timeValue.split(":").map(Number);
  const hour = hourPart % 12 + (ampm === "PM" ? 12 : 0);
  const date = new Date(`${dateValue}T00:00:00`);
  date.setHours(hour, minutePart, 0, 0);
  return date.toISOString();
}

function nextLocalTimeOptions() {
  const select = $("#catch-time");
  for (let hour = 1; hour <= 12; hour++) {
    for (const minute of [0, 30]) {
      const option = document.createElement("option");
      option.value = `${hour}:${String(minute).padStart(2, "0")}`;
      option.textContent = option.value;
      select.add(option);
    }
  }
  setLocalDateTime();
}

async function preparePhoto(file) {
  state.photo = null;
  if (state.photoUrl) URL.revokeObjectURL(state.photoUrl);
  state.photoUrl = null;
  $("#photo-preview-wrap").hidden = true;
  $("#photo-consent").hidden = true;
  $("#suggestions").replaceChildren();
  $("#ai-consent").checked = false;
  if (!file) return;
  if (!["image/jpeg", "image/png", "image/webp"].includes(file.type)) {
    $("#catch-photo").value = "";
    setMessage(catchMessage, "Please choose a JPEG, PNG, or WebP photo.", true);
    return;
  }
  if (file.size > MAX_PHOTO_BYTES) {
    $("#catch-photo").value = "";
    setMessage(catchMessage, "That photo is over 8 MB. Choose a smaller image.", true);
    return;
  }
  state.photo = file;
  state.photoUrl = URL.createObjectURL(file);
  $("#photo-preview").src = state.photoUrl;
  $("#photo-preview-wrap").hidden = false;
  $("#photo-consent").hidden = false;
}

function setMessage(element, message, error = false) {
  element.textContent = message;
  element.classList.toggle("error", error);
}

function reportElement(tag, className = "", text = "") {
  const element = document.createElement(tag);
  if (className) element.className = className;
  element.textContent = text;
  return element;
}

function renderRecommendationReport(report) {
  const container = $("#recommendation-results");
  container.replaceChildren();
  container.hidden = false;

  const heading = reportElement(
    "h2",
    "",
    `${report.water_body} · ${report.state.name} · ${report.local_date}`
  );
  const intro = reportElement(
    "p",
    "muted",
    `Shore-casting guide for ${report.recommendations.species}. ${report.season[0].toUpperCase()}${report.season.slice(1)} season.`
  );
  const overview = reportElement("section", "panel recommendation-overview");
  overview.append(heading, intro);

  const water = report.water_temperature;
  const waterText = water.available
    ? `${water.temperature_f}°F · ${water.station_name || water.site_id} · ${water.distance_miles} mi from ${water.distance_reference}`
    : water.message;
  const overviewGrid = reportElement("div", "report-facts");
  const facts = [
    ["Water temperature", waterText],
    ["Moon", `${report.moon.phase} · ${report.moon.illumination_percent}% illumination`],
    ["Waterbody location", `${report.location_resolution.place} · located with ${report.location_resolution.provider}`],
    ["Forecast date", `${report.weather.forecast_date} · ${report.weather.local_time_zone}`]
  ];
  for (const [label, value] of facts) {
    const fact = reportElement("article", "report-fact");
    fact.append(reportElement("strong", "", label), reportElement("span", "", value));
    overviewGrid.append(fact);
  }
  overview.append(overviewGrid);
  overview.append(reportElement("p", "muted small report-caveat", water.message));
  if (water.waterbody_site_message) {
    overview.append(reportElement("p", "muted small report-caveat", water.waterbody_site_message));
  }

  const weather = report.weather;
  const forecast = reportElement("section", "panel report-weather");
  forecast.append(reportElement("h3", "", "Local weather forecast"));
  const weatherGrid = reportElement("div", "report-facts");
  for (const [label, period] of [["Daytime", weather.daytime], ["Nighttime", weather.nighttime]]) {
    const description = period
      ? `${period.conditions || "Conditions unavailable"} · ${
          period.temperature_low_f === null || period.temperature_high_f === null
            ? "temperature unavailable"
            : `${period.temperature_low_f}–${period.temperature_high_f}°F`
        } · wind ${period.wind || "unavailable"}`
      : "Forecast unavailable for this period.";
    const item = reportElement("article", "report-fact");
    item.append(reportElement("strong", "", label), reportElement("span", "", description));
    if (period?.maximum_precipitation_chance_percent !== null && period?.maximum_precipitation_chance_percent !== undefined) {
      item.append(reportElement("small", "muted", `Max precipitation chance: ${period.maximum_precipitation_chance_percent}%`));
    }
    weatherGrid.append(item);
  }
  forecast.append(weatherGrid);

  const context = reportElement("section", "panel report-tactics");
  context.append(
    reportElement("h3", "", "Fish behavior and decision factors"),
    reportElement("p", "muted behavior-context", report.behavior_context)
  );
  const factorList = document.createElement("ul");
  for (const factor of report.decision_factors) {
    factorList.append(
      reportElement("li", "", `${factor.factor}: ${factor.use}`)
    );
  }
  context.append(factorList);

  const lureGrid = reportElement("div", "lure-recommendation-grid");
  for (const [periodName, picks] of [["Day picks", report.recommendations.day], ["Night picks", report.recommendations.night]]) {
    const panel = reportElement("section", "panel lure-panel");
    panel.append(reportElement("h3", "", `Top 3 ${periodName.toLowerCase()}`));
    const list = document.createElement("ol");
    list.className = "lure-picks";
    for (const pick of picks) {
      const item = document.createElement("li");
      item.append(reportElement("strong", "", pick.lure), reportElement("p", "muted", pick.why));
      list.append(item);
    }
    panel.append(list);
    lureGrid.append(panel);
  }

  const tactics = reportElement("section", "panel report-tactics");
  tactics.append(reportElement("h3", "", "Shore-casting notes"));
  const tacticsList = document.createElement("ul");
  for (const tip of report.recommendations.shore_tips) {
    tacticsList.append(reportElement("li", "", tip));
  }
  tactics.append(tacticsList);

  const sources = reportElement("section", "panel report-sources");
  sources.append(reportElement("h3", "", "Sources and what to verify"));
  const sourceList = document.createElement("ul");
  for (const source of report.sources) {
    const item = document.createElement("li");
    const link = document.createElement("a");
    link.href = source.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = source.name;
    item.append(link, reportElement("span", "muted", ` — ${source.description}`));
    sourceList.append(item);
  }
  sources.append(sourceList);
  const limitations = document.createElement("ul");
  limitations.className = "report-limitations";
  for (const note of report.data_notes) limitations.append(reportElement("li", "", note));
  sources.append(limitations);

  container.append(overview, forecast, context, lureGrid, tactics, sources);
}

async function submitRecommendation(event) {
  event.preventDefault();
  const form = $("#recommendation-form");
  if (!form.reportValidity()) return;
  const button = $("#recommendation-submit");
  const message = $("#recommendation-message");
  button.disabled = true;
  button.textContent = "Gathering local conditions…";
  setMessage(message, "Locating the waterbody and checking the local forecast and USGS measurements. This may take a few seconds.");
  try {
    const report = await fetchJson(`${(CONFIG.apiBaseUrl || "").replace(/\/$/, "")}/api/recommendations`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        water_body: $("#recommendation-water").value.trim(),
        state: $("#recommendation-state").value,
        target_species: $("#recommendation-species").value
      })
    });
    renderRecommendationReport(report);
    setMessage(message, "Report ready. Conditions and readings may change; verify before heading out.");
  } catch (error) {
    setMessage(message, `Could not build the report: ${error.message}`, true);
  } finally {
    button.disabled = false;
    button.innerHTML = 'Build my report <span aria-hidden="true">→</span>';
  }
}

async function identifyPhoto() {
  if (!state.photo) return setMessage(catchMessage, "Choose a photo first.", true);
  if (!$("#ai-consent").checked) return setMessage(catchMessage, "Check the consent box before sending the photo to AI.", true);
  if (!state.user || !state.session?.access_token) return setMessage(catchMessage, "Sign in before requesting an AI suggestion.", true);
  const button = $("#identify-button");
  button.disabled = true;
  button.textContent = "Looking closely…";
  setMessage(catchMessage, "Sending this photo to the configured AI provider for a suggestion…");
  try {
    const dataUrl = await fileToDataUrl(state.photo);
    const response = await fetch(`${(CONFIG.apiBaseUrl || "").replace(/\/$/, "")}/api/identify`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${state.session.access_token}`
      },
      body: JSON.stringify({
        image_data: dataUrl.split(",")[1],
        content_type: state.photo.type,
        species_options: [...new Set([...SPECIES, ...state.catches.map((record) => record.species)])]
      })
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "Species suggestions are unavailable.");
    renderSuggestions(result);
    setMessage(catchMessage, result.needs_better_photo && !result.suggestions.length
      ? "The AI couldn't confidently identify that fish. Try a clearer side-on photo."
      : "Choose a suggestion to fill in species, or enter it yourself.");
  } catch (error) {
    setMessage(catchMessage, error.message || "Could not identify the photo.", true);
  } finally {
    button.disabled = false;
    button.textContent = "Suggest species with AI";
  }
}

function fileToDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error("Could not read the selected photo."));
    reader.readAsDataURL(file);
  });
}

function renderSuggestions(result) {
  const container = $("#suggestions");
  container.replaceChildren();
  for (const suggestion of result.suggestions || []) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "suggestion";
    const name = document.createElement("strong");
    name.textContent = suggestion.species;
    const reason = document.createElement("span");
    const confidence = Math.round(suggestion.confidence * 100);
    reason.textContent = `${confidence}% model estimate · ${suggestion.reason}`;
    button.append(name, reason);
    button.addEventListener("click", () => {
      $("#catch-species").value = suggestion.species;
      container.querySelectorAll(".suggestion").forEach((item) => item.classList.remove("chosen"));
      button.classList.add("chosen");
    });
    container.append(button);
  }
}

function recordFromForm(existing = {}) {
  const recordId = existing.id || uuid();
  const caughtAt = localDateTimeToIso($("#catch-date").value, $("#catch-time").value, $("#catch-ampm").value);
  return {
    ...existing,
    id: recordId,
    user_id: state.user?.id || null,
    caught_at: caughtAt,
    species: $("#catch-species").value.trim(),
    bait: $("#catch-bait").value.trim(),
    notes: $("#catch-notes").value.trim(),
    photo_blob: state.photo || existing.photo_blob || null,
    photo_type: state.photo?.type || existing.photo_type || null,
    photo_path: state.photo ? null : (existing.photo_path || null),
    updated_at: new Date().toISOString(),
    deleted_at: null,
    dirty: true
  };
}

async function saveCatch(event) {
  event.preventDefault();
  const species = $("#catch-species").value.trim();
  const bait = $("#catch-bait").value.trim();
  if (!species || !bait || !$("#catch-date").value || !$("#catch-time").value) {
    return setMessage(catchMessage, "Choose a date and time, and enter both species and lure / bait.", true);
  }
  const record = recordFromForm();
  try {
    await localPut(record);
    state.catches = [...state.catches.filter((item) => item.id !== record.id), record];
    await renderCatches();
    catchForm.reset();
    setLocalDateTime();
    if (state.photoUrl) URL.revokeObjectURL(state.photoUrl);
    state.photo = null;
    state.photoUrl = null;
    $("#photo-preview-wrap").hidden = true;
    $("#photo-consent").hidden = true;
    $("#suggestions").replaceChildren();
    setMessage(catchMessage, navigator.onLine && state.user
      ? "Catch saved on this device. Syncing to your shared log…"
      : "Catch saved on this device. It will sync when you're signed in and online.");
    if (navigator.onLine && state.user) await syncNow();
  } catch (error) {
    setMessage(catchMessage, `Could not save this catch: ${error.message}`, true);
  }
}

function editCatch(record) {
  const parsed = new Date(record.caught_at);
  setLocalDateTime(parsed);
  $("#catch-species").value = record.species;
  $("#catch-bait").value = record.bait;
  $("#catch-notes").value = record.notes || "";
  if (record.photo_blob) {
    preparePhoto(record.photo_blob);
  } else {
    state.photo = null;
    if (state.photoUrl) URL.revokeObjectURL(state.photoUrl);
    state.photoUrl = record.photo_url || null;
    $("#photo-preview-wrap").hidden = !record.photo_url;
    if (record.photo_url) $("#photo-preview").src = record.photo_url;
    $("#photo-consent").hidden = !record.photo_url;
  }
  catchForm.dataset.editingId = record.id;
  catchForm.querySelector('button[type="submit"]').innerHTML = "Update catch <span aria-hidden=\"true\">→</span>";
  setMessage(catchMessage, "Editing this catch. Save to apply your changes.");
  catchForm.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function deleteCatch(record) {
  if (!confirm(`Delete your ${record.species} catch? It will be removed from synced devices too.`)) return;
  const tombstone = {
    ...record,
    photo_url: undefined,
    photo_blob: null,
    deleted_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    dirty: true
  };
  try {
    await localPut(tombstone);
    state.catches = [...state.catches.filter((item) => item.id !== record.id), tombstone];
    renderCatches();
    if (navigator.onLine && state.user) await syncNow();
  } catch (error) {
    setMessage(catchMessage, `Could not delete catch: ${error.message}`, true);
  }
}

async function syncNow() {
  if (!navigator.onLine || !state.user || !state.session?.access_token || state.syncBusy) return;
  state.syncBusy = true;
  updateConnectionStatus();
  $("#queue-status").textContent = "Syncing catches…";
  try {
    const allLocal = await localGetAll();
    const local = allLocal.filter((record) => record.user_id === state.user.id);
    const remote = await state.client.listCatches();
    const merged = new Map();
    for (const record of remote) merged.set(record.id, { ...record, dirty: false });
    for (const record of local) {
      const cloud = merged.get(record.id);
      const localTime = Date.parse(record.updated_at);
      const cloudTime = cloud ? Date.parse(cloud.updated_at) : Number.NEGATIVE_INFINITY;
      if (!cloud || (record.dirty && localTime >= cloudTime) || localTime > cloudTime) {
        merged.set(record.id, { ...cloud, ...record, dirty: true });
      }
    }

    const toUpload = [];
    for (const record of merged.values()) {
      if (record.dirty) {
        const uploaded = await state.client.uploadPhoto(record);
        toUpload.push(uploaded);
      }
    }
    await state.client.upsertCatches(toUpload);

    for (const record of merged.values()) {
      const syncedRecord = { ...record, dirty: false };
      if (syncedRecord.photo_path && !syncedRecord.deleted_at) {
        try {
          syncedRecord.photo_url = await state.client.signedPhotoUrl(syncedRecord.photo_path);
        } catch {
          syncedRecord.photo_url = null;
        }
      } else {
        syncedRecord.photo_url = null;
      }
      await localPut(syncedRecord);
      merged.set(syncedRecord.id, syncedRecord);
    }
    state.catches = [...merged.values()].filter((record) => record.user_id === state.user.id);
    renderCatches();
    $("#queue-status").textContent = "Everything is synced.";
    setStatus("Synced", "online");
  } catch (error) {
    $("#queue-status").textContent = `Sync paused: ${error.message}`;
    setStatus("Sync needs attention", "offline");
  } finally {
    state.syncBusy = false;
    updateConnectionStatus();
  }
}

function editSelectedRecord(originalId) {
  const record = state.catches.find((item) => item.id === originalId && !item.deleted_at);
  if (!record) return;
  const updated = recordFromForm(record);
  return localPut(updated).then(async () => {
    state.catches = [...state.catches.filter((item) => item.id !== originalId), updated];
    delete catchForm.dataset.editingId;
    catchForm.reset();
    setLocalDateTime();
    catchForm.querySelector('button[type="submit"]').innerHTML = "Save catch <span aria-hidden=\"true\">→</span>";
    state.photo = null;
    if (state.photoUrl) URL.revokeObjectURL(state.photoUrl);
    state.photoUrl = null;
    $("#photo-preview-wrap").hidden = true;
    $("#photo-consent").hidden = true;
    $("#suggestions").replaceChildren();
    await renderCatches();
    setMessage(catchMessage, "Catch updated on this device. Syncing changes…");
    if (navigator.onLine && state.user) await syncNow();
  });
}

async function handleCatchSubmit(event) {
  if (catchForm.dataset.editingId) {
    event.preventDefault();
    try {
      await editSelectedRecord(catchForm.dataset.editingId);
    } catch (error) {
      setMessage(catchMessage, `Could not update this catch: ${error.message}`, true);
    }
    return;
  }
  await saveCatch(event);
}

function initializeTabs() {
  document.querySelectorAll(".tab").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((tab) => {
        const active = tab === button;
        tab.classList.toggle("active", active);
        tab.setAttribute("aria-selected", String(active));
      });
      $("#journal-tab").hidden = button.dataset.tab !== "journal";
      $("#insights-tab").hidden = button.dataset.tab !== "insights";
      $("#recommendations-tab").hidden = button.dataset.tab !== "recommendations";
      if (button.dataset.tab === "insights") renderInsights();
    });
  });
}

async function initialize() {
  nextLocalTimeOptions();
  initializeTabs();
  $("#auth-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!configIsReady() || !state.client) {
      return setMessage($("#auth-message"), "Configure Supabase first (see README).", true);
    }
    const action = event.submitter?.value || "signin";
    const email = $("#auth-email").value.trim();
    const password = $("#auth-password").value;
    setMessage($("#auth-message"), action === "signup" ? "Creating your account…" : "Signing in…");
    try {
      if (action === "signup") {
        const result = await state.client.signUp(email, password);
        if (!result.access_token) {
          setMessage($("#auth-message"), "Account created. Check your email to confirm, then sign in.");
        }
      } else {
        await state.client.signIn(email, password);
      }
    } catch (error) {
      setMessage($("#auth-message"), error.message, true);
    }
  });
  $("#sign-out-button").addEventListener("click", () => state.client?.signOut());
  $("#catch-form").addEventListener("submit", handleCatchSubmit);
  $("#recommendation-form").addEventListener("submit", submitRecommendation);
  $("#catch-photo").addEventListener("change", (event) => preparePhoto(event.target.files?.[0]));
  $("#identify-button").addEventListener("click", identifyPhoto);
  $("#clear-photo").addEventListener("click", () => {
    $("#catch-photo").value = "";
    preparePhoto(null);
  });
  $("#insights-bait").addEventListener("change", renderInsights);
  window.addEventListener("online", syncNow);
  window.addEventListener("offline", updateConnectionStatus);
  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    state.installPrompt = event;
    $("#install-button").hidden = false;
  });
  $("#install-button").addEventListener("click", async () => {
    if (!state.installPrompt) return;
    await state.installPrompt.prompt();
    state.installPrompt = null;
    $("#install-button").hidden = true;
  });

  for (const element of $("#catch-form").elements) element.disabled = true;
  for (const element of $("#auth-form").elements) element.disabled = true;
  state.client = configIsReady() ? new SupabaseRestClient() : null;
  if (state.client) state.client.onSession = updateSessionUi;

  state.db = await openLocalDatabase();
  state.catches = (await localGetAll()).filter((record) => record.user_id === null);
  renderCatches();
  if (state.client) {
    await state.client.restoreSession();
  } else {
    $("#auth-message").textContent = "Add your Supabase URL and publishable key to config.js to enable accounts and shared sync.";
  }
  for (const element of $("#catch-form").elements) element.disabled = false;
  for (const element of $("#auth-form").elements) element.disabled = false;
  updateConnectionStatus();
}

initialize().catch((error) => {
  setStatus("App startup failed", "offline");
  setMessage($("#auth-message"), `Could not start the app: ${error.message}`, true);
});
