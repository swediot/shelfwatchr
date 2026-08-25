"""Browser-level checks for things the Python tests can't see.

The report's wording and layout live in JavaScript, so they need a real browser
to assert against. Needs a server on localhost:8080 in mock mode:

  SHELFWATCHR_MOCK=1 SHELFWATCHR_MOCK_SYNTHETIC=1 uvicorn app.main:app --port 8080
  python tests/ui_smoke.py
"""

import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
FIXTURE = str(Path(__file__).parent / "fixtures" / "goodreads.csv")
failures = []


def waitLabel_py(days):
    """Mirrors waitLabel() in app.js, so the test knows what it should say."""
    if days < 14:
        return f"{days} days"
    weeks = round(days / 7)
    return f"{weeks} weeks" if weeks < 9 else f"{round(days / 30)} months"


def check(condition, message):
    if condition:
        print(f"  ok — {message}")
    else:
        failures.append(message)
        print(f"  FAILED — {message}")


errors = []
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 430, "height": 900})
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(BASE)

    for term, label in (("queens", "Queens"), ("westmount", "Westmount"), ("aubora", "Aubora")):
        page.fill("#lib-search", term)
        page.wait_for_selector(f".suggestions button:has-text('{label}')", timeout=8000)
        page.click(f".suggestions button:has-text('{label}')")
    page.set_input_files("#csv-input", FIXTURE)
    page.wait_for_selector("#shelf-row:not([hidden])")

    print("\nThe shelf row")
    sel = page.locator("#shelf-select").bounding_box()
    btn = page.locator("#btn-check").bounding_box()
    check(abs(sel["y"] - btn["y"]) < 8,
          f"the check button sits beside the shelf menu, not under it "
          f"({sel['y']:.0f} vs {btn['y']:.0f})")
    check(btn["x"] > sel["x"], "and to its right")
    label = page.locator("#btn-check").inner_text()
    check(not re.search(r"\d", label), f"with no book count on it: {label!r}")
    check(page.locator("#csv-report").inner_text().strip() == "",
          "and no export summary underneath")

    page.click("#btn-check")
    # Wait on the rendered result, not on state.running: the flag is false both
    # before a run starts and after it ends, so waiting for "not running" can
    # pass instantly — and waiting for "running" can miss a fast cached run.
    page.wait_for_selector("#results article.card", timeout=120000)
    page.wait_for_function("!state.running", timeout=120000)
    page.wait_for_timeout(400)

    print("\nTop level — one status per book")
    heads = page.locator(".card-head .pill").all_inner_texts()
    check(heads, "every card leads with a single status")
    check(page.locator(".card .libs:visible").count() == 0,
          "and no per-library breakdown until asked for")
    holds = [t for t in heads if t != "Available"]
    check(all(re.fullmatch(r"~?\d+ (days?|weeks?|months?)|wait unknown", t) for t in holds),
          f"waits are just the time: {sorted(set(holds))[:4]}")
    labels = [p.get_attribute("aria-label") for p in page.locator(".card-head .pill").all()]
    check(all(l and (l.startswith("Hold,") or l.startswith("Available")) for l in labels),
          f"with the full phrasing kept for screen readers: {labels[0]!r}")

    # The headline must be the best across libraries, not the first one back.
    # Matched by title, because the DOM is grouped into sections while the data
    # is in file order — zipping the two compares unrelated books.
    verdicts = page.evaluate("""() => {
        const byTitle = Object.fromEntries(state.results.map(b => [b.title, b]));
        return [...document.querySelectorAll('article.card')].map(card => {
            const book = byTitle[card.querySelector('h3').textContent];
            const pill = card.querySelector('.card-head .pill');
            const waits = book.results
                .filter(r => r.status === 'holdable' && typeof r.wait_days === 'number')
                .map(r => r.wait_days);
            return {
                title: book.title,
                anyAvailable: book.results.some(r => r.status === 'available'),
                shortest: waits.length ? Math.min(...waits) : null,
                shownAvailable: pill.classList.contains('available'),
                shownText: pill.textContent,
            };
        });
    }""")
    available = [v for v in verdicts if v["anyAvailable"]]
    check(available and all(v["shownAvailable"] for v in available),
          f"a book available at any library reads as available ({len(available)} of them)")
    waiting = [v for v in verdicts if not v["anyAvailable"] and v["shortest"] is not None]
    check(waiting, "there are wait-only books to check")
    mismatches = [v for v in waiting
                  if v["shownText"] != waitLabel_py(v["shortest"])]
    check(not mismatches, f"and each shows its shortest wait, not just any: {mismatches[:2]}")

    print("\nThe report's own heading")
    subtitle = page.locator("#results-subtitle").inner_text()
    check(re.fullmatch(r"Checked \d{2}/\d{2}/\d{4}", subtitle),
          f"the date is dd/mm/yyyy with no time: {subtitle!r}")
    # Fixed format, not the browser's locale: the same saved report opened on a
    # US-English phone must not read the day and month the other way round.
    day = page.evaluate("() => shortDate('2026-08-03T22:00:00Z')")
    check(day.startswith("03/08") or day.startswith("04/08"),
          f"day first, month second, whatever the browser's locale: {day!r}")
    check("not in any" not in subtitle,
          "and it doesn't count books no library carries — nothing to do about those")

    print("\nExpanding a card")
    card = page.locator("article.card", has=page.locator(".expand")).first
    toggle = card.locator(".expand")
    check(toggle.get_attribute("aria-expanded") == "false", "the chevron starts closed")
    before = card.bounding_box()["height"]
    toggle.click()
    page.wait_for_timeout(250)
    check(toggle.get_attribute("aria-expanded") == "true", "and reports itself open")
    check(card.bounding_box()["height"] > before, "the card grows to show the libraries")
    rows = card.locator(".lib")
    check(rows.count() >= 2, f"listing each library ({rows.count()})")
    names = card.locator(".libname").all_inner_texts()
    check(len(set(names)) == len(names), "each library appearing once")

    queues = card.locator(".queue")
    if queues.count():
        check(queues.first.is_visible(), "the queue numbers come with it")
        check(card.locator(".legend").count() == 1, "explained once, inside the card showing them")

    toggle.click()
    page.wait_for_timeout(250)
    check(card.locator(".libs:visible").count() == 0, "and it closes again")

    print("\nRe-uploading over a saved list")
    page.fill("#profile-name", "Saved")
    page.click("#btn-save")
    page.wait_for_function("() => document.getElementById('save-result').textContent.length > 0",
                           timeout=15000)
    slug = page.evaluate("() => state.slug")
    page.set_input_files("#csv-input", str(Path(FIXTURE).parent / "storygraph.csv"))
    page.wait_for_function("() => state.pendingUpload && state.pendingUpload.length", timeout=15000)
    page.wait_for_timeout(400)
    check(page.locator("#shelf-row button.primary").count() == 1,
          "there's one button to press, not a choice between two")
    check(page.locator("#btn-check").inner_text() == "Check availability",
          f"and it's the same button as always: {page.locator('#btn-check').inner_text()!r}")
    check(page.locator("#csv-report").inner_text().strip() == "",
          "and nothing written underneath it")
    sel = page.locator("#shelf-select").bounding_box()
    btn = page.locator("#btn-check").bounding_box()
    check(abs(sel["height"] - btn["height"]) < 1,
          f"the button is the same height as the menu "
          f"({btn['height']:.0f}px vs {sel['height']:.0f}px)")

    page.click("#btn-check")
    page.wait_for_function("!state.running && state.results.length", timeout=120000)
    page.wait_for_timeout(500)
    check(page.evaluate("() => state.pendingUpload") is None, "pressing it takes the upload")
    check(page.evaluate("() => state.slug") == slug, "keeping the same saved link")
    saved = page.evaluate("""async (s) => (await (await fetch('/api/profile/'+s)).json()).books.length""",
                          slug)
    check(saved == page.evaluate("() => state.books.length"),
          f"and the server's copy matches what's on screen ({saved} books) — "
          "one button can't leave the report and the saved list disagreeing")

    print("\nFooter")
    footer = page.locator("footer").inner_text()
    check("public catalogue" not in footer, "the standing note about the data source is gone")
    check("Book links open" in footer, "while the link-style setting stays — it does something")

    print("\nDownloaded report")
    with page.expect_download() as dl:
        page.click("#btn-download")
    html = Path(dl.value.path()).read_text(encoding="utf-8")
    check(not re.search(r"\d+\s+on\s+\d+", html), "the downloaded copy drops the old shorthand too")
    check("people waiting" in html, "and carries the legend")
    check("<svg" in html, "with the icons inlined so it works offline")
    check("Hold," in html, "labelled for screen readers in the saved copy too")
    check(html.count("<article") == page.locator("article.card").count(),
          "one article per book, same as the page")

    browser.close()

print("\nJS errors:", errors or "none")
if failures or errors:
    print(f"\n{len(failures)} failure(s).")
    sys.exit(1)
print("\nUI smoke passed.")
