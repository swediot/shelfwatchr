"""Checks the filter and sort controls against a real render.

The ordering rules live in JavaScript and only mean anything once the DOM is
built, so this drives a browser. Needs a server on localhost:8080 in mock mode:

  SHELFWATCHR_MOCK=1 SHELFWATCHR_MOCK_SYNTHETIC=1 uvicorn app.main:app --port 8080
  python tests/filter_check.py
"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
# A 60-book list, not the 4-book one: sorting a single-item section proves
# nothing. Regenerate with `python tests/make_big_csv.py 60 tests/fixtures/varied.csv`.
FIXTURE = str(Path(__file__).parent / "fixtures" / "varied.csv")
failures = []


def check(condition, message):
    if condition:
        print(f"  ok — {message}")
    else:
        failures.append(message)
        print(f"  FAILED — {message}")


def titles(page):
    """Visible cards, in the order they're painted."""
    return page.locator("article.card h3").all_inner_texts()


def settle(page):
    page.wait_for_timeout(350)


errors = []
with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1100, "height": 900})
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))

    page.goto(BASE)
    page.evaluate("() => localStorage.removeItem('shelfwatchr.view.v1')")
    page.reload()
    for term, label in (("queens", "Queens"), ("westmount", "Westmount"), ("aubora", "Aubora")):
        page.fill("#lib-search", term)
        page.wait_for_selector(f".suggestions button:has-text('{label}')", timeout=8000)
        page.click(f".suggestions button:has-text('{label}')")
    page.set_input_files("#csv-input", FIXTURE)
    page.wait_for_selector("#shelf-row:not([hidden])")
    page.click("#btn-check")
    page.wait_for_selector("#results article.card", timeout=120000)
    page.wait_for_function("!state.running", timeout=120000)
    settle(page)

    # Every "show all" open, so a section's cap never masquerades as a filter.
    page.evaluate("() => { for (const k of Object.keys(state.expanded)) state.expanded[k] = true; "
                  "renderResults(true); }")
    settle(page)
    baseline = titles(page)

    print("\nThe controls appear with the results")
    check(not page.locator("#controls").is_hidden(), "the control bar is showing")
    check(page.locator("#filter-panel").is_hidden(), "with the filter panel folded away")
    # Regression: the button was built with replaceChildren(text, cond ? node : null),
    # which renders the word "null" when no filters are set.
    check(page.locator("#btn-filters").inner_text().strip() == "Filters",
          f"the Filters button reads cleanly with nothing set: "
          f"{page.locator('#btn-filters').inner_text()!r}")
    libs = page.locator("#f-library option").all_inner_texts()
    check(len(libs) == 4, f"the library menu is built from the report: {libs}")
    check(len(baseline) > 20, f"and there's a real list to work on ({len(baseline)} books)")
    # Every check below looks books up by title. Duplicates would make them
    # compare one book's card against another's data and fail for no reason.
    check(len(set(baseline)) == len(baseline), "with no two books sharing a title")

    print("\nFormat toggle")
    seg = page.locator("#format-toggle button")
    check(seg.count() == 3, "three positions: audiobooks, ebooks, both")
    check(page.locator('#format-toggle button[aria-pressed="true"]').inner_text() == "Both",
          "starting on both")
    rows_per_book = page.evaluate("() => state.results[0].results.length")
    scopes = page.evaluate("() => state.scopes.length")
    check(rows_per_book == scopes * 2,
          f"one run fetched both formats ({rows_per_book} rows for {scopes} libraries)")
    check(sorted(page.evaluate(
        "() => [...new Set(state.results.flatMap(b => b.results.map(r => r.fmt)))]")) ==
        ["audiobook-overdrive", "ebook-overdrive"],
        "and each row says which format it is")

    both = titles(page)
    shown_both = page.evaluate(
        "() => [...document.querySelectorAll('article.card')].length")
    page.click('#format-toggle button[data-format="audiobook"]')
    settle(page)
    ok = page.evaluate("""() => {
        const byTitle = Object.fromEntries(state.results.map(b => [b.title, b]));
        return [...document.querySelectorAll('article.card')].every(card => {
            const book = byTitle[card.querySelector('h3').textContent];
            const pill = card.querySelector('.card-head .pill').textContent;
            const audio = book.results.filter(r => r.fmt === 'audiobook-overdrive');
            // The headline must come from an audiobook row, not the ebook one.
            return audio.some(r => r.status === 'available') === pill.startsWith('Available');
        });
    }""")
    check(ok, "on Audiobooks the headline comes from the audiobook rows only")
    check(page.locator(".card .fmt-tag").count() == 0,
          "and rows drop the format tag — every row is that format")

    page.click('#format-toggle button[data-format="both"]')
    settle(page)
    check(titles(page) == both, "switching back restores the merged view")
    card = page.locator("article.card", has=page.locator(".expand")).first
    card.locator(".expand").click()
    settle(page)
    check(card.locator(".lib").count() == scopes,
          f"an opened card lists each library once, not once per format "
          f"({card.locator('.lib').count()} for {scopes})")
    check(card.locator(".fmt-tag").count() == scopes,
          "each row saying which format won for that library")
    card.locator(".expand").click()
    settle(page)

    print("\nSearch")
    # Search is by word, not substring — "ash hollow" should find "The Hollow Ash"
    # too — so the check is that every word turns up, not that the phrase does.
    words = [w.lower() for w in baseline[0].split()[:3]]
    page.fill("#q", " ".join(words))
    settle(page)
    found = titles(page)
    # Author counts as a match too, so check title-plus-author, not title alone.
    hay = page.evaluate("""() => {
        const byTitle = Object.fromEntries(state.results.map(b => [b.title, b]));
        return [...document.querySelectorAll('article.card h3')].map(h => {
            const b = byTitle[h.textContent];
            return `${b.title} ${b.author}`.toLowerCase();
        });
    }""")
    check(found and all(all(w in h for w in words) for h in hay),
          f"searching {' '.join(words)!r} leaves only matches ({len(found)})")
    check("of" in page.locator("#control-note").inner_text(),
          f"and says how many of how many: {page.locator('#control-note').inner_text()!r}")
    page.fill("#q", "zzzzz no such book")
    settle(page)
    check("No books match" in page.locator("#results").inner_text(),
          "a search with no hits says so plainly")
    page.fill("#q", "")
    settle(page)
    check(titles(page) == baseline, "clearing it puts every book back")

    print("\nFilters")
    page.click("#btn-filters")
    settle(page)
    check(not page.locator("#filter-panel").is_hidden(), "the Filters button opens the panel")

    page.select_option("#f-status", "available")
    settle(page)
    ok = page.evaluate("""() => {
        const byTitle = Object.fromEntries(state.results.map(b => [b.title, b]));
        return [...document.querySelectorAll('article.card h3')]
            .every(h => byTitle[h.textContent].results.some(r => r.status === 'available'));
    }""")
    check(ok, "filtering to on-the-shelf leaves only books that are")
    check(len(titles(page)) < len(baseline), "and it's a real narrowing, not a no-op")
    check(page.locator("#btn-filters .badge").inner_text() == "1",
          "the button carries a count of what's active")

    page.select_option("#f-wait", "14")
    settle(page)
    check(page.locator("#btn-filters .badge").inner_text() == "2", "two filters, count of two")

    page.click("#btn-clear-filters")
    settle(page)
    check(page.locator("#btn-filters .badge").count() == 0, "clearing drops the count")
    check(titles(page) == baseline, "and restores the full list")

    lib_key = page.evaluate("() => document.querySelector('#f-library option:nth-child(2)').value")
    page.select_option("#f-library", lib_key)
    settle(page)
    ok = page.evaluate("""(key) => {
        const byTitle = Object.fromEntries(state.results.map(b => [b.title, b]));
        return [...document.querySelectorAll('article.card h3')].every(h =>
            byTitle[h.textContent].results.some(r => r.scope_key === key && r.status !== 'not_owned'));
    }""", lib_key)
    check(ok, "filtering by library leaves only books that library holds")
    page.click("#btn-clear-filters")
    settle(page)

    print("\nSorting")
    page.select_option("#sort", "title-az")
    settle(page)
    az = titles(page)
    page.select_option("#sort", "title-za")
    settle(page)
    za = titles(page)
    check(az != za and set(az) == set(za), "A–Z and Z–A hold the same books in different orders")
    # Reversed per section, not across the whole page, so compare within one.
    first_az = page.evaluate("() => [...document.querySelectorAll('section.group')][0]"
                             "?.querySelectorAll('h3').length")
    check(az[:first_az] == list(reversed(za[:first_az])),
          "and the first section is exactly the mirror of itself")

    page.select_option("#sort", "author-az")
    settle(page)
    surnames = page.evaluate("""() => {
        const byTitle = Object.fromEntries(state.results.map(b => [b.title, b]));
        const sec = [...document.querySelectorAll('section.group')][0];
        return [...sec.querySelectorAll('h3')].map(h => surname(byTitle[h.textContent].author));
    }""")
    check(surnames == sorted(surnames), f"author sort goes by surname: {surnames[:4]}")

    page.select_option("#sort", "added-first")
    settle(page)
    dates = page.evaluate("""() => {
        const byTitle = Object.fromEntries(state.results.map(b => [b.title, b]));
        const sec = [...document.querySelectorAll('section.group')][0];
        return [...sec.querySelectorAll('h3')].map(h => byTitle[h.textContent].added || '9999');
    }""")
    check(dates == sorted(dates), f"added-first is oldest first: {dates[:3]}")
    check(any(d != "9999" for d in dates), "and the dates actually came through the import")

    page.select_option("#sort", "longest")
    settle(page)
    lens = page.evaluate("""() => {
        const byTitle = Object.fromEntries(state.results.map(b => [b.title, b]));
        const sec = [...document.querySelectorAll('section.group')][0];
        return [...sec.querySelectorAll('h3')].map(h => byTitle[h.textContent].duration_seconds ?? -1);
    }""")
    check(lens == sorted(lens, reverse=True), f"longest first: {[round(x/3600,1) for x in lens[:4]]}")
    check(any(x > 0 for x in lens), "durations reached the browser")

    print("\nRandom")
    page.select_option("#sort", "random")
    settle(page)
    shuffled = titles(page)
    check(set(shuffled) == set(baseline), "random keeps every book")
    check(shuffled != az, "in some other order")
    page.evaluate("() => renderResults(true)")
    settle(page)
    check(titles(page) == shuffled, "and a re-render doesn't reshuffle under your thumb")
    page.click("#control-note button")   # "Shuffle again"
    settle(page)
    check(titles(page) != shuffled, "but 'shuffle again' does")

    print("\nIt survives a reload")
    page.select_option("#sort", "title-az")
    page.select_option("#f-status", "available")
    settle(page)
    kept = titles(page)
    page.reload()
    page.wait_for_selector("#results article.card", timeout=120000)
    page.wait_for_function("!state.running", timeout=120000)
    page.evaluate("() => { for (const k of Object.keys(state.expanded)) state.expanded[k] = true; "
                  "renderResults(true); }")
    settle(page)
    check(page.locator("#sort").input_value() == "title-az", "the sort came back")
    check(page.locator("#f-status").input_value() == "available", "so did the filter")
    check(not page.locator("#filter-panel").is_hidden(),
          "with the panel opened, so a short list has a visible reason")
    check(titles(page) == kept, "and the same books are on screen")

    print("\nThe download matches what's on screen")
    with page.expect_download() as dl:
        page.click("#btn-download")
    html = Path(dl.value.path()).read_text(encoding="utf-8")
    check(html.count("<article") == len(kept), f"same number of books ({len(kept)})")
    check("filtered" in html, "and it admits it's a filtered copy")

    page.click("#btn-clear-filters")
    ctx.close()
    browser.close()

print("\nJS errors:", errors or "none")
if failures or errors:
    print(f"\n{len(failures)} failure(s).")
    sys.exit(1)
print("\nFilter and sort checks passed.")
