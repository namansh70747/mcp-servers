"""CDP helpers — connect Playwright to an existing Chrome/Edge via remote debugging.

Used by apollo (background tabs) and any server that must not launch Playwright Chromium.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp_base import get_env, get_env_bool, http

DEFAULT_CDP_URL = "http://127.0.0.1:9222"
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def cdp_url() -> str:
    return (get_env("BROWSER_CDP_URL") or DEFAULT_CDP_URL).strip().rstrip("/")


def cdp_alive(url: str | None = None) -> bool:
    base = (url or cdp_url()).rstrip("/")
    try:
        r = http.request("GET", f"{base}/json/version", timeout=3)
        return bool(r.get("ok") and r.get("json"))
    except Exception:
        return False


def playwright_available() -> tuple[bool, str | None]:
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        return True, None
    except Exception:
        return False, "uv sync --group browser && uv run playwright install chromium"


def allow_headless_fallback() -> bool:
    return get_env_bool("APOLLO_ALLOW_HEADLESS_FALLBACK", False)


def _is_login_url(url: str) -> bool:
    u = (url or "").lower()
    return any(x in u for x in ("/login", "/sign-in", "/sign_up", "/signup", "/register"))


def pick_cdp_page(context, *, url_hint: str = "apollo.io", prefer_fresh: bool = False):
    """Pick an existing tab or create one. prefer_fresh=True always opens a new tab (OAuth)."""
    if not prefer_fresh:
        for page in context.pages:
            try:
                u = (page.url or "").lower()
                if url_hint in u and not _is_login_url(u):
                    return page, "cdp-reuse"
            except Exception:
                continue
    page = context.new_page()
    return page, "cdp-new"


def with_cdp_page(
    work: Callable[..., Any],
    *,
    cdp: str | None = None,
    headless_profile_dir: str | None = None,
    user_agent: str = _BROWSER_UA,
    reuse_tab: bool = False,
    url_hint: str = "apollo.io",
    prefer_fresh_tab: bool = False,
    close_on_done: bool | None = None,
) -> Any:
    """Run `work(page, backend)` on a CDP tab. Optional headless fallback."""
    from playwright.sync_api import sync_playwright

    target = (cdp or cdp_url()).rstrip("/")
    with sync_playwright() as p:
        if cdp_alive(target):
            browser = p.chromium.connect_over_cdp(target)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            if reuse_tab and not prefer_fresh_tab:
                page, backend = pick_cdp_page(ctx, url_hint=url_hint)
            else:
                page = ctx.new_page()
                backend = "cdp"
            try:
                return work(page, backend)
            finally:
                should_close = (
                    close_on_done
                    if close_on_done is not None
                    else (backend == "cdp-new" or (backend == "cdp" and not reuse_tab))
                )
                if should_close:
                    try:
                        page.close()
                    except Exception:
                        pass
        if allow_headless_fallback() and headless_profile_dir:
            from pathlib import Path
            prof = Path(headless_profile_dir)
            prof.mkdir(parents=True, exist_ok=True)
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(prof), headless=True,
                viewport={"width": 1366, "height": 900}, user_agent=user_agent,
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                return work(page, "headless")
            finally:
                ctx.close()
        return {
            "error": "CDP browser not running",
            "hint": "Run apollo.start_background_browser() or automation/setup-apollo-web.ps1",
            "cdp_url": target,
        }
