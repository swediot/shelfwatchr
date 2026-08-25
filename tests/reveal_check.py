"""Checks the two ways the queue detail is revealed: hover, and the toggle.

Needs a server on localhost:8080 in mock mode.
"""

import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
FIXTURE = str(Path(__file__).parent / "fixtures" / "goodreads.csv")
failures = []


def check(condition, message):
    if condition:
        print(f"  ok — {message}")
    else:
        failures.append(message)
        print(f"  FAILED — {message}")


def setup(page):
    page.goto(BASE)
    for term, label in (("queens", "Queens"), ("westmount", "Westmount"), ("aubora", "Aubora")):
        page.fill("#lib-search", term)
        page.wait_for_selector(f".suggestions button:has-text('{label}')", timeout=8000)
        page.click(f".suggestions button:has-text('{label}')")
    page.set_input_files("#csv-input", FIXTURE)
    page.wait_for_selector("#shelf-row:not([hidden])")
    page.click("#btn-check")
    # Wait on the rendered result, not on state.running: the flag is false both
    # before a run starts and after it ends, so waiting for "not running" can
    # pass instantly — and waiting for "running" can miss a fast cached run.
    page.wait_for_selector("#results article.card", timeout=120000)
    page.wait_for_function("!state.running", timeout=120000)
    page.wait_for_timeout(300)


errors = []
with sync_playwright() as p:
    browser = p.chromium.launch()

    print("\nDesktop — hover reveals it")
    ctx = browser.new_context(viewport={"width": 1100, "height": 900}, color_scheme="dark")
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))
    setup(page)

    check(page.locator(".card .libs:visible").count() == 0,
          "cards start collapsed — one status, no library list")
    page.locator("article.card", has=page.locator(".expand")).first.locator(".expand").click()
    page.wait_for_timeout(250)

    row = page.locator(".lib", has=page.locator(".queue-pop")).first
    check(row.locator(".queue").is_visible(), "expanding shows the queue numbers")
    check(not row.locator(".queue-pop").is_visible(), "but not the spelled-out tooltip")

    # Scroll first, then measure: hover() scrolls the element into view on its
    # own, which would move the box for reasons that have nothing to do with
    # the tooltip.
    row.scroll_into_view_if_needed()
    page.wait_for_timeout(150)
    before = row.bounding_box()
    card_before = page.locator("article.card").first.bounding_box()
    row.hover()
    page.wait_for_timeout(250)
    after = row.bounding_box()
    card_after = page.locator("article.card").first.bounding_box()
    check(row.locator(".queue-pop").is_visible(), "hovering the row reveals it")
    text = row.locator(".queue-pop").inner_text()
    check("waiting" in text, f"as a sentence, not glyphs: {text!r}")
    check(abs(after["y"] - before["y"]) < 1 and abs(after["height"] - before["height"]) < 1,
          "the row itself doesn't move or grow")
    check(abs(card_after["height"] - card_before["height"]) < 1,
          "and neither does the card around it")

    # Regression: the tooltip used to be `background: var(--ink)`, an inversion
    # that reads as raised on a light page and as a glaring white box on a dark
    # one. It needs its own tokens, so check it's darker than the card in dark
    # mode rather than brighter.
    def luma(css_colour):
        r, g, b = [int(n) for n in re.findall(r"\d+", css_colour)[:3]]
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    tip = page.evaluate("""() => {
        const pop = document.querySelector('.queue-pop');
        const card = document.querySelector('article.card');
        return [getComputedStyle(pop).backgroundColor, getComputedStyle(card).backgroundColor];
    }""")
    check(luma(tip[0]) > luma(tip[1]),
          f"in dark mode the tooltip lifts off the card rather than inverting: "
          f"{tip[0]} on {tip[1]}")
    page.locator("section.group").nth(1).screenshot(path="/tmp/shot-hover.png")
    ctx.close()

    print("\nMobile — a toggle, since there's no hover")
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True)
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))
    setup(page)

    check(page.locator(".card .libs:visible").count() == 0, "cards start collapsed here too")
    card = page.locator("article.card", has=page.locator(".expand")).first
    toggle = card.locator(".expand")
    box = toggle.bounding_box()
    check(box["width"] >= 44 and box["height"] >= 44,
          f"the chevron meets the 44px touch-target guideline "
          f"({box['width']:.0f}x{box['height']:.0f}px)")

    page.tap(".expand")
    page.wait_for_timeout(300)
    check(card.locator(".lib").first.is_visible(), "tapping it reveals the libraries")
    check(card.locator(".queue").first.is_visible(), "with the queue numbers")
    check(card.locator(".legend").count() == 1, "and a legend inside that card")
    heights = card.locator(".lib").evaluate_all("els => els.map(e => e.getBoundingClientRect().height)")
    check(max(heights) < 40, f"rows stay one line ({max(heights):.0f}px)")
    page.locator("section.group").nth(1).screenshot(path="/tmp/shot-mobile-on.png")

    page.tap(".expand")
    page.wait_for_timeout(250)
    check(card.locator(".libs:visible").count() == 0, "and tapping again puts it away")
    ctx.close()
    browser.close()

print("\nJS errors:", errors or "none")
if failures or errors:
    print(f"\n{len(failures)} failure(s).")
    sys.exit(1)
print("\nReveal checks passed.")
