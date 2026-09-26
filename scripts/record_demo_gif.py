"""Record the README demo GIF from a real session with the running demo (headless Chromium + ffmpeg).

    python scripts/record_demo_gif.py [URL]      # default http://127.0.0.1:7860; spends ~$0.05 of API credit
    python scripts/record_demo_gif.py --encode-only  # re-encode runs/demo/demo.webm, no API calls

Asks two real questions, then approves a draft as a named human. Writes docs/images/demo.gif. Waiting
for the model is sped up in the GIF (the caption says so); the answers and tool calls are not edited.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parents[1] / "docs" / "images" / "demo.gif"
URL = next((a for a in sys.argv[1:] if not a.startswith("--")), "http://127.0.0.1:7860")
SIZE = {"width": 1280, "height": 760}
SPEEDUP = 2.0
KEEP = Path(__file__).resolve().parents[1] / "runs" / "demo" / "demo.webm"  # source video, for re-encoding


def wait_answer(page, n: int) -> None:
    page.wait_for_function(
        f"document.querySelectorAll('.answer, .notice.error').length > {n}", timeout=120_000
    )


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport=SIZE, record_video_dir=tmp, record_video_size=SIZE)
        page = ctx.new_page()
        page.goto(URL)
        page.wait_for_selector(".examples button")
        page.wait_for_timeout(1500)

        page.locator(".examples button", has_text="most at risk").click()
        wait_answer(page, 0)
        page.wait_for_timeout(2500)
        page.locator(".answer").last.scroll_into_view_if_needed()
        page.wait_for_timeout(2000)

        page.locator("textarea").type("Draft a reorder for CA_2 HOBBIES_1.", delay=35)
        page.click("button[type=submit]")
        wait_answer(page, 1)
        page.wait_for_selector(".draft")
        page.wait_for_timeout(1500)

        card = page.locator(".draft").first
        card.scroll_into_view_if_needed()
        card.locator("input[aria-label='Your name']").type("Aziz (buyer)", delay=40)
        card.locator("button:has-text('Approve')").click()
        page.wait_for_selector(".badge.approved")
        page.wait_for_timeout(2500)
        video = Path(page.video.path())
        ctx.close()
        browser.close()
        KEEP.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(video, KEEP)
        encode(KEEP, Path(tmp))
    print(f"wrote {OUT} ({OUT.stat().st_size / 1e6:.1f} MB)")


def encode(video: Path, tmp: Path, start_s: float = 1.2) -> None:
    """webm -> GIF: skip the page load, speed up, 8 fps, 880 px wide, one shared palette."""

    palette = tmp / "palette.png"
    vf = f"trim=start={start_s},setpts=(PTS-STARTPTS)/{SPEEDUP},fps=8,scale=880:-1:flags=lanczos"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", video, "-vf", f"{vf},palettegen=max_colors=128:stats_mode=diff", palette],
        check=True,
    )  # fmt: skip
    gif = tmp / "demo.gif"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", video, "-i", palette,
         "-lavfi", f"{vf}[x];[x][1:v]paletteuse=dither=none:diff_mode=rectangle", gif],
        check=True,
    )  # fmt: skip
    OUT.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(gif, OUT)


if __name__ == "__main__":
    if "--encode-only" in sys.argv:  # re-encode the kept video without asking the model again
        with tempfile.TemporaryDirectory() as t:
            encode(KEEP, Path(t))
        print(f"wrote {OUT} ({OUT.stat().st_size / 1e6:.1f} MB)")
    else:
        main()
