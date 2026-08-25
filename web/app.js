/* Shelfwatchr frontend. No build step, no dependencies. */

const $ = (id) => document.getElementById(id);
const STORAGE_KEY = "shelfwatchr.state.v1";
const THEME_KEY = "shelfwatchr.theme";
const PAGE_SIZE = 100;   // cards rendered per group before "show all"
const RENDER_MS = 250;   // floor between re-renders while results stream in

const state = {
  scopes: [],
  books: [],
  results: [],
  changes: [],
  file: null,
  slug: null,
  listName: "",
  jobId: null,
  channels: { ntfy: true, webhook: true, email: false },
  running: false,
  generatedAt: null,
  expanded: {},          // group key -> showing everything
  pendingUpload: null,   // books from a new CSV, waiting to update a saved list
  auth: null,            // null until /api/auth/me answers; then {accounts, user, ...}
  claimOffered: false,   // so the offer to attach a list to the account shows once
};

/* ------------------------------------------------------------ storage */

function save() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      scopes: state.scopes, slug: state.slug, jobId: state.jobId,
    }));
  } catch (_) { /* private mode — works, just forgets */ }
}

function restore() {
  try {
    const data = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
    if (Array.isArray(data.scopes)) state.scopes = data.scopes;
    state.slug = data.slug || null;
    state.jobId = data.jobId || null;
  } catch (_) { /* ignore corrupt state */ }
}

/* -------------------------------------------------------------- theme */

/* One button, showing the theme you'd get by pressing it.
 *
 * "System" survives as the starting state — nothing is stored until you press
 * it once — but it stops being a thing you can pick, because a three-way
 * control is a lot of chrome for a decision most people make once. The icon
 * tracks the *resolved* theme, so while still on system it follows the OS.
 */

const SUN = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
  + '<circle cx="12" cy="12" r="4.6"/>'
  + '<g stroke="currentColor" stroke-width="1.9" stroke-linecap="round">'
  + '<path d="M12 2.2v2.4M12 19.4v2.4M2.2 12h2.4M19.4 12h2.4"/>'
  + '<path d="M5.1 5.1l1.7 1.7M17.2 17.2l1.7 1.7M18.9 5.1l-1.7 1.7M6.8 17.2l-1.7 1.7"/>'
  + '</g></svg>';
const MOON = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
  + '<path d="M20.3 14.4A8.6 8.6 0 0 1 9.6 3.7a8.7 8.7 0 1 0 10.7 10.7z"/></svg>';

const systemDark = () => matchMedia("(prefers-color-scheme: dark)");

/** What's actually on screen: an explicit choice, or what the OS says. */
function resolvedTheme(theme) {
  return theme === "system" ? (systemDark().matches ? "dark" : "light") : theme;
}

function applyTheme(theme) {
  const root = document.documentElement;
  if (theme === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);

  // The button advertises where it takes you, not where you are — so it shows
  // the opposite of what's on screen.
  const going = resolvedTheme(theme) === "dark" ? "light" : "dark";
  const btn = $("theme-toggle");
  btn.innerHTML = going === "dark" ? MOON : SUN;
  btn.setAttribute("aria-label", `Switch to ${going} theme`);
  btn.setAttribute("title", `Switch to ${going} theme`);
  btn.dataset.next = going;

  try { localStorage.setItem(THEME_KEY, theme); } catch (_) { /* fine */ }
}

function initTheme() {
  let saved = "system";
  try { saved = localStorage.getItem(THEME_KEY) || "system"; } catch (_) { /* fine */ }
  applyTheme(saved);
  $("theme-toggle").addEventListener("click", () => {
    applyTheme($("theme-toggle").dataset.next);
  });
  // Still on system: follow the OS, and keep the icon honest when it flips at
  // sunset. Once you've pressed the button there's a stored choice and this
  // does nothing.
  systemDark().addEventListener("change", () => {
    let saved = "system";
    try { saved = localStorage.getItem(THEME_KEY) || "system"; } catch (_) { /* fine */ }
    if (saved === "system") applyTheme("system");
  });
}

/* ------------------------------------------------------------ helpers */

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (v !== null && v !== undefined && v !== false) node.setAttribute(k, v);
  }
  for (const child of children.flat()) {
    if (child == null) continue;
    node.append(child.nodeType ? child : document.createTextNode(child));
  }
  return node;
}

/** replaceChildren() renders a literal "null" for a null child — unlike el(),
 *  which skips them. Anything built with a `cond ? node : null` goes through
 *  here first. */
const setChildren = (node, ...children) =>
  node.replaceChildren(...children.flat().filter((c) => c != null));

const message = (text, kind = "info") =>
  $("messages").replaceChildren(el("div", { class: `notice ${kind}`, text }));
const clearMessage = () => $("messages").replaceChildren();

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

async function api(url, options = {}) {
  const resp = await fetch(url, options);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || `${resp.status} ${resp.statusText}`);
  return data;
}

const jsonPost = (body) => ({
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
});

/** dd/mm/yyyy. Fixed, not toLocaleDateString(): the same report opened on a
 *  phone set to US English would otherwise read the day and month the other way
 *  round, and 08/12 is a different date depending on who's holding it. */
