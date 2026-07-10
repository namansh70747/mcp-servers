"""Probe Google password step after Log In with Google (debug only — not used in manual flow)."""
from __future__ import annotations

import importlib.util
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("apollo", ROOT / "servers" / "apollo" / "server.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

email, password = mod._google_creds()
cdp = mod._cdp_url()
with sync_playwright() as p:
    browser = None
    page = None
    try:
        browser = p.chromium.connect_over_cdp(cdp)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.new_page()
        page.goto(f"{mod.APOLLO_APP}/#/login", wait_until="domcontentloaded", timeout=60000)
        time.sleep(2)
        mod._click_google_sso(page)
        time.sleep(3)
        for _ in range(15):
            url = page.url
            print("url:", url[:90])
            if "accounts.google.com" in url:
                body = page.inner_text("body")[:500].replace("\n", " | ")
                print("body:", body)
                if "identifier" in url or page.locator("#identifierId").first.is_visible(timeout=500):
                    mod._fill_first(page, mod._GOOGLE_EMAIL_SELECTORS, email or "")
                    mod._click_submit(page)
                    time.sleep(3)
                    continue
                if page.locator("input[name=Passwd]").first.is_visible(timeout=1000):
                    loc = page.locator("input[name=Passwd]").first
                    loc.click()
                    loc.fill("")
                    page.keyboard.type(password or "", delay=80)
                    mod._click_submit(page)
                    time.sleep(8)
                    print("AFTER url:", page.url[:90])
                    print("AFTER body:", page.inner_text("body")[:500].replace("\n", " | "))
                    code, hint = mod._classify_google_error(page)
                    print("error_code:", code)
                    break
            time.sleep(2)
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
