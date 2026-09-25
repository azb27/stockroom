"""Drive the web demo in headless Chromium and save screenshots for the README.

    python scripts/screenshot_demo.py [URL]      # default http://127.0.0.1:7860; spends ~$0.10 of API credit

Asks two real questions (a reorder draft and the outage day), approves one draft as a named human, and
saves docs/images/web_demo*.png. The screenshots show real answers from a real run, not mock-ups.
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parents[1] / "docs" / "images"
URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7860"


def ask(page, question: str) -> None:
    n = page.locator(".answer, .notice.error").count()
    page.fill("textarea", question)
    page.click("button[type=submit]")
    page.wait_for_function(
        f"document.querySelectorAll('.answer, .notice.error').length > {n}", timeout=120_000
    )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000}, device_scale_factor=2)
        page.goto(URL)
        page.wait_for_selector(".stats")
        page.screenshot(path=OUT / "web_demo_empty.png")
        phone = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2)
        phone.goto(URL)
        phone.wait_for_selector(".stats")
        phone.screenshot(path=OUT / "web_demo_mobile.png", full_page=True)
        phone.close()

        ask(page, "Draft a reorder for CA_2 HOBBIES_1.")
        page.wait_for_selector(".draft")
        ask(page, "What were CA_4's total unit sales on 2016-03-14?")
        page.locator(".trace").last.evaluate("el => el.open = true")
        page.screenshot(path=OUT / "web_demo.png", full_page=True)

        card = page.locator(".draft").first
        card.locator("text=Show lines").click()
        card.locator("input[aria-label='Your name']").fill("Aziz (buyer)")
        card.locator("input[aria-label='Note']").fill("Checked lead times")
        card.locator("button:has-text('Approve')").click()
        page.wait_for_selector(".badge.approved")
        page.locator(".grid .panel").nth(1).screenshot(path=OUT / "web_demo_approval.png")
        browser.close()
    print(f"wrote {OUT}/web_demo*.png")


if __name__ == "__main__":
    main()
