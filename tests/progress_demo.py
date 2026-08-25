"""Watch the progress panel during a long run, in a real browser.

Needs a server on localhost:8080 (mock mode with a delay is ideal) and a large
CSV. Prints what the panel says every few seconds and saves a screenshot.

  python tests/make_big_csv.py 1200 /tmp/big.csv
  SHELFWATCHR_MOCK=1 SHELFWATCHR_MOCK_SYNTHETIC=1 SHELFWATCHR_MOCK_DELAY_MS=60 \
    uvicorn app.main:app --port 8080
  python tests/progress_demo.py
"""

import sys

from playwright.sync_api import sync_playwright

CSV = sys.argv[1] if len(sys.argv) > 1 else "/tmp/big.csv"
BASE = "http://127.0.0.1:8080"

errors = []
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 430, "height": 900}, device_scale_factor=2)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(BASE)
    page.evaluate("localStorage.setItem('shelfwatchr.theme','light')")
    page.reload()

    for term, label in (("queens", "Queens"), ("westmount", "Westmount"), ("aubora", "Aubora")):
        page.fill("#lib-search", term)
        page.wait_for_selector(f".suggestions button:has-text('{label}')", timeout=8000)
        page.click(f".suggestions button:has-text('{label}')")

    page.set_input_files("#csv-input", CSV)
    page.wait_for_selector("#shelf-row:not([hidden])")
    page.click("#btn-check")

    page.wait_for_selector("#progress:not([hidden])", timeout=5000)
    print(f"panel appears immediately: {page.text_content('#progress-count')!r}\n")

    for i in range(5):
        page.wait_for_timeout(4000)
        print(f"t+{(i + 1) * 4:2d}s  {page.text_content('#progress-pct'):>4}  "
              f"{page.text_content('#progress-count'):24}")
        print(f"      phase:  {page.text_content('#progress-phase')}")
        print(f"      detail: {page.text_content('#progress-detail')}")

    sticky = "floating" in (page.get_attribute("#progress", "class") or "")
    print(f"\nfollows the page while scrolling: {sticky}")

    # The panel itself, and then the page scrolled down to show it staying put.
    page.locator("#progress").screenshot(path="/tmp/shot-progress-panel.png")
    page.mouse.wheel(0, 1400)
    page.wait_for_timeout(400)
    # Whole viewport: the panel pins to the bottom, so a top-clipped shot misses it.
    page.screenshot(path="/tmp/shot-progress-sticky.png")
    box = page.locator("#progress").bounding_box()
    view = page.viewport_size
    print(f"panel sits at y={box['y']:.0f}–{box['y'] + box['height']:.0f} "
          f"in a {view['height']}px viewport (on screen: "
          f"{box['y'] < view['height'] and box['y'] + box['height'] > 0})")

    page.click("#btn-cancel")
    page.wait_for_function("!state.running", timeout=60000)
    print(f"panel hidden once finished: {page.locator('#progress').is_hidden()}")
    browser.close()

print("JS errors:", errors or "none")