function shortDate(value) {
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${d.getFullYear()}`;
}

function humanDuration(seconds) {
  if (seconds < 90) return `${Math.round(seconds)} seconds`;
  const mins = Math.round(seconds / 60);
  if (mins < 90) return `${mins} minutes`;
  const hours = seconds / 3600;
  return `${hours.toFixed(hours < 3 ? 1 : 0)} hours`;
}

function waitLabel(av) {
  if (av.status === "available") return "Available now";
  if (av.status !== "holdable") return "";
  const d = av.wait_days;
  if (typeof d !== "number") return "wait unknown";
  const approx = av.wait_estimated ? "~" : "";
  if (d < 14) return `${approx}${d} days`;
  const weeks = Math.round(d / 7);
  if (weeks < 9) return `${approx}${weeks} weeks`;
  return `${approx}${Math.round(d / 30)} months`;
}

function pillText(av) {
  switch (av.status) {
    case "available":
      if (av.available_copies > 0) {
        return `Available · ${av.available_copies} ${av.available_copies === 1 ? "copy" : "copies"}`;
      }
      return av.lucky_day > 0 ? "Available · Lucky Day" : "Available";
    // Just the wait. The section heading and the yellow already say "hold";
    // repeating it on every row is noise.
    case "holdable": return waitLabel(av);
    case "not_owned": return "Not in catalogue";
    case "unknown": return "Couldn't check";
    default: return "Lookup failed";
  }
}

/* Colour and position carry the meaning visually, and neither reaches a screen
   reader — so the full phrasing lives in the label. */
function pillLabel(av) {
  if (av.status === "holdable") {
    const wait = waitLabel(av);
    return av.wait_estimated
      ? `Hold, estimated ${wait.replace("~", "")} wait`
      : `Hold, ${wait} wait`;
  }
  return pillText(av);
}

/* Two small glyphs: people in the queue, and copies the library owns. They
   carry the "why is the wait that long" detail without a cryptic "5 on 1". */
const ICON_QUEUE =
  '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false">' +
  '<circle cx="5.5" cy="5" r="2.4"/><path d="M1 13.4c0-2.3 2-3.7 4.5-3.7s4.5 1.4 4.5 3.7z"/>' +
  '<circle cx="11.6" cy="5.6" r="1.9" opacity=".55"/>' +
  '<path d="M9.6 9.9c2.4-.5 5.4.6 5.4 3.5h-3.6c0-1.4-.7-2.6-1.8-3.5z" opacity=".55"/></svg>';
const ICON_COPIES =
  '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false">' +
  '<rect x="2" y="2.5" width="7.5" height="11" rx="1"/>' +
  '<rect x="10.5" y="4.2" width="3.6" height="9.3" rx="1" opacity=".55"/></svg>';

function plural(n, one, many) {
  return `${n} ${n === 1 ? one : many}`;
}

function queueLabel(av) {
  const holds = av.holds || 0;
  const copies = av.owned_copies || 0;
  return copies
    ? `${plural(holds, "person is", "people are")} waiting for ${plural(copies, "copy", "copies")}`
    : `${plural(holds, "person is", "people are")} waiting`;
}

/** The queue detail: hidden by default, revealed on hover or by the toggle.
 *
 *  The wait time is the answer; this is only the reason behind it. Two nodes,
 *  because the two ways of revealing it want different shapes — icons when it
 *  sits in the row, words when it floats above it on hover.
 */
function queueDetail(av) {
  if (av.status !== "holdable" || (!av.holds && !av.owned_copies)) return null;
  const holds = av.holds || 0;
  const copies = av.owned_copies || 0;
  const label = queueLabel(av);

  const inline = el("span", { class: "queue", "aria-label": label });
  const queue = el("span", { class: "queue-part" });
  queue.innerHTML = ICON_QUEUE;
  queue.append(String(holds));
  inline.append(queue);
  if (copies) {
    const owned = el("span", { class: "queue-part" });
    owned.innerHTML = ICON_COPIES;
    owned.append(String(copies));
    inline.append(owned);
  }

  // Floats over the row, so revealing it never shifts anything.
  const pop = el("span", { class: "queue-pop", role: "tooltip", text: label });
  return [inline, pop];
}

/* Opening a book in Libby.
 *
 * Two candidate links, because nothing published settles which one reaches the
 * installed app:
 *
 *   library — https://libbyapp.com/library/{key}/everything/page-1/{id}
 *             The web app, pointed at the library offering the best terms.
 *   share   — https://share.libbyapp.com/title/{id}
 *             What Libby's own "share a title" button produces.
 *
 * share.libbyapp.com serves no Universal Links file and no app-link meta tags,
 * so on the evidence it's a landing page rather than an app handoff — but that
 * couldn't be tested on a real device, and libbyapp.com's own configuration was
 * unreadable. So: the library link stays the default, the share link is one tap
 * away, and whichever actually opens the app on your phone can be made the
 * default here.
 */
const LINK_KEY = "shelfwatchr.linkStyle";

function linkStyle() {
  try { return localStorage.getItem(LINK_KEY) || "library"; } catch (_) { return "library"; }
}

function applyLinkStyle(style) {
  try { localStorage.setItem(LINK_KEY, style); } catch (_) { /* fine */ }
  const share = style === "share";
  $("link-style-name").textContent = share ? "a Libby share link" : "the Libby website";
  $("btn-link-style").textContent = share
    ? "Use website links instead" : "Use app links instead";
}

/** Never put a URL in an href without knowing its scheme. */
function safeUrl(url) {
  if (!url) return "";
  try {
    const parsed = new URL(url, location.origin);
    return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.href : "";
  } catch (_) {
    return "";
  }
}

function shareUrl(av) {
  return av.title_id
    ? `https://share.libbyapp.com/title/${encodeURIComponent(av.title_id)}` : "";
}

function openUrl(av) {
  const share = shareUrl(av);
  const library = safeUrl(av.url);
  return linkStyle() === "share" && share ? share : (library || share);
}

/* A tap on a plain same-tab link is the case an OS is most likely to intercept
   and hand to the app; window.open and target=_blank are the cases that most
   often stay in the browser. So on a touch device, don't open a new tab. */
const TOUCH = matchMedia("(hover: none), (pointer: coarse)").matches;

function linkAttrs(av) {
  const href = openUrl(av);
  if (!href) return null;
  return TOUCH ? { href } : { href, target: "_blank", rel: "noopener" };
}

const RANK = { available: 0, holdable: 1, not_owned: 2, unknown: 3, error: 4 };

const FORMAT_KEY = { audiobook: "audiobook-overdrive", ebook: "ebook-overdrive" };
// Every run fetches both, so the toggle never has to go back to OverDrive.
const CHECK_FORMATS = ["audiobook-overdrive", "ebook-overdrive"];

/** Better of two rows for the same library: status first, then the terms. */
function betterRow(a, b) {
  const byStatus = (RANK[a.status] ?? 9) - (RANK[b.status] ?? 9);
  if (byStatus) return byStatus < 0 ? a : b;
  if (a.status === "available") return (a.available_copies || 0) >= (b.available_copies || 0) ? a : b;
  if (a.status === "holdable") return (a.wait_days ?? 1e9) <= (b.wait_days ?? 1e9) ? a : b;
  return a;
}

/** A book's rows as the current format view sees them.
 *
 *  Every run checks both formats, so the toggle is a lens over data already in
 *  hand rather than a reason to go back to OverDrive. Every reader of a book's
 *  results goes through here — the moment one doesn't, the headline pill and the
 *  expanded rows start disagreeing about which format you're looking at.
 *
 *  On "both" the two formats collapse to the better row per library, so an
 *  opened card lists three libraries rather than the same three twice.
 */
function rowsOf(book) {
  const rows = book.results || [];
  if (view.format !== "both") {
    const want = FORMAT_KEY[view.format];
    return rows.filter((r) => r.fmt === want);
  }
  const byScope = new Map();
  for (const r of rows) {
    const cur = byScope.get(r.scope_key);
    byScope.set(r.scope_key, cur ? betterRow(cur, r) : r);
  }
  return [...byScope.values()];
}

const rankOf = (book) => Math.min(...rowsOf(book).map((r) => RANK[r.status] ?? 9), 9);

function bestWait(book) {
  let best = null;
  for (const r of rowsOf(book)) {
    if (r.status === "holdable" && typeof r.wait_days === "number") {
      if (best === null || r.wait_days < best) best = r.wait_days;
    }
  }
  return best;
}

function groupOf(book, shortWaitDays = 21) {
  const rank = rankOf(book);
  if (rank === 0) return "available";
  if (rank === 1) {
    const w = bestWait(book);
    return typeof w === "number" && w <= shortWaitDays ? "short" : "long";
  }
  return "none";   // counted, never listed
}

/* ------------------------------------------------------ library picker */

function renderChips() {
  $("lib-chips").replaceChildren(...state.scopes.map((s) => {
    const remove = el("button", { type: "button", "aria-label": `Remove ${s.name}` }, "×");
    remove.addEventListener("click", () => {
      state.scopes = state.scopes.filter((x) => x.key !== s.key);
      renderChips(); save();
    });
    return el("span", { class: "chip" }, s.name, remove);
  }));
  $("lib-empty").hidden = state.scopes.length > 0;
}

function addScope(scope) {
  if (!state.scopes.some((s) => s.key === scope.key)) {
    state.scopes.push(scope);
    renderChips(); save();
  }
  $("lib-search").value = "";
  $("lib-suggestions").replaceChildren();
}

const searchLibraries = debounce(async (query) => {
  const box = $("lib-suggestions");
  if (query.trim().length < 2) { box.replaceChildren(); return; }
  box.replaceChildren(el("button", { type: "button", disabled: true, text: "Searching…" }));
  try {
    const data = await api(`/api/libraries?q=${encodeURIComponent(query)}`);
    if (!data.items.length) {
      box.replaceChildren(el("button", { type: "button", disabled: true },
        "Nothing matched. Try the slug from your Libby URL."));
      return;
    }
    box.replaceChildren(...data.items.map((item) => {
      const btn = el("button", { type: "button", role: "option" }, item.name,
        el("span", { class: "sub", text: item.region ? `${item.key} · ${item.region}` : item.key }));
      btn.addEventListener("click", () => addScope(item));
      return btn;
    }));
  } catch (err) {
    box.replaceChildren(el("button", { type: "button", disabled: true, text: `Search failed: ${err.message}` }));
  }
}, 320);

/* ---------------------------------------------------------------- csv */

const SHELF_LABEL = {
  "to-read": "To read", read: "Read", "currently-reading": "Currently reading",
  "did-not-finish": "Did not finish",
};

