"""Regenerate the README screenshots from the offline demo (needs `pip install playwright` and a Chromium:
`playwright install chromium`).

    python scripts/screenshots.py
"""
import os
import sys
import threading
import time
from contextlib import ExitStack
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "screenshots"
PORT = 8765


def start_demo_server():
    os.environ["DATABASE_URL"] = "sqlite:///" + (ROOT / "demo.db").as_posix()
    os.environ["ADMIN_PASSWORD"] = "demo"
    os.environ["ALLOW_OPEN_REGISTRATION"] = "1"
    sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)
    from app.main import app
    from scripts import demo
    import uvicorn

    demo.build_database(ROOT / "demo.db")
    stack = ExitStack()
    demo.install_fakes(demo.World(), stack)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.1)
    return server, stack


def main() -> None:
    from playwright.sync_api import sync_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    server, stack = start_demo_server()
    base = f"http://127.0.0.1:{PORT}"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1280, "height": 900}, device_scale_factor=2)
            page = ctx.new_page()
            page.goto(f"{base}/login")
            page.fill("[name=username]", "ioana")
            page.fill("[name=password]", "demo-pass")
            page.click("button[type=submit], button:has-text('Sign in')")
            page.wait_for_url(lambda url: "/login" not in url)

            def shot(name, clip_selector=None, **kw):
                target = OUT / f"{name}.png"
                if clip_selector:
                    page.locator(clip_selector).first.screenshot(path=str(target))
                else:
                    page.screenshot(path=str(target), **kw)
                print("wrote", target.relative_to(ROOT), f"({target.stat().st_size // 1024} KB)")

            # 1. the assignment matrix: a team gym virtual expanded next to a live round
            page.goto(f"{base}/assignments/1")
            for item_id in (1, 2):
                page.click(f"#tog-{item_id}")
            page.wait_for_timeout(300)
            shot("matrix", ".box:has(table.t)")

            # 2. a problem's hints and notes, entries opened
            page.locator("tr.sub .hint-btn.has-hints").first.click()
            page.wait_for_selector("#hint-list .hint-toggle")
            for toggle in page.locator("#hint-list .hint-toggle").all():
                toggle.click()
            page.wait_for_timeout(300)
            shot("hints", "#hint-overlay .modal")
            page.keyboard.press("Escape")

            # 3. a member's profile: heatmap + recent submissions (light and dark)
            page.goto(f"{base}/users/ioana")
            page.locator("details.box summary").click()
            page.wait_for_selector("#heatmap-container svg")
            drop_old_years = "[...document.querySelectorAll('#heatmap-container > div')].slice(2).forEach(e => e.remove())"
            page.evaluate(drop_old_years)  # the oldest year is mostly empty in the demo
            page.wait_for_timeout(500)
            shot("profile", full_page=True)
            page.evaluate("document.documentElement.setAttribute('data-theme','dark')")
            page.wait_for_timeout(600)  # switching theme redraws the heatmap
            page.evaluate(drop_old_years)
            page.wait_for_timeout(300)
            shot("profile-dark", full_page=True)
            browser.close()
    finally:
        server.should_exit = True
        stack.close()


if __name__ == "__main__":
    main()
