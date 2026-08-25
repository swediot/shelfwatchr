/* The sign-in page. One form, four modes.
 *
 * Standalone rather than part of app.js: this page loads before you have an
 * account and shouldn't drag in a reading-list renderer to show two inputs.
 *
 * The modes share fields on purpose. Someone who types their address, finds out
 * they meant "create account", and has to type it again has been made to do
 * work by an implementation detail.
 */

const $ = (id) => document.getElementById(id);
const THEME_KEY = "shelfwatchr.theme";

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

function message(text, kind = "info") {
  const box = document.createElement("div");
  box.className = `notice ${kind}`;
  box.textContent = text;
  $("messages").replaceChildren(box);
}
const clearMessage = () => $("messages").replaceChildren();

async function api(url, body) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || `${resp.status} ${resp.statusText}`);
  return data;
}

/* ------------------------------------------------------------------ modes */

const MODES = {
  signin: {
    title: "Sign in",
    blurb: "",
    submit: "Sign in",
    passwordLabel: "Password",
    autocomplete: "current-password",
    hint: "",
    needsPassword: true,
  },
  signup: {
    title: "Create your account",
    blurb: "We'll email you a link to confirm the address.",
    submit: "Create account",
    passwordLabel: "Choose a password",
    autocomplete: "new-password",
    hint: "At least 10 characters.",
    needsPassword: true,
  },
  forgot: {
    title: "Reset your password",
    blurb: "We'll email you a link to set a new password.",
    submit: "Send the link",
    needsPassword: false,
  },
  reset: {
    title: "Choose a new password",
    blurb: "",
    submit: "Save and sign in",
    passwordLabel: "New password",
    autocomplete: "new-password",
    hint: "At least 10 characters.",
    needsPassword: true,
    needsEmail: false,
  },
};

let mode = "signin";
let resetToken = "";

function setMode(next) {
  mode = next;
  const m = MODES[next];
  clearMessage();
  $("sent-note").hidden = true;

  $("auth-title").textContent = m.title;
  $("auth-blurb").textContent = m.blurb;
  $("auth-blurb").hidden = !m.blurb;
  $("btn-submit").textContent = m.submit;

  $("field-email").hidden = m.needsEmail === false;
  $("email").required = m.needsEmail !== false;
  $("field-password").hidden = !m.needsPassword;
  $("password").required = !!m.needsPassword;
  if (m.needsPassword) {
    $("password-label").textContent = m.passwordLabel;
    $("password").setAttribute("autocomplete", m.autocomplete);
    $("password-hint").textContent = m.hint;
    $("password-hint").hidden = !m.hint;
  }

  // The tabs only describe the two entry points; forgot and reset are places
  // you arrive at, so neither tab should claim to be current.
  const onTabs = next === "signin" || next === "signup";
  $("tabs").hidden = !onTabs && next === "reset";
  for (const btn of $("tabs").querySelectorAll("button")) {
    btn.setAttribute("aria-selected", String(onTabs && btn.dataset.mode === next));
  }
  $("link-forgot").hidden = next !== "signin";
  $("link-back").hidden = next !== "forgot";

  const first = m.needsEmail === false ? $("password") : $("email");
  if (matchMedia("(hover: hover)").matches) first.focus();
}

/* --------------------------------------------------------------- submit */

async function submit(event) {
  event.preventDefault();
  const email = $("email").value.trim();
  const password = $("password").value;
  const btn = $("btn-submit");
  btn.disabled = true;
  clearMessage();

  try {
    if (mode === "signin") {
      await api("/api/auth/login", { email, password });
      // Full navigation, not history.pushState: the app boots from scratch and
      // picks up the account's list on the way in.
      location.assign(next() || "/");
      return;
    }
    if (mode === "signup") {
      const out = await api("/api/auth/register", { email, password });
      told(out.detail || "Check your email for a link to finish setting up.");
      return;
    }
    if (mode === "forgot") {
      const out = await api("/api/auth/forgot", { email });
      told(out.detail || "If that address has an account, a reset link is on its way.");
      return;
    }
    if (mode === "reset") {
      await api("/api/auth/reset", { token: resetToken, password });
      location.assign("/?reset=ok");
      return;
    }
  } catch (err) {
    message(err.message, "warn");
  } finally {
    btn.disabled = false;
  }
}

/** The "we've sent you something" state: the form goes away, because the next
 *  move is in an inbox and leaving the button live invites re-sending. */
function told(text) {
  $("auth-form").hidden = true;
  $("tabs").hidden = true;
  const note = $("sent-note");
  note.hidden = false;
  note.replaceChildren();
  const line = document.createElement("p");
  line.textContent = text;
  note.append(line);

  const again = document.createElement("button");
  again.type = "button";
  again.className = "link";
  again.textContent = "Use a different address";
  again.addEventListener("click", () => {
    $("auth-form").hidden = false;
    note.hidden = true;
    setMode(mode);
  });
  note.append(again);
}

/** Where to go after signing in. Only same-origin paths: a ?next= that accepts
 *  anything is an open redirect, and open redirects are what make phishing
 *  links look legitimate. */
function next() {
  const raw = new URLSearchParams(location.search).get("next") || "";
  return /^\/[^/\\]/.test(raw) ? raw : "";
}

/* ----------------------------------------------------------------- boot */

async function init() {
  initTheme();

  const params = new URLSearchParams(location.search);
  resetToken = params.get("token") || "";

  try {
    const who = await (await fetch("/api/auth/me")).json();
    if (who.user) { location.replace("/"); return; }   // already signed in
    if (!who.accounts) {
      message("Accounts are turned off on this server. The app works without one.", "warn");
      $("auth-form").hidden = true;
      $("tabs").hidden = true;
      return;
    }
    if (!who.signups) {
      $("tabs").querySelector('[data-mode="signup"]').hidden = true;
    }
    if (!who.email_configured) {
      // Not message(): that slot is for what just happened, and setMode() clears
      // it. This is a standing fact about the server.
      const note = $("server-note");
      note.hidden = false;
      note.textContent = "This server has no mail set up, so confirmation and reset "
        + "links are written to its log instead of emailed.";
    }
  } catch (_) { /* the form still works; the server will say if it doesn't */ }

  $("tabs").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-mode]");
    if (btn) setMode(btn.dataset.mode);
  });
  for (const id of ["link-forgot", "link-back"]) {
    $(id).addEventListener("click", (e) => setMode(e.target.dataset.mode));
  }
  $("auth-form").addEventListener("submit", submit);

  $("btn-peek").addEventListener("click", () => {
    const field = $("password");
    const showing = field.type === "text";
    field.type = showing ? "password" : "text";
    $("btn-peek").textContent = showing ? "Show" : "Hide";
    $("btn-peek").setAttribute("aria-pressed", String(!showing));
    $("btn-peek").setAttribute("aria-label", showing ? "Show password" : "Hide password");
  });

  if (resetToken) {
    setMode("reset");
    $("lede").textContent = "Set a new password for your account.";
  } else {
    setMode(params.get("mode") === "signup" ? "signup" : "signin");
  }
}

document.addEventListener("DOMContentLoaded", init);