async function uploadCsv(file, statuses = "to-read") {
  if (!file) return;
  state.file = file;
  const body = new FormData();
  body.append("file", file);
  $("csv-report").replaceChildren(el("p", { class: "empty-note", text: `Reading ${file.name}…` }));
  try {
    const data = await api(`/api/import?statuses=${encodeURIComponent(statuses)}`,
      { method: "POST", body });
    if (state.slug && state.books.length) {
      state.pendingUpload = data.books;      // a saved list is open: offer to update it
    } else {
      state.books = data.books;
      state.pendingUpload = null;
    }
    renderCsvReport(data.report, statuses);
  } catch (err) {
    $("shelf-row").hidden = true;
    $("csv-report").replaceChildren(el("div", { class: "notice err", text: err.message }));
  }
}

function renderCsvReport(report, chosen) {
  const shelves = Object.keys(report.statuses_found || {});
  if (!shelves.includes(chosen)) shelves.unshift(chosen);

  $("shelf-select").replaceChildren(...shelves.map((s) => {
    const n = report.statuses_found?.[s];
    const label = SHELF_LABEL[s] || s;
    return el("option", { value: s, selected: s === chosen }, n ? `${label} (${n})` : label);
  }));
  $("shelf-row").hidden = false;

  // Nothing under the row. The shelf menu carries the count, and the button
  // says what it does — everything else that used to live here (which export,
  // how many duplicates, what an update would replace) was read once and then
  // sat there. #csv-report stays in the markup for the one thing worth
  // interrupting for: a file that couldn't be read at all.
  if (state.pendingUpload) {
    // Uploading a fresh export while a saved list is open has one plausible
    // meaning — this is the list now — so the button saves it and checks it.
    $("btn-check").textContent = "Check availability";
    $("btn-check").disabled = state.pendingUpload.length === 0;
  } else {
    // The count lives in the shelf menu next to it; repeating it on the button
    // made the button change width every time you switched shelf.
    $("btn-check").textContent = report.imported ? "Check availability" : "Nothing to check";
    $("btn-check").disabled = report.imported === 0;
  }
  $("csv-report").replaceChildren();
}

async function updateSavedList() {
  if (!state.pendingUpload || !state.slug) return;
  try {
    const out = await api(`/api/profile/${state.slug}/books`, jsonPost({
      name: state.listName,
      scopes: state.scopes,
      formats: CHECK_FORMATS,
      books: state.pendingUpload,
    }));
    state.books = state.pendingUpload;
    state.pendingUpload = null;
    message(`List updated: ${out.total} books (${out.added.length} added, ${out.removed.length} removed). Re-checking…`, "info");
    runLookup(state.books);
  } catch (err) {
    message(`Could not update the list: ${err.message}`, "err");
  }
}

/* ------------------------------------------------------------- lookup */

const progress = {
  startedAt: 0,
  marks: [],        // [timestamp, done] — a short window, for a rate that reflects *now*
  lastDone: 0,
};

function resetProgress(total) {
  progress.startedAt = Date.now();
  progress.marks = [[Date.now(), 0]];
  progress.lastDone = 0;
  $("progress").classList.remove("settled", "floating");
  setProgress(0, total, "Starting…");
}

function humanClock(seconds) {
  if (!isFinite(seconds) || seconds < 0) return "";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const mins = Math.floor(seconds / 60);
  const secs = Math.round(seconds % 60);
  if (mins < 60) return secs ? `${mins}m ${secs}s` : `${mins}m`;
  return `${Math.floor(mins / 60)}h ${mins % 60}m`;
}

/** Books per second over the last ~30s, so a slow patch shows up as a slow patch. */
function recentRate() {
  const now = Date.now();
  progress.marks = progress.marks.filter(([at]) => now - at < 30000);
  if (progress.marks.length < 2) return null;
  const [firstAt, firstDone] = progress.marks[0];
  const [lastAt, lastDone] = progress.marks[progress.marks.length - 1];
  const seconds = (lastAt - firstAt) / 1000;
  if (seconds < 3 || lastDone <= firstDone) return null;
  return (lastDone - firstDone) / seconds;
}

function setProgress(done, total, note = "", stats = null) {
  const box = $("progress");
  box.hidden = false;
  if (done !== progress.lastDone) {
    progress.marks.push([Date.now(), done]);
    progress.lastDone = done;
  }
  // Only start following the page once there's a report worth scrolling past.
  const floating = state.results.length > 6;
  box.classList.toggle("floating", floating);
  // Keep the pinned panel from sitting on top of the last card.
  document.body.classList.toggle("run-active", floating);

  const pct = total ? Math.min(100, Math.round((done / total) * 100)) : 0;
  $("bar-fill").style.width = `${pct}%`;
  $("progress-pct").textContent = `${pct}%`;
  $("bar").setAttribute("aria-valuenow", String(pct));
  $("progress-count").textContent = total
    ? `${done.toLocaleString()} of ${total.toLocaleString()} books`
    : "Working…";

  // Phase: which kind of work is dominating right now.
  let phase = note;
  if (!phase && stats) {
    phase = stats.searches > 0 && done < total
      ? "Looking up titles we haven't seen before — the slow part"
      : "Checking availability";
  }
  $("progress-phase").textContent = phase || "";

  const bits = [];
  const rate = recentRate();
  if (rate && done < total) {
    const eta = (total - done) / rate;
    bits.push(`about ${humanClock(eta)} left`);
  }
  if (progress.startedAt) bits.push(`${humanClock((Date.now() - progress.startedAt) / 1000)} elapsed`);
  if (stats) {
    if (stats.requests) bits.push(`${stats.requests.toLocaleString()} requests`);
    if (stats.cache_hits) bits.push(`${stats.cache_hits.toLocaleString()} from cache`);
    if (stats.rate_per_minute) bits.push(`${Math.round(stats.rate_per_minute)}/min`);
  }
  $("progress-detail").textContent = bits.join(" · ");
  $("btn-cancel").hidden = !state.running;
}

function finishProgress() {
  $("progress").classList.add("settled");
  $("progress").classList.remove("floating");
  document.body.classList.remove("run-active");
  $("progress").hidden = true;
  $("btn-cancel").hidden = true;
}

async function runLookup(books, { refresh = false } = {}) {
  if (state.running) return;
  if (!state.scopes.length) { message("Pick at least one library first.", "warn"); return; }
  if (!books.length) { message("Nothing to look up.", "warn"); return; }

  clearMessage();
  state.results = [];
  state.expanded = {};
  $("results").replaceChildren();
  // Show progress before the request that starts the job, not after it:
  // otherwise the button click appears to do nothing for a moment.
  state.running = true;
  resetProgress(books.length);

  try {
    const job = await api("/api/jobs", jsonPost({
      books,
      scopes: state.scopes.map((s) => s.key),
      formats: CHECK_FORMATS,
      refresh,
      slug: state.slug || "",
    }));
    state.jobId = job.job_id;
    save();
    if (job.estimated_seconds > 120) {
      const cached = job.already_cached
        ? ` ${job.already_cached} are already cached, so the rest take`
        : " That takes";
      const firstRun = job.estimated_seconds > 600
        ? " Most of that is looking each title up for the first time; the next run reuses what it learns and is far quicker."
        : "";
      message(
        `${job.total} books across ${state.scopes.length} libraries.${cached} about `
        + `${humanDuration(job.estimated_seconds)}.${firstRun} `
        + "It runs on the server — you can close this page and come back to it.",
        "info");
    }
    await followJob(job.job_id, 0, job.total);
  } catch (err) {
    message(`Couldn't start the lookup: ${err.message}`, "err");
    state.running = false;
    finishProgress();
  }
}

