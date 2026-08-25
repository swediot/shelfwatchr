"""The account journey, in a real browser, end to end.

Sign up, click the link out of the server log, upload a list, sign out, sign in
somewhere else and find the list waiting. Needs a server whose log this can
read, which is what makes the no-SMTP fallback testable:

  SHELFWATCH_MOCK=1 SHELFWATCH_MOCK_SYNTHETIC=1 SHELFWATCH_DB=/tmp/acct.db \
    uvicorn app.main:app --port 8080 > /tmp/srv.log 2>&1 &
  python tests/auth_flow_check.py [base_url] [logfile]
"""

import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
LOG = Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/srv.log")
FIXTURE = str(Path(__file__).parent / "fixtures" / "goodreads.csv")
EMAIL = f"browser{int(time.time())}@example.com"
PASSWORD = "a reasonable passphrase"
failures = []


def check(condition, message):
    if condition:
        print(f"  ok — {message}")
    else:
        failures.append(message)
        print(f"  FAILED — {message}")


def link_from_log(kind: str) -> str:
    """The confirm/reset link the server printed because no SMTP is set."""
    for _ in range(30):
        text = LOG.read_text(errors="replace")
        found = re.findall(rf"printing the {kind} link.*?\n\s+(\S+)", text)
        if found:
            return found[-1]
        time.sleep(0.2)
    raise AssertionError(f"no {kind} link appeared in {LOG}")


def sign_in_form(page, email, password, mode="signin"):
    page.goto(f"{BASE}/signin")
    if mode == "signup":
        page.click('[data-mode="signup"]')
    page.fill("#email", email)
    page.fill("#password", password)
    page.click("#btn-submit")


def upload_and_check(page):
    for term, label in (("queens", "Queens"), ("westmount", "Westmount")):
        page.fill("#lib-search", term)
        page.wait_for_selector(f".suggestions button:has-text('{label}')", timeout=8000)
        page.click(f".suggestions button:has-text('{label}')")
    page.set_input_files("#csv-input", FIXTURE)
    page.wait_for_selector("#shelf-row:not([hidden])")
    page.click("#btn-check")
    page.wait_for_selector("#results article.card", timeout=120000)
    page.wait_for_function("!state.running", timeout=120000)


errors = []
with sync_playwright() as p:
    browser = p.chromium.launch()

    print("\nSigned out, the app still works")
    ctx = browser.new_context(viewport={"width": 430, "height": 900})
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(BASE)
    page.wait_for_selector("#account-bar:not([hidden])", timeout=8000)
    bar = page.locator("#account-bar").inner_text()
    check("Sign in" in bar, f"the bar offers a way in: {bar!r}")
    check("this device only" in bar, "and says where the list currently lives")
    check("null" not in bar, "with no stray null from a conditional child")

    print("\nSigning up")
    sign_in_form(page, EMAIL, "short", mode="signup")
    page.wait_for_timeout(400)
    check("10 characters" in page.locator("#messages").inner_text(),
          "a too-short password is refused with a reason")

    sign_in_form(page, EMAIL, PASSWORD, mode="signup")
    page.wait_for_selector("#sent-note:not([hidden])", timeout=8000)
    check("email" in page.locator("#sent-note").inner_text().lower(),
          "a good one gets 'check your email'")
    check(page.locator("#auth-form").is_hidden(),
          "and the form goes away, so the button can't be leant on")

    print("\nThe confirmation link")
    page.goto(link_from_log("confirm"))
    page.wait_for_selector("#account-bar:not([hidden])", timeout=8000)
    page.wait_for_timeout(400)
    check(EMAIL in page.locator("#account-bar").inner_text(), "lands back on the app, signed in")
    check("confirmed" in page.locator("#auth-note").inner_text().lower(),
          f"saying so, up where you can see it: {page.locator('#auth-note').inner_text()!r}")
    check("confirm=" not in page.url, f"and the spent token is out of the address bar: {page.url}")

    print("\nSaving a list to the account")
    upload_and_check(page)
    page.fill("#profile-name", "Browser list")
    page.click("#btn-save")
    page.wait_for_function("() => document.getElementById('save-result').textContent.length > 0",
                           timeout=15000)
    note = page.locator("#save-result").inner_text()
    check("your account" in note.lower(), f"the confirmation names the account: {note!r}")
    slug = page.evaluate("() => state.slug")
    check(slug, f"and it has a link too ({slug})")

    print("\nSigning out")
    page.click("#account-bar button")
    page.wait_for_selector("#account-bar:not([hidden])", timeout=8000)
    page.wait_for_timeout(500)
    check("Sign in" in page.locator("#account-bar").inner_text(), "the bar goes back to offering a way in")
    check(page.locator("#results article.card").count() == 0,
          "and the list is off the screen — signing out means signed out")
    ctx.close()

    print("\nA different device")
    ctx = browser.new_context(viewport={"width": 430, "height": 900})
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(BASE)
    page.wait_for_timeout(400)
    check(page.locator("#results article.card").count() == 0, "starts empty, as a new browser should")

    sign_in_form(page, EMAIL.upper(), PASSWORD)
    page.wait_for_url(f"{BASE}/", timeout=10000)
    page.wait_for_selector("#results article.card", timeout=120000)
    check(page.evaluate("() => state.slug") == slug,
          "signing in brings the account's list with it, with no link pasted")
    check(page.locator("#lib-chips .chip").count() == 2, "libraries and all")

    print("\nForgotten password")
    page.click("#account-bar button")            # sign out first
    page.wait_for_timeout(600)
    page.goto(f"{BASE}/signin")
    page.click("#link-forgot")
    page.fill("#email", EMAIL)
    page.click("#btn-submit")
    page.wait_for_selector("#sent-note:not([hidden])", timeout=8000)
    check("reset link" in page.locator("#sent-note").inner_text(),
          "asking for a reset says a link is on its way")

    page.goto(link_from_log("reset"))
    page.wait_for_timeout(500)
    check(page.locator("#auth-title").inner_text() == "Choose a new password",
          "the link opens straight into choosing a new one")
    check(page.locator("#field-email").is_hidden(),
          "with no email box — the link already proved the address")

    page.fill("#password", "an entirely new phrase")
    page.click("#btn-submit")
    page.wait_for_url(re.compile(r"/\?reset=ok|/$"), timeout=10000)
    page.wait_for_selector("#account-bar:not([hidden])", timeout=8000)
    page.wait_for_timeout(600)
    check(EMAIL in page.locator("#account-bar").inner_text(), "and signs you in with it")
    check(page.evaluate("() => state.slug") == slug, "with the list still there")
    ctx.close()
    browser.close()

print("\nJS errors:", errors or "none")
if failures or errors:
    print(f"\n{len(failures)} failure(s).")
    sys.exit(1)
print("\nAccount flow passed.")
