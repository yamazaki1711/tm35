import sys
from playwright.sync_api import sync_playwright

BASE = "https://tm.asd-kontur.ru"
AUTH = {"username": "tm-35", "password": "TM-35-20026!"}
PAGES = ["/", "/works", "/resources", "/quality", "/form"]

with sync_playwright() as p:
    browser = p.chromium.launch()
    context = browser.new_context(http_credentials=AUTH, ignore_https_errors=True)
    page = context.new_page()
    console_errors = []
    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda exc: console_errors.append(str(exc)))

    for path in PAGES:
        url = BASE + path
        resp = page.goto(url, wait_until="networkidle", timeout=20000)
        fname = f"/home/oleg/Documents/TM-35/deploy/screens{path.replace('/', '_') or '_home'}.png"
        page.screenshot(path=fname, full_page=True)
        title = page.title()
        body_text_len = len(page.inner_text("body"))
        print(f"{path} -> status={resp.status} title={title!r} body_chars={body_text_len} screenshot={fname}")

    if console_errors:
        print("CONSOLE/PAGE ERRORS:")
        for e in console_errors:
            print(" ", e)
    else:
        print("No console/page errors.")

    browser.close()