async function followJob(jobId, after = 0, total = 0) {
  state.running = true;
  if (!progress.startedAt) resetProgress(total || state.results.length);

  try {
    const resp = await fetch(`/api/jobs/${jobId}/stream?after=${after}`);
    if (!resp.ok) throw new Error(`stream said ${resp.status}`);
    await readEvents(resp);
  } catch (err) {
    // The job keeps running server-side; only our view of it broke.
    message(`Lost the live connection (${err.message}). The run continues — reload to rejoin it.`, "warn");
  } finally {
    state.running = false;
    finishProgress();
    $("save-panel").hidden = state.results.length === 0;
    renderResults(true);
  }
}

async function readEvents(resp) {
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let split;
    while ((split = buffer.indexOf("\n\n")) !== -1) {
      const chunk = buffer.slice(0, split);
      buffer = buffer.slice(split + 2);
      if (chunk.startsWith(":")) continue;   // keep-alive

      let event = "message";
      const dataLines = [];
      for (const line of chunk.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      }
      if (!dataLines.length) continue;
      let data;
      try { data = JSON.parse(dataLines.join("\n")); } catch (_) { continue; }

      if (event === "start") {
        setProgress(data.done, data.total, data.done ? "Rejoining a run already in progress" : "", data);
      } else if (event === "heartbeat") {
        setProgress(data.done, data.total, "", data);
      } else if (event === "results") {
        state.results.push(...data.books);
        state.generatedAt = new Date().toISOString();
        setProgress(data.done, data.total, "", data);
        renderResults();
      } else if (event === "stalled") {
        setProgress(data.done, data.total, "Waiting on the library's servers — still going", data);
      } else if (event === "done") {
        setProgress(data.done, data.total, "Finished", data);
        if (data.state === "failed") message(`The run stopped: ${data.error || "unknown error"}`, "err");
        if (data.state === "cancelled") message("Run cancelled. Partial results are below.", "warn");
        return;
      } else if (event === "error") {
        message(data.message, "err");
        return;
      }
    }
  }
}

async function cancelJob() {
  if (!state.jobId) return;
  try { await api(`/api/jobs/${state.jobId}/cancel`, { method: "POST" }); } catch (_) { /* it's stopping anyway */ }
}

/* ------------------------------------------------------------ results */

const GROUPS = [
  ["available", "Available now", "Borrow these today."],
  ["short", "Short wait", ""],
  ["long", "Longer wait", ""],
];

/** The one result that matters: best status, then shortest wait / most copies. */
function bestResult(book) {
  return [...rowsOf(book)].sort((a, b) => {
    const byStatus = (RANK[a.status] ?? 9) - (RANK[b.status] ?? 9);
    if (byStatus) return byStatus;
    if (a.status === "available") return (b.available_copies || 0) - (a.available_copies || 0);
    if (a.status === "holdable") return (a.wait_days ?? 1e9) - (b.wait_days ?? 1e9);
    return 0;
  })[0];
}

function libRow(av) {
  const pill = el("span", {
    class: `pill ${av.status}`,
    "aria-label": pillLabel(av),
  }, pillText(av));
  const name = el("span", { class: "libname", text: av.scope_name, title: av.scope_name });
  // On "both" a row is whichever format won for that library, and without
  // saying which, "Available at Westmount" doesn't tell you what to borrow.
  // In a single-format view every row is that format, so the tag is noise.
  const tag = view.format === "both" && av.fmt
    ? el("span", { class: "fmt-tag", title: av.format },
         av.fmt === "ebook-overdrive" ? "Ebook" : "Audio")
    : null;
  const attrs = linkAttrs(av);
  const inner = attrs
    ? el("a", attrs, name, tag, pill)
    : el("span", { class: "static" }, name, tag, pill);
  const parts = [inner];
  const queue = queueDetail(av);
  if (queue) parts.push(...queue);
  if (av.note && av.note.startsWith("lookup failed")) {
    parts.push(el("span", { class: "flag", text: "last known" }));
  }
  return el("div", { class: "lib" }, parts);
}

const CHEVRON =
  '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false">' +
  '<path d="M4.2 6.1 8 9.9l3.8-3.8" fill="none" stroke="currentColor" ' +
  'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';

let cardSeq = 0;

function bookCard(book) {
  const rows = rowsOf(book);
  const best = bestResult(book);
  const others = rows.filter((r) => r !== best && r.status !== "not_owned");

  // The headline: what you can do about this book, wherever that happens to be.
  const summary = el("span", {
    class: `pill ${best.status}`,
    "aria-label": pillLabel(best),
  }, best.status === "available" ? "Available" : pillText(best));
  const attrs = linkAttrs(best);
  const openBest = attrs
    ? el("a", Object.assign({ class: "best", title: `Open in Libby — ${best.scope_name}` }, attrs),
         summary)
    : el("span", { class: "best" }, summary);

  const card = el("article", { class: "card" });
  const head = el("div", { class: "card-head" },
    el("div", { class: "bk" },
      el("h3", { text: book.title }),
      el("p", { class: "byline", text: book.author || "Unknown author" })),
    openBest);

  // Where else, and on what terms — a tap away, because it's rarely the question.
  if (others.length) {
    const id = `libs-${++cardSeq}`;
    const ordered = [best, ...others].sort((a, b) => (RANK[a.status] ?? 9) - (RANK[b.status] ?? 9));
    const rows = el("div", { class: "libs", id, hidden: true }, ordered.map(libRow));
    // Only worth explaining if there's a queue on show.
    if (ordered.some((av) => av.status === "holdable" && (av.holds || av.owned_copies))) {
      rows.append(legend());
    }
    // The other way in, for whichever one your phone actually honours.
    const alt = altLink(best);
    if (alt) rows.append(alt);
    const toggle = el("button", {
      class: "expand", type: "button", "aria-expanded": "false", "aria-controls": id,
      "aria-label": `Show all ${rows.length} libraries for ${book.title}`,
    });
    toggle.innerHTML = CHEVRON;
    toggle.addEventListener("click", () => {
      const open = toggle.getAttribute("aria-expanded") === "true";
      toggle.setAttribute("aria-expanded", String(!open));
      rows.hidden = open;
      card.classList.toggle("open", !open);
    });
    head.append(toggle);
    card.append(head, rows);
  } else {
    card.append(head);
  }
  return card;
}

function changesBox() {
  if (!state.changes.length) return null;
  return el("div", { class: "notice changes" },
    el("strong", { text: "Since the last check" }),
    el("ul", {}, state.changes.map((c) =>
      el("li", { class: c.good ? "good" : "warn" }, c.sentence))));
}

let renderTimer = null;
let lastRender = 0;

/** Explains the two glyphs, inside the card that's showing them. */
function legend() {
  const make = (svg, text) => {
    const item = el("span", { class: "legend-item" });
    item.innerHTML = svg;
    item.append(text);
    return item;
  };
  return el("p", { class: "legend" },
    make(ICON_QUEUE, "waiting"),
    make(ICON_COPIES, "copies"));
}

/** The link the pill *isn't* using — the fallback if one doesn't reach the app. */
function altLink(av) {
  const share = shareUrl(av);
  const library = safeUrl(av.url);
  if (!share || !library) return null;
  const offerShare = linkStyle() !== "share";
  const href = offerShare ? share : library;
  const label = offerShare ? "Open in the Libby app" : "Open on libbyapp.com";
  const attrs = TOUCH ? { href } : { href, target: "_blank", rel: "noopener" };
  return el("p", { class: "alt-link" }, el("a", attrs, label));
}

/** Throttled: streaming 1,200 results shouldn't mean 1,200 full re-renders. */
function renderResults(immediate = false) {
  const since = Date.now() - lastRender;
  if (!immediate && since < RENDER_MS) {
    if (!renderTimer) renderTimer = setTimeout(() => { renderTimer = null; renderResults(true); }, RENDER_MS - since);
    return;
  }
  if (renderTimer) { clearTimeout(renderTimer); renderTimer = null; }
  lastRender = Date.now();
  paint();
}

/* ---------------------------------------------------------------- account */

