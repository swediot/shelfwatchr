"""What the library picker does in a browser: the list, and how it is worked.

The ranking itself is covered in tests/test_app.py against the API. This is the
part only a browser can see — that the suggestions can be walked with the
keyboard, that a library already picked says so instead of quietly doing
nothing, and that the sub-line carries the country and the kind of card.

Needs a server on localhost:8080 in mock mode:

  SHELFWATCHR_MOCK=1 uvicorn app.main:app --port 8080
  python tests/picker_check.py
"""

import sys

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
failures = []


def check(condition, message):
    if condition:
        print(f"  ok — {message}")
    else:
        failures.append(message)
        print(f"  FAILED — {message}")


def suggest(page, query, expect):
    """Type a query and wait for its own answer.

    Waiting on ".suggestions button" alone is not enough: the box still holds
    the last query's list, so the wait returns instantly and the assertions
    read stale rows. `expect` is text only the new answer has.
    """
    page.fill("#lib-search", query)
    page.wait_for_selector(f".suggestions button:has-text('{expect}')", timeout=8000)
    page.wait_for_timeout(150)
    return page.locator(".suggestions button")


errors = []
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 430, "height": 900})
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(BASE)

    print("\nWhat comes back")
    rows = suggest(page, "new york", "New York Public Library")
    check(rows.count() > 3, f"a real list, not the first thing that matched ({rows.count()})")
    check("New York Public Library" in rows.first.inner_text(),
          f"the public library first: {rows.first.inner_text()!r}")
    subs = page.locator(".suggestions .sub").all_inner_texts()
    check(any("college" in s or "company" in s for s in subs),
          f"and the ones that need somebody else's card say so: {subs[:3]}")

    print("\nWorking it from the keyboard")
    page.click("#lib-search")
    suggest(page, "queens", "Queens Public Library")
    page.keyboard.press("ArrowDown")
    first = page.evaluate("() => document.activeElement.textContent")
    check("Queens" in first, f"ArrowDown lands on the first suggestion: {first[:30]!r}")
    page.keyboard.press("ArrowDown")
    second = page.evaluate("() => document.activeElement.textContent")
    check(second != first, "and again moves to the second")
    page.keyboard.press("Escape")
    check(page.locator(".suggestions button").count() == 0, "Escape puts the list away")

    print("\nEnter takes the top hit")
    page.fill("#lib-search", "queens")
    page.wait_for_selector(".suggestions button:has-text('Queens Public Library')", timeout=8000)
    page.keyboard.press("Enter")
    page.wait_for_selector("#lib-chips .chip")
    check(page.locator("#lib-chips .chip").count() == 1, "one library picked")
    check("Queens" in page.locator("#lib-chips .chip").first.inner_text(),
          "and it is the one the ranking put first")
    check(page.input_value("#lib-search") == "", "with the box cleared for the next one")

    print("\nOne already picked")
    rows = suggest(page, "queens", "already picked")
    check("already picked" in rows.first.inner_text(),
          f"says so rather than looking clickable: {rows.first.inner_text()!r}")
    check(rows.first.is_disabled(), "and cannot be picked twice")

    browser.close()

print("\nJS errors:", errors or "none")
if failures or errors:
    print(f"\n{len(failures)} failure(s).")
    sys.exit(1)
print("\nPicker checks passed.")
