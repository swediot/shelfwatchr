"""Browser checks for the setup panels, the format glyphs, and the save link.

Everything here lives in app.js, so it needs a real browser. Same server as
ui_smoke.py:

  SHELFWATCHR_MOCK=1 SHELFWATCHR_MOCK_SYNTHETIC=1 uvicorn app.main:app --port 8080
  python tests/ui_panels.py
"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
FIXTURES = Path(__file__).parent / "fixtures"
failures = []


def check(condition, message):
    print(f"  {'ok' if condition else 'FAILED'} - {message}")
    if not condition:
        failures.append(message)


def pick_libraries(page, *terms):
    """More than one, so cards get the chevron that reveals per-library rows —
    which is where the format marker lives."""
    for term, label in terms:
        page.fill("#lib-search", term)
        page.wait_for_selector(f".suggestions button:has-text('{label}')", timeout=15000)
        page.click(f".suggestions button:has-text('{label}')")
    page.wait_for_function(f"() => state.scopes.length === {len(terms)}", timeout=15000)


def run(page):
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(BASE)
    page.wait_for_selector("#dropzone")

    print("\nNo step numbers")
    check(page.locator(".step-no").count() == 0, "the 1 and 2 badges are gone")
    check(page.locator("#panel-libs .panel-body").count() == 1,
          "the libraries panel has a body that can fold")

    print("\nBefore anything is chosen")
    check(page.locator("#libs-toggle").is_hidden(),
          "an empty panel doesn't offer to collapse itself")
    check("Drop your CSV here" in page.locator("#dropzone").inner_text(),
          "the dropzone asks for a file")

    pick_libraries(page, ("queens", "Queens"), ("westmount", "Westmount"))
    check(page.locator("#libs-toggle").is_visible(),
          "once a library is picked, the panel can be folded")
    check(not page.locator("#panel-libs").evaluate("e => e.classList.contains('collapsed')"),
          "but picking one doesn't fold it out from under you mid-choice")

    print("\nUploading a list")
    page.set_input_files("#csv-input", str(FIXTURES / "storygraph.csv"))
    page.wait_for_function("() => state.books.length > 0", timeout=15000)
    page.wait_for_timeout(300)
    dz = page.locator("#dropzone").inner_text()
    check("storygraph.csv" in dz, f"the dropzone names the file it took: {dz.splitlines()[0]!r}")
    check("Drop your CSV here" not in dz,
          "and stops asking for one, so the upload doesn't read as a no-op")

    # The fixture's four to-read books include one marked Owned? = Yes.
    print("\nBooks you already own")
    opts = page.locator("#shelf-select option").all_inner_texts()
    check(not any("unowned" in o.lower() for o in opts),
          f"no 'unowned' variant to choose between: {opts}")
    check(page.evaluate("() => state.books.length") == 3,
          "the owned one is dropped without being asked about "
          f"({page.evaluate('() => state.books.length')} of 4 kept)")
    check(not any("Fifth Season" in b for b in
                  page.evaluate("() => state.books.map(b => b.title)")),
          "and it's the owned title that went")
    check(any("(3)" in o for o in opts),
          f"the menu counts what will actually be checked: {opts}")

    print("\nOnce the report lands")
    page.click("#btn-check")
    page.wait_for_function("() => !state.running && state.results.length", timeout=120000)
    page.wait_for_timeout(500)
    check(page.locator("#panel-libs").evaluate("e => e.classList.contains('collapsed')"),
          "the libraries panel folds away")
    check(page.locator("#libs-summary").is_visible()
          and page.locator("#libs-summary").inner_text().strip() != "",
          f"saying what it settled on: {page.locator('#libs-summary').inner_text()!r}")
    check(page.locator("#libs-summary").inner_text().count("·") == 1,
          "listing both libraries, not just the first")
    check(page.locator("#panel-list").is_hidden(), "and the upload panel steps aside")
    check(page.locator("#btn-new-list").is_visible(),
          "with a button to bring it back")

    page.click("#libs-toggle")
    page.wait_for_timeout(200)
    check(not page.locator("#panel-libs").evaluate("e => e.classList.contains('collapsed')"),
          "the libraries panel reopens on demand")
    check(page.locator("#lib-search").is_visible(), "with the search box usable again")

    page.click("#btn-new-list")
    page.wait_for_timeout(200)
    check(page.locator("#panel-list").is_visible(), "and 'Upload new list' reopens the other")
    check("Drop your CSV here" in page.locator("#dropzone").inner_text(),
          "cleared, rather than still showing the file it replaced")

    print("\nWhich edition a book is about")
    page.locator('#format-toggle button[data-format="both"]').click()
    page.wait_for_timeout(300)

    # The headline is the whole card for most books - the chevron only appears
    # when a second library has something, so a marker that lived only in the
    # expanded rows was invisible on the majority of cards.
    borrowable = page.locator("article.card").filter(
        has=page.locator(".pill.available, .pill.holdable"))
    n = borrowable.count()
    tagged = borrowable.locator(".card-head .fmt-tag").count()
    check(n > 0, f"there are borrowable books to look at ({n})")
    check(tagged == n,
          f"every borrowable book says which edition, unexpanded ({tagged} of {n})")
    unowned = page.locator("article.card").filter(
        has=page.locator(".card-head .pill.not_owned"))
    check(unowned.locator(".card-head .fmt-tag").count() == 0,
          "and a book no library carries claims no edition at all")
    card = page.locator("article.card", has=page.locator(".expand")).first
    card.locator(".expand").click()
    page.wait_for_timeout(300)
    check(card.locator(".lib").count() > 0,
          f"the card opens to its library rows ({card.locator('.lib').count()})")
    tags = card.locator(".fmt-tag")
    check(tags.count() > 0, f"rows carry a format marker on 'both' ({tags.count()})")
    check(tags.first.locator("svg").count() == 1, "drawn as an icon, not a word")
    labels = set(tags.evaluate_all("els => els.map(e => e.getAttribute('aria-label'))"))
    check(labels and labels <= {"Audiobook", "Ebook"},
          f"named for anyone who can't see it: {labels}")

    page.locator('#format-toggle button[data-format="audiobook"]').click()
    page.wait_for_timeout(300)
    solo = page.locator("article.card", has=page.locator(".expand")).first
    solo.locator(".expand").click()
    page.wait_for_timeout(300)
    check(solo.locator(".lib").count() > 0, "a single-format card opens too")
    check(solo.locator(".fmt-tag").count() == 0,
          "and carries no marker, since every row there is that one format")

    print("\nSaving the list")
    page.locator('#format-toggle button[data-format="both"]').click()
    page.fill("#profile-name", "Panels")
    page.click("#btn-save")
    page.wait_for_function("() => document.getElementById('save-result').textContent.length > 0",
                           timeout=15000)
    note = page.locator("#save-result").inner_text()
    check("clipboard" in note.lower(), f"it says the link was copied: {note!r}")
    pasted = page.evaluate("() => navigator.clipboard.readText()")
    check(pasted.startswith("http") and "?p=" in pasted,
          f"and the link really is on the clipboard: {pasted!r}")
    check(pasted == page.locator("#save-result a").inner_text(),
          "the same link it shows on screen")

    print("\nHow you get told")
    opts = page.locator("#notify-type option").all_inner_texts()
    check(not any("webhook" in o.lower() for o in opts), f"no webhook option: {opts}")
    check(not any("email" in o.lower() or "smtp" in o.lower() for o in opts),
          f"no email option: {opts}")
    check(len(opts) == 2, f"just the two that work here: {opts}")

    print(f"\nJS errors: {errors or 'none'}")
    if errors:
        failures.append("javascript errors")


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    ctx = browser.new_context()
    # Reading the clipboard back is the only way to prove the copy happened.
    ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin=BASE)
    run(ctx.new_page())
    browser.close()

print()
if failures:
    print(f"{len(failures)} failure(s).")
    sys.exit(1)
print("Panel and format tests passed.")