/* Accounts are optional throughout. `state.auth` starts as "we don't know yet"
 * and nothing account-shaped renders until /api/auth/me answers, so an instance
 * with accounts turned off never shows a sign-in link at all.
 */

async function loadAccount() {
  try {
    state.auth = await api("/api/auth/me");
  } catch (_) {
    state.auth = { accounts: false, user: null };
  }
  renderAccountBar();
  return state.auth;
}

function renderAccountBar() {
  const auth = state.auth;
  const bar = $("account-bar");
  if (!auth || !auth.accounts) { bar.hidden = true; return; }
  bar.hidden = false;

  if (!auth.user) {
    setChildren(bar,
      el("span", { text: "Your list is saved on this device only." }),
      el("span", { class: "spacer" }),
      el("a", { href: "/signin", text: "Sign in" }),
      auth.signups ? el("a", { href: "/signin?mode=signup", text: "Create account" }) : null);
    return;
  }

  const out = el("button", { type: "button", class: "link" }, "Sign out");
  out.addEventListener("click", signOut);
  bar.replaceChildren(
    el("span", { class: "who" }, el("span", { class: "dot" }), el("span", { text: auth.user.email })),
    el("span", { class: "spacer" }),
    out);
}

async function signOut() {
  try { await api("/api/auth/logout", jsonPost({})); } catch (_) { /* going anyway */ }
  // Drop what points back at the account's list before reloading. Without this
  // the reload rejoins the still-running job, or reopens the saved slug, and
  // the books reappear under a signed-out page. Libraries stay — those are a
  // preference of this device, not a possession of the account.
  state.slug = null;
  state.jobId = null;
  save();
  location.assign("/");
}

const signedIn = () => !!(state.auth && state.auth.user);

/** Save to the account when there is one, to a slug when there isn't.
 *  Same button, same panel — the difference is only where it lands. */
async function saveToAccount(body) {
  const data = await api("/api/me/list", jsonPost(body));
  return data.slug;
}

/** After signing in with a list already open in this browser, offer to keep it.
 *  Never silent: absorbing a list into an account without asking is the kind of
 *  surprise that makes someone distrust the whole thing. */
function offerClaim(slug) {
  const note = $("save-result");
  const yes = el("button", { type: "button", class: "link" }, "Save it to my account");
  yes.addEventListener("click", async () => {
    try {
      await api("/api/me/list/claim", jsonPost({ slug }));
      state.claimOffered = true;
      note.textContent = "Saved to your account.";
      renderWatchPanel(await api(`/api/profile/${slug}`));
    } catch (err) {
      note.textContent = `Could not save it: ${err.message}`;
    }
  });
  note.replaceChildren(
    document.createTextNode("This list isn't attached to your account yet. "), yes);
}

/** Messages the server sends by redirect, after a link in an email. */
function showAuthOutcome() {
  const params = new URLSearchParams(location.search);
  const outcome = params.get("confirm") || (params.get("reset") && "reset");
  if (!outcome) return;
  const note = $("auth-note");
  note.hidden = false;
  if (outcome === "ok") {
    note.className = "notice good";
    note.textContent = "Address confirmed — you're signed in.";
  } else if (outcome === "reset") {
    note.className = "notice good";
    note.textContent = "Password changed. You're signed in.";
  } else {
    note.className = "notice warn";
    note.textContent = "That confirmation link has expired. Sign in to get a new one.";
  }
  // Strip it, so a reload doesn't repeat the message or leave a spent token in
  // the address bar to be shoulder-read.
  params.delete("confirm"); params.delete("reset");
  const rest = params.toString();
  history.replaceState(null, "", rest ? `/?${rest}` : "/");
}

/* ------------------------------------------------------ filter and sort */

const VIEW_KEY = "shelfwatchr.view.v1";
const view = { q: "", sort: "default", library: "", length: "", status: "", wait: "",
               format: "both", seed: 1 };

function loadView() {
  try { Object.assign(view, JSON.parse(localStorage.getItem(VIEW_KEY) || "{}")); }
  catch (_) { /* defaults are fine */ }
}

function saveView() {
  try { localStorage.setItem(VIEW_KEY, JSON.stringify(view)); } catch (_) { /* fine */ }
}

const norm = (s) => (s || "").toLowerCase().normalize("NFKD").replace(/[\u0300-\u036f]/g, "");

/** Surname, for sorting.
 *
 * A comma is not reliably "Last, First" — real StoryGraph exports use it to
 * separate people: "Mohamed Choukri, Paul Bowles (Translator)". So take the
 * text before the first comma and then its last word, which lands on the right
 * name for every shape that turns up:
 *
 *   "N.K. Jemisin"                      -> jemisin
 *   "Jemisin, N.K."                     -> jemisin
 *   "Le Guin, Ursula K."                -> guin
 *   "Mohamed Choukri, Paul Bowles (Tr)" -> choukri
 *
 * Same rule as matching.surname() on the server, deliberately: two different
 * ideas of who the author is would be worse than one imperfect one.
 */
function surname(author) {
  // Each credited name in turn, because a real export can lead with a name in
  // a non-Latin script — "جبران خليل جبران, Anthony Rizcallah Ferris, Kahlil
  // Gibran" — and stopping at the first would sort that book under nothing.
  for (const credit of norm(author).split(/[,;]| and /)) {
    const cleaned = credit
      .replace(/\([^)]*\)/g, " ")       // "(Translator)", "(Illustrator)"
      .replace(/[^a-z ]+/g, " ").trim();
    const parts = cleaned.split(/\s+/).filter(Boolean);
    if (parts.length) return parts[parts.length - 1];
  }
  return "";
}

/** Titles sort by what a shelf would use: no leading article, no punctuation. */
function sortTitle(title) {
  return norm(title).replace(/^(the|a|an)\s+/, "").replace(/[^a-z0-9 ]/g, "").trim();
}

/** Deterministic shuffle, so a re-render doesn't reorder under your thumb. */
function seededShuffle(list, seed) {
  const out = [...list];
  let s = seed || 1;                    // not `state` — that's the app's own
  for (let i = out.length - 1; i > 0; i--) {
    s = (s * 1664525 + 1013904223) % 4294967296;
    const j = s % (i + 1);
    [out[i], out[j]] = [out[j], out[i]];
  }
  return out;
}

function bookLength(book) {
  return typeof book.duration_seconds === "number" ? book.duration_seconds : null;
}

function matchesFilters(book) {
  if (view.q) {
    const hay = `${norm(book.title)} ${norm(book.author)}`;
    if (!norm(view.q).split(/\s+/).filter(Boolean).every((t) => hay.includes(t))) return false;
  }
  if (view.library && !rowsOf(book).some(
    (r) => r.scope_key === view.library && r.status !== "not_owned")) return false;
  if (view.status && !rowsOf(book).some((r) => r.status === view.status)) return false;
  if (view.wait) {
    const w = bestWait(book);
    const onShelf = rankOf(book) === 0;
    if (!onShelf && !(typeof w === "number" && w <= Number(view.wait))) return false;
  }
  if (view.length) {
    const secs = bookLength(book);
    if (secs === null) return false;   // unknown length can't satisfy a length filter
    const [lo, hi] = view.length.split("-").map(Number);
    const hours = secs / 3600;
    if (hours < lo || hours >= hi) return false;
  }
  return true;
}

const SORTS = {
  // Within a section everything shares a status, so the wait is what separates them.
  "default": (a, b) => (bestWait(a) ?? 9999) - (bestWait(b) ?? 9999),
  "added-first": (a, b) => (a.added || "9999").localeCompare(b.added || "9999"),
  "added-last": (a, b) => (b.added || "").localeCompare(a.added || ""),
  "longest": (a, b) => (bookLength(b) ?? -1) - (bookLength(a) ?? -1),
  "shortest": (a, b) => (bookLength(a) ?? Infinity) - (bookLength(b) ?? Infinity),
  "author-az": (a, b) => surname(a.author).localeCompare(surname(b.author)),
  "author-za": (a, b) => surname(b.author).localeCompare(surname(a.author)),
  "title-az": (a, b) => sortTitle(a.title).localeCompare(sortTitle(b.title)),
  "title-za": (a, b) => sortTitle(b.title).localeCompare(sortTitle(a.title)),
};

function sortBooks(list) {
  if (view.sort === "random") return seededShuffle(list, view.seed);
  const fn = SORTS[view.sort] || SORTS.default;
  // Title breaks every tie, so equal-ranked books don't shuffle between renders.
  return [...list].sort((a, b) => fn(a, b) || sortTitle(a.title).localeCompare(sortTitle(b.title)));
}

function activeFilterCount() {
  return ["library", "length", "status", "wait"].filter((k) => view[k]).length;
}

/** The library list comes from the results, not the chips: a saved report you
 *  opened by link has libraries in it before state.scopes is filled in. */
function libraryOptions() {
  // Every row, not rowsOf(): the menu of libraries is a property of the report,
  // and a menu that grew and shrank as you flipped format would be maddening.
  const seen = new Map();
  for (const book of state.results) {
    for (const r of book.results) {
      if (r.status !== "not_owned" && !seen.has(r.scope_key)) {
        seen.set(r.scope_key, r.scope_name || r.scope_key);
      }
    }
  }
  for (const s of state.scopes) if (!seen.has(s.key)) seen.set(s.key, s.name || s.key);
  return [...seen].sort((a, b) => a[1].localeCompare(b[1]));
}

/** Which formats this report actually contains.
 *
 *  A report saved before the app checked ebooks has audiobook rows only, and
 *  switching to Ebooks would empty the page with no explanation. So the segment
 *  for a format that isn't there is disabled, and the note says why.
 */
function formatsPresent() {
  const found = new Set();
  for (const book of state.results) {
    for (const r of book.results) {
      for (const [name, key] of Object.entries(FORMAT_KEY)) if (r.fmt === key) found.add(name);
    }
  }
  return found;
}

function syncFormatToggle() {
  const present = formatsPresent();
  // Nothing tagged at all means an old report from before rows carried a
  // format. Leave every segment usable rather than locking the page to "both".
  const known = present.size > 0;
  if (known && !present.has(view.format) && view.format !== "both") view.format = "both";

  for (const btn of $("format-toggle").querySelectorAll("button")) {
    const name = btn.dataset.format;
    btn.setAttribute("aria-pressed", String(name === view.format));
    btn.disabled = known && name !== "both" && !present.has(name);
  }

  const missing = known ? ["audiobook", "ebook"].filter((f) => !present.has(f)) : [];
  const note = $("format-note");
  note.hidden = missing.length === 0;
  if (missing.length) {
    note.textContent = `This report only covers ${missing[0] === "ebook" ? "audiobooks" : "ebooks"}.`
      + " Re-check to include the other.";
  }
}

function syncControls() {
  const has = state.results.length > 0;
  $("controls").hidden = !has;
  if (!has) return;
  syncFormatToggle();

  const sel = $("f-library");
  const opts = libraryOptions();
  const wanted = ["", ...opts.map(([key]) => key)].join("|");
  if (sel.dataset.keys !== wanted) {            // only rebuild when it changed
    sel.dataset.keys = wanted;
    sel.replaceChildren(
      el("option", { value: "", text: "Any" }),
      ...opts.map(([key, name]) => el("option", { value: key, text: name })));
  }
  if (view.library && !opts.some(([key]) => key === view.library)) view.library = "";

  $("q").value = view.q;
  $("sort").value = view.sort;
  sel.value = view.library;
  $("f-length").value = view.length;
  $("f-status").value = view.status;
  $("f-wait").value = view.wait;

  // Length means audiobook running time; there's nothing to filter on in an
  // ebooks-only view, so the control goes rather than sitting there doing
  // nothing. Clear it on the way out or it'd keep filtering invisibly.
  const lengthUsable = view.format !== "ebook";
  $("f-length").closest(".filter").hidden = !lengthUsable;
  if (!lengthUsable && view.length) { view.length = ""; $("f-length").value = ""; saveView(); }

  const n = activeFilterCount();
  setChildren($("btn-filters"),
    document.createTextNode("Filters"),
    n ? el("span", { class: "badge", text: String(n) }) : null);
}

function renderControlNote(shownCount) {
  const total = state.results.length;
  const filtering = view.q || activeFilterCount();
  const bits = [];
  if (filtering) {
    bits.push(el("span", {
      text: `Showing ${shownCount.toLocaleString()} of ${total.toLocaleString()} books`,
    }));
    const reset = el("button", { type: "button", class: "link" }, "Clear");
    reset.addEventListener("click", clearFilters);
    bits.push(reset);
  }
  if (view.sort === "random") {
    const again = el("button", { type: "button", class: "link" }, "Shuffle again");
    again.addEventListener("click", () => {
      view.seed = (view.seed * 1103515245 + 12345) % 2147483647 || 1;
      saveView(); renderResults(true);
    });
    bits.push(again);
  }
  $("control-note").replaceChildren(...bits);
}

function clearFilters() {
  view.q = ""; view.library = ""; view.length = ""; view.status = ""; view.wait = "";
  saveView();
  syncControls();
  renderResults(true);
}

function bindControls() {
  let debounce;
  $("q").addEventListener("input", (e) => {
    view.q = e.target.value;
    clearTimeout(debounce);
    debounce = setTimeout(() => { saveView(); renderResults(true); }, 180);
  });
  $("sort").addEventListener("change", (e) => {
    view.sort = e.target.value;
    saveView(); renderResults(true);
  });
  for (const [id, key] of [["f-library", "library"], ["f-length", "length"],
                           ["f-status", "status"], ["f-wait", "wait"]]) {
    $(id).addEventListener("change", (e) => {
      view[key] = e.target.value;
      saveView(); syncControls(); renderResults(true);
    });
  }
  $("btn-filters").addEventListener("click", () => {
    const open = $("btn-filters").getAttribute("aria-expanded") === "true";
    $("btn-filters").setAttribute("aria-expanded", String(!open));
    $("filter-panel").hidden = open;
  });
  $("btn-clear-filters").addEventListener("click", clearFilters);
  $("format-toggle").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-format]");
    if (!btn || btn.disabled) return;
    view.format = btn.dataset.format;
    saveView();
    // A section can empty out under a format switch, so re-render the whole
    // report rather than just the toggle.
    renderResults(true);
  });
  if (activeFilterCount()) {                    // a saved filter shouldn't hide
    $("btn-filters").setAttribute("aria-expanded", "true");   // why the list is short
    $("filter-panel").hidden = false;
  }
}

/** Books grouped and ordered for display. One definition, two consumers:
 *  the page and the downloadable report, which must not drift apart. */
function bucketed({ filtered = true } = {}) {
  const buckets = { available: [], short: [], long: [], none: [] };
  const books = filtered ? state.results.filter(matchesFilters) : state.results;
  for (const book of books) buckets[groupOf(book)].push(book);
  // Sections stay: status is still the first question. The chosen order applies
  // inside each one.
  for (const key of ["available", "short", "long"]) buckets[key] = sortBooks(buckets[key]);
  return buckets;
}

function paint() {
  const buckets = bucketed();

  const out = [];
  const box = changesBox();
  if (box) out.push(box);

  for (const [key, title, blurb] of GROUPS) {
    const list = buckets[key];
    if (!list.length) continue;
    const limit = state.expanded[key] ? list.length : PAGE_SIZE;
    const shown = list.slice(0, limit);
    const section = el("section", { class: "group" },
      el("h2", {}, title, el("span", { class: "count", text: String(list.length) })),
      blurb ? el("p", { class: "hint", text: blurb }) : null,
      shown.map(bookCard));
    if (list.length > limit) {
      const more = el("button", { type: "button", class: "show-all" },
        `Show all ${list.length}`);
      more.addEventListener("click", () => { state.expanded[key] = true; renderResults(true); });
      section.append(more);
    }
    out.push(section);
  }

  const shown = buckets.available.length + buckets.short.length + buckets.long.length;
  if (!out.length && !state.running && state.results.length) {
    out.push(el("p", {
      class: "empty-note",
      text: (view.q || activeFilterCount())
        ? "No books match these filters."
        : "None of these are in your libraries' catalogues.",
    }));
  }
  $("results").replaceChildren(...out);

  $("results-head").hidden = state.results.length === 0;
  syncControls();
  if (state.results.length) renderControlNote(shown);
  if (state.results.length) {
    $("results-title").textContent = state.listName
      ? `${state.listName} — ${state.results.length} books`
      : `${state.results.length} books`;
    // Just the date. The time was noise on a report that's refreshed nightly,
    // and the count of books no library carries is a dead end — there's nothing
    // to do about it, which is why the section for them went too.
    const when = shortDate(state.generatedAt);
    $("results-subtitle").textContent = when ? `Checked ${when}` : "";
  }
}

/* --------------------------------------------------- downloadable copy */

function reportHtml() {
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  // The download is a copy of what you're looking at, filters and order
  // included — so it says so, rather than quietly being a shorter list.
  const buckets = bucketed();
  const kept = state.results.filter(matchesFilters).length;
  const filtered = (view.q || activeFilterCount()) ? kept !== state.results.length : false;

  const section = ([key, title]) => {
    const list = buckets[key];
    if (!list.length) return "";
    const cards = list.map((b) => {
      const top = bestResult(b);
      return `
      <article>
        <div class="head"><div><h3>${esc(b.title)}</h3><p class="by">${esc(b.author || "")}</p></div>
        <b class="${esc(top.status)}" aria-label="${esc(pillLabel(top))}">${
          esc(top.status === "available" ? "Available" : pillText(top))}</b></div>
        <ul>${[...rowsOf(b)].sort((x, y) => (RANK[x.status] ?? 9) - (RANK[y.status] ?? 9))
          .filter((av) => av.status !== "not_owned")
          .map((av) => {
            // The saved copy matches what you were looking at: if you'd hidden
            // the queue numbers, they don't turn up in the download either.
            const q = av.status === "holdable" && (av.holds || av.owned_copies)
              ? `<i class="q" title="${esc(queueLabel(av))}">${ICON_QUEUE}${av.holds || 0}` +
                (av.owned_copies ? `${ICON_COPIES}${av.owned_copies}` : "") + "</i>"
              : "";
            const href = safeUrl(av.url);
            return `<li><span>${esc(av.scope_name)}</span>` +
              `<b class="${esc(av.status)}" aria-label="${esc(pillLabel(av))}">${
                esc(pillText(av))}</b>${q}` +
              (href ? ` <a href="${esc(href)}">open</a>` : "") + "</li>";
          }).join("")}</ul>
      </article>`;
    }).join("");
    return `<section><h2>${esc(title)} <span class="n">${list.length}</span></h2>${cards}</section>`;
  };

  return `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Shelfwatchr report</title><style>
:root{--bg:#fbfaf8;--card:#fff;--ink:#1b1a18;--muted:#6b6660;--line:#e6e2dc;
--ok:#1d6b35;--okbg:#e2f2e6;--hold:#8a5a12;--holdbg:#fdf0dc;--non:#7a736b;--nonbg:#f0eeea}
@media(prefers-color-scheme:dark){:root{--bg:#17181a;--card:#202225;--ink:#ece9e4;--muted:#9a948c;
--line:#32353a;--ok:#8bd6a3;--okbg:#1c3a26;--hold:#e5bc6f;--holdbg:#3d2f16;--non:#8e8a84;--nonbg:#2a2c30}}
body{margin:0;padding:24px 16px 60px;background:var(--bg);color:var(--ink);
font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:820px;margin:0 auto}h1{font-size:1.5rem;margin:0 0 4px}
.meta{color:var(--muted);font-size:.88rem;margin:0 0 24px}
h2{font-size:1.05rem;margin:28px 0 10px}.n{font-size:.72rem;background:var(--nonbg);color:var(--non);
padding:2px 8px;border-radius:99px;vertical-align:middle}
article{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:10px}
h3{margin:0;font-size:1rem}.by{margin:2px 0 10px;color:var(--muted);font-size:.84rem}
.head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}
.head b{flex:0 0 auto}
ul{list-style:none;margin:0;padding:0}li{display:flex;align-items:center;gap:8px;padding:3px 0;flex-wrap:wrap}
li span{color:var(--muted);font-size:.85rem;flex:1 1 auto}
i.q{display:inline-flex;align-items:center;gap:8px;font-style:normal;font-size:.72rem;
color:var(--muted);font-variant-numeric:tabular-nums}
i.q svg{width:11px;height:11px;fill:currentColor}
.legend{display:flex;gap:14px;flex-wrap:wrap;margin:0 0 16px;font-size:.76rem;color:var(--muted)}
.legend span{display:inline-flex;align-items:center;gap:5px}
.legend svg{width:11px;height:11px;fill:currentColor}
b{font-size:.78rem;padding:3px 10px;border-radius:99px;background:var(--nonbg);color:var(--non)}
b.available{background:var(--okbg);color:var(--ok)}b.holdable{background:var(--holdbg);color:var(--hold)}
a{color:inherit;font-size:.78rem}
footer{margin-top:36px;padding-top:12px;border-top:1px solid var(--line);color:var(--muted);font-size:.78rem}
</style></head><body><div class="wrap">
<h1>${esc(state.listName || "Shelfwatchr report")}</h1>
<p class="meta">${esc(shortDate(state.generatedAt || Date.now()))} ·
${filtered ? `${kept} of ${state.results.length}` : state.results.length} books ·
${state.scopes.length} libraries · audiobooks${filtered ? " · filtered" : ""}</p>
<p class="legend"><span>${ICON_QUEUE} people waiting</span>
<span>${ICON_COPIES} copies at that library</span></p>
${GROUPS.map(section).join("")}
<footer>From Libby / OverDrive's public catalogue. Wait times marked ~ are estimated
from the hold queue rather than reported by OverDrive.</footer>
</div></body></html>`;
}

function downloadReport() {
  const blob = new Blob([reportHtml()], { type: "text/html" });
  const url = URL.createObjectURL(blob);
  const a = el("a", { href: url, download: `shelfwatchr-${new Date().toISOString().slice(0, 10)}.html` });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

/* -------------------------------------------------------------- watch */

function renderWatchPanel(prof) {
  $("watch-panel").hidden = !state.slug;
  if (!state.slug) return;
  $("watch-enabled").checked = !!prof?.watch_enabled;
  $("watch-frequency").value = prof?.watch_frequency || "weekly";
  $("notify-type").value = prof?.notify_type || "none";
  $("notify-target").value = prof?.notify_target || "";
  const emailOption = $("notify-type").querySelector('option[value="email"]');
  emailOption.disabled = !state.channels.email;
  emailOption.textContent = state.channels.email
    ? "Email digest" : "Email digest (no SMTP on this server)";
  updateNotifyHint();
}

const NOTIFY_HINT = {
  none: "Changes still show at the top of the report when you open it — nothing gets sent.",
  ntfy: "A topic name of your choosing. Install the ntfy app, subscribe to the same topic, and alerts arrive as push notifications. Pick something nobody would guess.",
  webhook: "Any URL that accepts a JSON POST — a Discord or Slack webhook, Home Assistant, your own script.",
  email: "A proper digest: what became available, what's newly holdable, which waits moved, and what's on the shelf right now.",
};

function updateNotifyHint() {
  const type = $("notify-type").value;
  $("notify-hint").textContent = NOTIFY_HINT[type] || "";
  $("notify-target").hidden = type === "none";
  $("notify-target-label").hidden = type === "none";
  $("btn-test-notify").hidden = type === "none";
  $("notify-target").placeholder = {
    ntfy: "shelfwatchr-a7f3k2", webhook: "https://…", email: "you@example.com", none: "",
  }[type] || "";
}

async function saveWatch() {
  try {
    await api(`/api/profile/${state.slug}/watch`, jsonPost({
      enabled: $("watch-enabled").checked,
      frequency: $("watch-frequency").value,
      notify_type: $("notify-type").value,
      notify_target: $("notify-target").value,
    }));
    const freq = $("watch-frequency").value === "weekly" ? "once a week" : "every day";
    const how = { none: "", email: ", and emails you a digest",
                  ntfy: ", and pushes you an alert", webhook: ", and posts to your webhook" }[$("notify-type").value] || "";
    $("watch-result").textContent = $("watch-enabled").checked
      ? `Saved. This list gets re-checked ${freq}${how}.`
      : "Saved. Automatic checks are off.";
  } catch (err) {
    $("watch-result").textContent = `Could not save: ${err.message}`;
  }
}

async function testNotify() {
  $("watch-result").textContent = "Sending a test…";
  try {
    await saveWatch();
    const out = await api(`/api/profile/${state.slug}/test-notify`, { method: "POST" });
    $("watch-result").textContent = out.ok
      ? "Test sent — go and check."
      : `Test failed: ${out.error || "unknown error"}`;
  } catch (err) {
    $("watch-result").textContent = `Test failed: ${err.message}`;
  }
}

/* ------------------------------------------------------------ profile */

async function saveProfile() {
  const body = {
    name: $("profile-name").value.trim(),
    scopes: state.scopes,
    formats: CHECK_FORMATS,
    books: state.books.length
      ? state.books
      : state.results.map((r) => ({ title: r.title, author: r.author, isbn: "" })),
  };
  try {
    // Signed in, the list belongs to the account and reuses its slug; signed
    // out, it's a slug and the link is the only way back to it.
    const slug = signedIn()
      ? await saveToAccount(body)
      : (await api(state.slug ? `/api/profile?slug=${encodeURIComponent(state.slug)}` : "/api/profile",
                   jsonPost(body))).slug;
    const data = { slug };
    state.slug = data.slug;
    state.listName = body.name;
    if (!state.books.length) state.books = body.books;
    save();
    const link = `${location.origin}/?p=${data.slug}`;
    history.replaceState(null, "", `/?p=${data.slug}`);
    $("save-result").replaceChildren(
      document.createTextNode(signedIn()
        ? "Saved to your account. It's also at this link: "
        : "Saved. Your link: "),
      el("a", { href: link, text: link }));
    renderWatchPanel(await api(`/api/profile/${data.slug}`));
  } catch (err) {
    $("save-result").textContent = `Could not save: ${err.message}`;
  }
}

async function loadProfile(slug) {
  try {
    const prof = await api(`/api/profile/${encodeURIComponent(slug)}`);
    state.slug = slug;
    state.scopes = prof.scopes;
    state.books = prof.books;
    state.listName = prof.name || "";
    if (prof.name) $("profile-name").value = prof.name;
    renderChips(); save();
    $("save-panel").hidden = false;
    renderWatchPanel(prof);

    const stored = await api(`/api/profile/${encodeURIComponent(slug)}/report`);
    if (stored.report) {
      state.results = stored.report.results;
      state.changes = stored.changes || [];
      state.generatedAt = stored.report.generated_at;
      renderResults(true);
      const age = (Date.now() - new Date(state.generatedAt).getTime()) / 3600000;
      if (age > 26) message("This report is more than a day old. Re-check to refresh it.", "warn");
    } else if (state.books.length) {
      runLookup(state.books);
    }
  } catch (err) {
    message(`Could not load that list: ${err.message}`, "warn");
  }
}

/** Pick a previous run back up: still going, or finished while the tab was shut.
 *  The results live on the server, so a reload should never mean re-running. */
async function rejoinJob(jobId) {
  try {
    const job = await api(`/api/jobs/${jobId}`);
    if (!["running", "done", "cancelled"].includes(job.state)) return false;

    // A big job's results come back in pages.
    state.results = job.books;
    let next = job.next;
    while (state.results.length < job.done) {
      const page = await api(`/api/jobs/${jobId}?after=${next}&limit=500`);
      if (!page.books.length) break;
      state.results.push(...page.books);
      next = page.next;
    }
    state.results.sort((a, b) => (a.idx ?? 0) - (b.idx ?? 0));
    if (!state.results.length && job.state !== "running") return false;

    state.jobId = jobId;
    $("save-panel").hidden = state.results.length === 0;
    renderResults(true);

    if (job.state === "running") {
      message(`Picking up a run that's still going — ${job.done} of ${job.total} done.`, "info");
      followJob(jobId, next, job.total);
    } else {
      message("Showing your last run. Re-check when you want fresh numbers.", "info");
    }
    return true;
  } catch (_) {
    return false;   // pruned or unknown; nothing to rejoin
  }
}

/* --------------------------------------------------------------- init */

async function init() {
  restore();
  initTheme();
  loadView();
  bindControls();
  renderChips();
  try { state.channels = await api("/api/notify/channels"); } catch (_) { /* defaults fine */ }

  $("lib-search").addEventListener("input", (e) => searchLibraries(e.target.value));
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".picker")) $("lib-suggestions").replaceChildren();
  });

  const dz = $("dropzone");
  dz.addEventListener("click", () => $("csv-input").click());
  dz.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("csv-input").click(); }
  });
  dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("over"); });
  dz.addEventListener("dragleave", () => dz.classList.remove("over"));
  dz.addEventListener("drop", (e) => {
    e.preventDefault(); dz.classList.remove("over");
    uploadCsv(e.dataTransfer.files[0]);
  });
  $("csv-input").addEventListener("change", (e) => uploadCsv(e.target.files[0]));
  $("shelf-select").addEventListener("change", (e) => uploadCsv(state.file, e.target.value));
  $("btn-check").addEventListener("click", () => {
    // With a saved list open and a new export waiting, checking means saving
    // it first — otherwise the report and the saved list quietly disagree.
    if (state.pendingUpload && state.slug) updateSavedList();
    else runLookup(state.pendingUpload || state.books);
  });
  $("btn-cancel").addEventListener("click", cancelJob);

  applyLinkStyle(linkStyle());
  $("btn-link-style").addEventListener("click", () => {
    applyLinkStyle(linkStyle() === "share" ? "library" : "share");
    renderResults(true);
  });

  $("btn-download").addEventListener("click", downloadReport);
  $("btn-recheck").addEventListener("click", () => {
    state.changes = [];
    runLookup(state.books.length
      ? state.books
      : state.results.map((r) => ({ title: r.title, author: r.author, isbn: "" })),
      { refresh: true });
  });

  $("btn-save").addEventListener("click", saveProfile);
  $("btn-save-watch").addEventListener("click", saveWatch);
  $("btn-test-notify").addEventListener("click", testNotify);
  $("notify-type").addEventListener("change", updateNotifyHint);

  await loadAccount();
  showAuthOutcome();

  // Precedence: an explicit ?p= link wins, then a job this browser left running,
  // then the account's own list. A link someone just clicked is the most
  // specific thing they asked for.
  const slug = new URLSearchParams(location.search).get("p");
  if (slug) { await loadProfile(slug); return; }
  if (state.jobId && await rejoinJob(state.jobId)) return;

  if (signedIn()) {
    try {
      const mine = await api("/api/me/list");
      if (mine.list) { await loadProfile(mine.list.slug); return; }
    } catch (_) { /* unconfirmed, or nothing saved yet — the empty page is right */ }
    // Signed in with a list open from before but not attached to the account.
    if (state.slug) { await loadProfile(state.slug); offerClaim(state.slug); }
  }
}

document.addEventListener("DOMContentLoaded", init);
