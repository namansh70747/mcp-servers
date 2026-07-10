"""apollo — find decision-makers via Apollo.io web UI in your existing browser (Option B).

No API key. Uses Chrome/Edge minimized via CDP. ensure_ready() opens the apollo profile
browser, clicks "Log In with Google" once, then waits for you to finish login manually.
find_people() opens background tabs and scrapes silently. find_ceo_email(domain) is the full
Option B pipeline for a company's CEO. find_person_email(name, company) is the general version
for ANY named person/title — both reveal the real email via Apollo's own "Access email" button
(never a guessed pattern), spending at most 1 Apollo credit per call.
"""
from __future__ import annotations

import ipaddress
import importlib.util
import os
import re
import subprocess
import sys
import threading
import time
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, urlparse

from mcp_base import base_data_dir, data_dir, db_path, get_env, get_env_bool, get_env_int, http, make_server
from mcp_base.store import BaseStore
from mcp_base.browser_cdp import (
    allow_headless_fallback,
    cdp_alive as _cdp_alive_shared,
    cdp_url as _cdp_url_shared,
    playwright_available as _pw_available_shared,
    with_cdp_page,
)
from mcp_base.apollo_extension import find_extension_sidebar_frame, wait_for_sidebar_frame

mcp = make_server(
    "apollo",
    instructions=("Find CTO/CEO/team via Apollo web UI in background (no API key). "
                  "Always call find_person_email / find_ceo_email / bulk_find_ceo_email via this "
                  "MCP server — never shell scripts for email lookup. "
                  "Option B: log in once in the apollo profile browser (taskbar). "
                  "Only manual user step: click Apollo FAB once per browser session on LinkedIn. "
                  "Path 1 (fast): Apollo app search + Access Email. Path 2 (fallback only): "
                  "LinkedIn + extension side-panel Access email. "
                  "find_person_email(name, company) — Path 1 then Path 2 if miss. "
                  "find_ceo_email(domain) — CEO Path 1 then extension if miss. "
                  "bulk_find_ceo_email(companies) — batch CEO emails for outreach lists. "
                  "find_people(domain) runs silently without spending credits."),
)

BASE = "https://api.apollo.io/api/v1"
APOLLO_APP = "https://app.apollo.io"
APOLLO_PEOPLE = f"{APOLLO_APP}/#/people"
PROFILE_DIR = data_dir("browser") / "profile"
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

MAX_PER_PAGE = 100
WEB_TIMEOUT_MS = 60000
DEFAULT_CDP_PORT = 9222


def _cdp_url() -> str:
    return _cdp_url_shared()


def _auto_login_enabled() -> bool:
    return get_env_bool("APOLLO_AUTO_LOGIN", True)


def _login_mode() -> str:
    """manual (default) | auto | hybrid. manual = click Google SSO then wait for user."""
    raw = (get_env("APOLLO_LOGIN_MODE") or "manual").strip().lower()
    return raw if raw in ("auto", "manual", "hybrid") else "manual"


def _sanitize_secret(value: str | None) -> str:
    v = (value or "").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1].strip()
    return v


def _google_creds() -> tuple[str | None, str | None]:
    email = _sanitize_secret(get_env("APOLLO_GOOGLE_EMAIL"))
    password = _sanitize_secret(get_env("APOLLO_GOOGLE_PASSWORD"))
    if email and password:
        return email, password
    return None, None


def _missing_creds_hint() -> str:
    return "Set APOLLO_GOOGLE_EMAIL and APOLLO_GOOGLE_PASSWORD in .env, then call ensure_ready()."


def _profile_login_hint() -> str:
    profile = str(_browser_user_data())
    return (
        f"Log in inside the apollo profile browser (NOT your daily Chrome). "
        f"Restore the minimized window from the taskbar — profile: {profile}"
    )


def _browser_user_data() -> Path:
    custom = get_env("BROWSER_USER_DATA_DIR")
    if custom:
        return Path(custom)
    return base_data_dir() / "apollo-browser"


def _cdp_port() -> int:
    try:
        return int(urlparse(_cdp_url()).port or DEFAULT_CDP_PORT)
    except (TypeError, ValueError):
        return DEFAULT_CDP_PORT


def _mode() -> str:
    """web (default) = headless browser only; api = REST API when APOLLO_API_KEY set."""
    m = (get_env("APOLLO_MODE") or "web").strip().lower()
    return m if m in ("web", "api") else "web"


def _web_only() -> bool:
    return _mode() == "web"


def _clamp(n: int, default: int, hi: int = MAX_PER_PAGE) -> int:
    try:
        v = int(n)
    except (TypeError, ValueError):
        return default
    if v <= 0:
        return default
    return min(v, hi)


DEFAULT_TITLES = ["CEO", "CTO", "Founder", "Co-Founder", "VP Engineering"]
SENIORITIES = ["owner", "founder", "c_suite", "partner", "vp", "head", "director",
               "manager", "senior", "entry", "intern"]
TITLE_PRESETS = {
    "founders": ["Founder", "Co-Founder", "CEO", "Owner"],
    "engineering": ["CTO", "VP Engineering", "Head of Engineering", "Engineering Manager",
                    "Lead Engineer", "Director of Engineering"],
    "product": ["CPO", "VP Product", "Head of Product", "Product Manager"],
    "sales": ["CRO", "VP Sales", "Head of Sales", "Sales Director", "Account Executive"],
    "marketing": ["CMO", "VP Marketing", "Head of Marketing", "Growth Lead"],
    "decision_makers": ["CEO", "CTO", "CFO", "COO", "Founder", "VP", "Head"],
}

_PEOPLE_ROW_SELECTORS = (
    "[role='row']",
    "table tbody tr",
    "[data-cy='person-row']",
    "[data-testid='person-row']",
    "div[class*='PersonRow']",
)
_NAME_SELECTORS = (
    "a[href*='linkedin.com/in/']",
    "[data-cy='person-name']",
    "span[class*='name']",
    "a[class*='person-name']",
    "[class*='zp_Y6y8d']",
)
_TITLE_SELECTORS = (
    "[data-cy='person-title']",
    "span[class*='title']",
    "div[class*='title']",
    "[class*='zp_FEm_X']",
)

_APOLLO_LOGIN_LABELS = ("Log in", "Sign in", "Login")
_APOLLO_GOOGLE_LABELS = (
    "Log In with Google",
    "Sign in with Google",
    "Continue with Google",
    "Google",
)
_GOOGLE_EMAIL_SELECTORS = ("#identifierId", "input[type=email]", "input[name=identifier]")
_GOOGLE_PASS_SELECTORS = ("input[name=Passwd]", "input[type=password]", "input[name=Passwd]")
_GOOGLE_NEXT_LABELS = ("Next", "Sign in", "Continue")
_GOOGLE_SUBMIT_SELECTORS = ("#passwordNext", "#identifierNext", "button[type=submit]")


def _is_internal_host(host: str) -> bool:
    h = (host or "").strip().lower().rstrip(".")
    if not h or h in ("localhost",) or h.endswith(".localhost") or h.endswith(".local"):
        return True
    if h.endswith(".internal"):
        return True
    try:
        ip = ipaddress.ip_address(h)
        return (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)
    except ValueError:
        return False


def _safe_company_url(domain: str) -> tuple[str | None, str | None]:
    domain = (domain or "").strip().lstrip("@").lower()
    if not domain:
        return None, "domain is required"
    host = domain.split("/", 1)[0]
    if _is_internal_host(host):
        return None, "refusing internal/private host"
    return f"https://{host}", None


def _headers(key: str) -> dict:
    return {"X-Api-Key": key, "Content-Type": "application/json",
            "Cache-Control": "no-cache", "Accept": "application/json"}


def _api(method: str, path: str, key: str, *, json_body: dict | None = None,
         params: dict | None = None, timeout: float = 25) -> tuple[dict | None, dict | None]:
    r = http.request(method, f"{BASE}{path}", headers=_headers(key), json_body=json_body,
                     params=params, timeout=timeout)
    if not r.get("ok"):
        return None, {"error": r.get("error") or f"HTTP {r.get('status')}"}
    if "json" not in r:
        return None, {"error": "non-JSON response"}
    return r["json"], None


def _person_row(p: dict) -> dict:
    org = p.get("organization") or {}
    return {
        "name": p.get("name"),
        "title": p.get("title"),
        "seniority": p.get("seniority"),
        "linkedin_url": p.get("linkedin_url"),
        "github": (p.get("github_url") or "").rsplit("/", 1)[-1] if p.get("github_url") else "",
        "location": ", ".join(x for x in (p.get("city"), p.get("state"), p.get("country")) if x),
        "organization": org.get("name"),
        "domain": org.get("primary_domain") or org.get("website_url"),
    }


def _web_person_row(name: str, title: str = "", linkedin_url: str = "", domain: str = "") -> dict:
    return {
        "name": name,
        "title": title or None,
        "seniority": None,
        "linkedin_url": linkedin_url or None,
        "github": "",
        "location": "",
        "organization": None,
        "domain": domain or None,
    }


def _company_keyword_from_domain(domain: str) -> str:
    """Best-effort company keyword for Apollo people search (e.g. cometapi.com -> CometAPI)."""
    base = (domain or "").split(".")[0].lower()
    if not base:
        return domain
    if base.endswith("api") and len(base) > 3:
        return base[:-3].title() + "API"
    return base.title()


def _apollo_people_urls(domain: str, titles: list[str] | None, limit: int) -> list[str]:
    """Candidate Apollo hash URLs for people search filtered by domain."""
    titles = titles or DEFAULT_TITLES
    lim = _clamp(limit, 5, 25)
    base_params: list[str] = [
        "sortByField=recommendations_score",
        "sortAscending=false",
        f"perPage={lim}",
    ]
    for t in titles[:6]:
        base_params.append(f"personTitles[]={quote(t)}")
    urls: list[str] = []
    company_kw = _company_keyword_from_domain(domain)
    # Strategy 0: company keyword (often works when domain filter returns 0)
    if company_kw:
        p0 = base_params + [f"qOrganizationKeywordTags[]={quote(company_kw)}"]
        urls.append(f"{APOLLO_PEOPLE}?{'&'.join(p0)}")
    # Strategy 1: organization keyword tag (domain)
    p1 = base_params + [f"qOrganizationKeywordTags[]={quote(domain)}"]
    urls.append(f"{APOLLO_PEOPLE}?{'&'.join(p1)}")
    # Strategy 2: organization domains list style param
    p2 = base_params + [f"qOrganizationDomains[]={quote(domain)}"]
    urls.append(f"{APOLLO_PEOPLE}?{'&'.join(p2)}")
    # Strategy 3: plain people page (UI interaction fallback)
    urls.append(APOLLO_PEOPLE)
    return urls


def _playwright_available() -> tuple[bool, str | None]:
    return _pw_available_shared()


def _cdp_alive(url: str | None = None) -> bool:
    return _cdp_alive_shared(url or _cdp_url())


def _find_system_browser() -> Path | None:
    """Locate Chrome or Edge on Windows/macOS/Linux."""
    roots = [
        os.environ.get("ProgramFiles", ""),
        os.environ.get("ProgramFiles(x86)", ""),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    names = (
        ("Google/Chrome/Application/chrome.exe", "chrome"),
        ("Microsoft/Edge/Application/msedge.exe", "msedge"),
        ("Chromium/Application/chrome.exe", "chromium"),
    )
    for root in roots:
        if not root:
            continue
        for rel, _ in names:
            p = Path(root) / rel
            if p.is_file():
                return p
    if sys.platform == "darwin":
        for p in (Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                  Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")):
            if p.is_file():
                return p
    for cmd in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
        try:
            r = subprocess.run(["which", cmd], capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                return Path(r.stdout.strip())
        except Exception:
            pass
    return None


def _launch_background_browser(url: str = APOLLO_APP) -> dict:
    """Start Chrome/Edge minimized with CDP — stays in background, not Playwright Chromium."""
    if _cdp_alive():
        return {"ok": True, "already_running": True, "cdp_url": _cdp_url(),
                "hint": "Browser already listening. Log in to Apollo if needed, then find_people()."}
    exe = _find_system_browser()
    if not exe:
        return {"error": "Chrome or Edge not found",
                "hint": "Install Google Chrome or Microsoft Edge, then retry start_background_browser()."}
    user_data = _browser_user_data()
    user_data.mkdir(parents=True, exist_ok=True)
    port = _cdp_port()
    args = [
        str(exe),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--disable-features=TranslateUI",
        "--disable-restore-session-state",
        url,
    ]
    try:
        kwargs: dict = {}
        if sys.platform == "win32":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 6  # SW_MINIMIZE — background, not foreground
            kwargs["startupinfo"] = si
        subprocess.Popen(args, **kwargs)  # noqa: S603
    except Exception as ex:  # noqa: BLE001
        return {"error": str(ex), "hint": "Could not start browser process."}
    for _ in range(20):
        time.sleep(0.5)
        if _cdp_alive():
            return {
                "ok": True,
                "cdp_url": _cdp_url(),
                "browser": exe.name,
                "user_data_dir": str(user_data),
                "hint": "Browser started minimized in apollo profile. Log in there (not daily Chrome), then ensure_ready().",
            }
    return {"ok": False, "cdp_url": _cdp_url(),
            "hint": "Browser started but CDP not ready yet — wait a few seconds and call check_session()."}


def _click_label(page, labels: tuple[str, ...], timeout: float = 5000) -> bool:
    for label in labels:
        try:
            btn = page.get_by_role("button", name=re.compile(re.escape(label), re.I)).first
            if btn.is_visible(timeout=timeout):
                btn.click(timeout=timeout)
                return True
        except Exception:
            pass
        try:
            link = page.get_by_role("link", name=re.compile(re.escape(label), re.I)).first
            if link.is_visible(timeout=2000):
                link.click(timeout=timeout)
                return True
        except Exception:
            pass
        try:
            txt = page.get_by_text(re.compile(label, re.I)).first
            if txt.is_visible(timeout=2000):
                txt.click(timeout=timeout)
                return True
        except Exception:
            pass
    return False


def _fill_first(page, selectors: tuple[str, ...], value: str, timeout: float = 8000) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=2000):
                loc.fill(value, timeout=timeout)
                return True
        except Exception:
            continue
    return False


def _type_human(page, selectors: tuple[str, ...], value: str, delay_ms: int = 60) -> bool:
    """Type like a human — Google rejects plain fill() on password fields."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=3000):
                loc.click(timeout=3000)
                loc.fill("", timeout=3000)
                loc.press_sequentially(value, delay=delay_ms)
                return True
        except Exception:
            continue
    return False


def _click_submit(page, timeout: float = 5000) -> bool:
    for sel in _GOOGLE_SUBMIT_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=2000):
                loc.click(timeout=timeout)
                return True
        except Exception:
            continue
    return _click_label(page, _GOOGLE_NEXT_LABELS, timeout=timeout)


def _classify_google_error(page) -> tuple[str | None, str]:
    """Return (error_code, hint) parsed from Google login page body."""
    try:
        body = (page.inner_text("body") or "").lower()
    except Exception:
        return None, _profile_login_hint()
    if "wrong password" in body:
        return "wrong_password", (
            "Google returned 'Wrong password'. Verify APOLLO_GOOGLE_PASSWORD in .env "
            f"or log in manually once. {_profile_login_hint()}"
        )
    if "captcha" in body or "recaptcha" in body:
        return "captcha", f"Google CAPTCHA — complete login manually. {_profile_login_hint()}"
    if "verify it's you" in body or "verify it�s you" in body or "2-step" in body:
        return "verify_identity", f"Google verification required — log in manually. {_profile_login_hint()}"
    if "couldn't sign you in" in body or "couldn�t sign you in" in body:
        return "account_blocked", f"Google blocked sign-in — try manual login. {_profile_login_hint()}"
    return None, _profile_login_hint()


def _restore_browser_window() -> None:
    """Restore minimized apollo browser on Windows so user can log in manually."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        found: list[int] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
        def _enum(hwnd, _lparam):
            if user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value.lower()
            if "apollo" in title or "chrome" in title or "edge" in title:
                found.append(hwnd)
            return True

        user32.EnumWindows(_enum, 0)
        for hwnd in found[:3]:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    except Exception:
        pass


def _click_google_sso(page) -> bool:
    """Click Apollo's Google SSO button — never Apple/Microsoft."""
    for label in _APOLLO_GOOGLE_LABELS:
        try:
            btn = page.get_by_role("button", name=re.compile(re.escape(label), re.I)).first
            if btn.is_visible(timeout=3000):
                btn.click(timeout=5000)
                return True
        except Exception:
            pass
    return _click_label(page, _APOLLO_GOOGLE_LABELS)


def _auto_login_google_on_page(page, email: str, password: str) -> dict:
    """Run Google SSO on an open Playwright page (CDP background tab)."""
    page.goto(f"{APOLLO_APP}/#/login", wait_until="domcontentloaded", timeout=WEB_TIMEOUT_MS)
    try:
        page.wait_for_load_state("networkidle", timeout=12000)
    except Exception:
        pass
    time.sleep(1.5)
    if not _is_login_page(page.url, _page_html(page)):
        return {"logged_in": True, "url": page.url, "skipped": "already_logged_in"}

    if not _click_google_sso(page):
        return {
            "logged_in": False,
            "url": page.url,
            "error": "google button not found",
            "hint": f"Could not find 'Log In with Google' on Apollo login page. {_profile_login_hint()}",
        }
    time.sleep(2.5)

    deadline = time.time() + 90
    pwd_attempted = False
    while time.time() < deadline:
        url = page.url.lower()
        if "appleid.apple.com" in url:
            page.goto(f"{APOLLO_APP}/#/login", wait_until="domcontentloaded", timeout=WEB_TIMEOUT_MS)
            time.sleep(1.5)
            _click_google_sso(page)
            time.sleep(2)
            continue
        if "accounts.google.com" in url or "google.com" in url:
            err_code, err_hint = _classify_google_error(page)
            if err_code:
                return {
                    "logged_in": False,
                    "url": page.url,
                    "error": "google login did not complete",
                    "error_code": err_code,
                    "hint": err_hint,
                }
            try:
                acct = page.get_by_text(re.compile(re.escape(email), re.I)).first
                if acct.is_visible(timeout=2000):
                    acct.click(timeout=3000)
                    time.sleep(2)
                    continue
            except Exception:
                pass
            on_pwd = "challenge/pwd" in url
            if not on_pwd:
                try:
                    on_pwd = page.locator("input[name=Passwd]").first.is_visible(timeout=500)
                except Exception:
                    on_pwd = False
            if on_pwd:
                try:
                    page.wait_for_selector("input[name=Passwd]", state="visible", timeout=8000)
                except Exception:
                    pass
                loc = page.locator("input[name=Passwd]").first
                try:
                    if loc.is_visible(timeout=2000):
                        loc.click(timeout=3000)
                        loc.fill("", timeout=3000)
                        page.keyboard.type(password, delay=80)
                        pwd_attempted = True
                        _click_submit(page)
                        time.sleep(6)
                        try:
                            page.wait_for_url(
                                re.compile(r"apollo\.io|google\.com/signin/oauth"), timeout=20000)
                        except Exception:
                            pass
                except Exception:
                    pass
                continue
            if _type_human(page, _GOOGLE_EMAIL_SELECTORS, email, delay_ms=40) or _fill_first(
                    page, _GOOGLE_EMAIL_SELECTORS, email):
                _click_submit(page)
                time.sleep(2)
                continue
        if "apollo.io" in url and not _is_login_page(page.url, _page_html(page)):
            return {"logged_in": True, "url": page.url}
        time.sleep(1)

    logged_in = "apollo.io" in page.url.lower() and not _is_login_page(page.url, _page_html(page))
    if logged_in:
        return {"logged_in": True, "url": page.url}
    err_code, err_hint = _classify_google_error(page)
    if not err_code and pwd_attempted:
        err_code = "password_step_timeout"
        err_hint = (
            "Google password step did not complete — log in manually in the apollo profile browser. "
            + _profile_login_hint()
        )
    return {
        "logged_in": False,
        "url": page.url,
        "error": "google login did not complete",
        "error_code": err_code,
        "hint": err_hint or _profile_login_hint(),
    }


def _auto_login_google_sync() -> dict:
    email, password = _google_creds()
    if not email:
        return {"logged_in": False, "error": "missing credentials", "hint": _missing_creds_hint()}
    if not _cdp_alive():
        launched = _launch_background_browser(APOLLO_APP)
        if not launched.get("ok") and launched.get("error"):
            return {"logged_in": False, **launched}

    def work() -> dict:
        def run(page, backend: str) -> dict:
            out = _auto_login_google_on_page(page, email, password)
            out["backend"] = backend
            out["password_len"] = len(password)
            return out
        return with_cdp_page(
            run, cdp=_cdp_url(), headless_profile_dir=str(PROFILE_DIR), prefer_fresh_tab=True)

    return _run_playwright(work, 180)


def _close_duplicate_apollo_tabs(keep_page) -> int:
    """Close extra Apollo login tabs so manual login stays on one page."""
    closed = 0
    try:
        for p in list(keep_page.context.pages):
            if p is keep_page:
                continue
            url = (p.url or "").lower()
            if "apollo.io" not in url:
                continue
            if "/login" in url or "#/login" in url:
                try:
                    p.close()
                    closed += 1
                except Exception:
                    pass
    except Exception:
        pass
    return closed


def _prep_manual_login_sync() -> dict:
    """Open/reuse Apollo tab, click Google SSO once, then hand off to the user."""
    if not _cdp_alive():
        boot = _launch_background_browser(f"{APOLLO_APP}/#/login")
        if boot.get("error"):
            return {"ok": False, **boot}
        _restore_browser_window()
        return {
            "ok": True,
            "clicked_google": False,
            "hint": "Browser started — click 'Log In with Google' if needed, then sign in manually.",
        }

    def work(page, backend: str) -> dict:
        page.goto(f"{APOLLO_APP}/#/login", wait_until="domcontentloaded", timeout=WEB_TIMEOUT_MS)
        try:
            page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass
        time.sleep(1.5)
        html = _page_html(page)
        if not _is_login_page(page.url, html):
            return {
                "ok": True,
                "clicked_google": False,
                "already_logged_in": True,
                "url": page.url,
                "backend": backend,
            }
        clicked = _click_google_sso(page)
        return {
            "ok": True,
            "clicked_google": clicked,
            "url": page.url,
            "backend": backend,
            "hint": (
                "Complete Google sign-in in the restored browser window — "
                "type your email and password yourself."
            ),
        }

    try:
        out = _with_background_page(work, reuse_tab=True, prefer_fresh_tab=False)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "hint": _profile_login_hint()}
    _restore_browser_window()
    return out


def _wait_for_manual_login_sync(wait_seconds: int | None = None) -> dict:
    """Poll check_session until logged in or timeout. Restores browser window once."""
    if wait_seconds is None:
        wait_seconds = get_env_int("APOLLO_MANUAL_WAIT_SECONDS", 300) or 300
    wait_seconds = max(0, min(int(wait_seconds), 900))
    if not _cdp_alive():
        boot = _launch_background_browser(APOLLO_APP)
        if boot.get("error"):
            return {"logged_in": False, **boot}
    if wait_seconds <= 0:
        session = _session_check_sync()
        return {
            "logged_in": bool(session.get("logged_in")),
            "waited_seconds": 0,
            "url": session.get("url"),
            "user_data_dir": session.get("user_data_dir"),
            "hint": _profile_login_hint(),
            "error": None if session.get("logged_in") else "manual login timeout",
        }
    _restore_browser_window()
    deadline = time.time() + wait_seconds
    waited = 0.0
    while time.time() < deadline:
        session = _session_check_sync()
        if session.get("logged_in"):
            return {
                "logged_in": True,
                "waited_seconds": round(waited, 1),
                "url": session.get("url"),
                "user_data_dir": session.get("user_data_dir"),
                "hint": "Manual login detected in apollo profile browser.",
            }
        time.sleep(3)
        waited += 3
    return {
        "logged_in": False,
        "waited_seconds": round(waited, 1),
        "hint": _profile_login_hint(),
        "error": "manual login timeout",
    }


def _ensure_ready_sync() -> dict:
    steps: list[dict] = []
    mode = _login_mode()
    if not _cdp_alive():
        boot = _launch_background_browser(APOLLO_APP)
        steps.append({"step": "start_background_browser", **boot})
        if boot.get("error"):
            return {"ready": False, "logged_in": False, "steps": steps, **boot}

    session = _session_check_sync()
    steps.append({"step": "check_session", "logged_in": session.get("logged_in")})
    if session.get("logged_in"):
        return {
            "ready": True, "logged_in": True, "cdp_alive": _cdp_alive(), "login_mode": mode,
            "steps": steps,
            "manual_steps": ["Click Apollo FAB once on LinkedIn tab per browser session"],
            "automated": ["CDP start", "profile navigation", "Access email click", "scrape"],
        }

    if mode == "manual":
        prep = _prep_manual_login_sync()
        steps.append({"step": "prep_manual_login", **prep})
        if prep.get("already_logged_in"):
            return {"ready": True, "logged_in": True, "cdp_alive": _cdp_alive(), "login_mode": mode, "steps": steps}
        if prep.get("error") and not prep.get("ok"):
            return {
                "ready": False, "logged_in": False, "cdp_alive": _cdp_alive(), "login_mode": mode,
                "steps": steps, "error": prep.get("error"), "hint": prep.get("hint"),
            }
        manual = _wait_for_manual_login_sync()
        steps.append({"step": "wait_for_manual_login", **manual})
        if manual.get("logged_in"):
            return {"ready": True, "logged_in": True, "cdp_alive": _cdp_alive(), "login_mode": mode, "steps": steps}
        return {
            "ready": False, "logged_in": False, "cdp_alive": _cdp_alive(), "login_mode": mode,
            "steps": steps, "error": manual.get("error", "not logged in"), "hint": manual.get("hint"),
        }

    if mode in ("auto", "hybrid") and _auto_login_enabled():
        login = _auto_login_google_sync()
        steps.append({"step": "auto_login_google", **{k: login.get(k) for k in login if k != "steps"}})
        if login.get("logged_in"):
            return {"ready": True, "logged_in": True, "cdp_alive": _cdp_alive(), "login_mode": mode, "steps": steps}
        if mode == "hybrid":
            manual = _wait_for_manual_login_sync()
            steps.append({"step": "wait_for_manual_login", **manual})
            if manual.get("logged_in"):
                return {"ready": True, "logged_in": True, "cdp_alive": _cdp_alive(), "login_mode": mode, "steps": steps}
            return {
                "ready": False, "logged_in": False, "cdp_alive": _cdp_alive(), "login_mode": mode,
                "steps": steps,
                "error": login.get("error", "login failed"),
                "error_code": login.get("error_code"),
                "hint": manual.get("hint") or login.get("hint", _profile_login_hint()),
            }
        return {
            "ready": False, "logged_in": False, "cdp_alive": _cdp_alive(), "login_mode": mode,
            "steps": steps,
            "error": login.get("error", "login failed"),
            "error_code": login.get("error_code"),
            "hint": login.get("hint", _missing_creds_hint()),
        }

    return {
        "ready": False,
        "logged_in": False,
        "cdp_alive": _cdp_alive(),
        "login_mode": mode,
        "steps": steps,
        "hint": session.get("hint") or _profile_login_hint(),
    }


def _with_background_page(work, *, reuse_tab: bool = True, prefer_fresh_tab: bool = False):
    """Acquire a CDP background tab; headless Playwright only if APOLLO_ALLOW_HEADLESS_FALLBACK=1."""
    def wrapped(page, backend: str):
        closed = 0
        if reuse_tab and not prefer_fresh_tab:
            closed = _close_duplicate_apollo_tabs(page)
        out = work(page, backend)
        if isinstance(out, dict) and closed:
            out.setdefault("closed_duplicate_tabs", closed)
        return out

    return with_cdp_page(
        wrapped,
        cdp=_cdp_url(),
        headless_profile_dir=str(PROFILE_DIR) if allow_headless_fallback() else None,
        reuse_tab=reuse_tab,
        url_hint="apollo.io",
        prefer_fresh_tab=prefer_fresh_tab,
    )


def _with_linkedin_extension_page(work):
    """Persistent LinkedIn tab for Apollo extension side-panel (never closed)."""
    ready = _ensure_ready_sync()
    if not ready.get("ready"):
        return {
            "error": ready.get("error", "apollo not ready"),
            "hint": ready.get("hint", _missing_creds_hint()),
            "ensure_ready": ready,
        }
    return with_cdp_page(
        work,
        cdp=_cdp_url(),
        reuse_tab=True,
        url_hint="linkedin.com",
        prefer_fresh_tab=False,
        close_on_done=False,
    )


def _extract_people_js() -> str:
    row_sels = list(_PEOPLE_ROW_SELECTORS)
    name_sels = list(_NAME_SELECTORS)
    title_sels = list(_TITLE_SELECTORS)
    return f"""() => {{
  const rows = [];
  const seen = new Set();
  const rowSels = {row_sels!r};
  const nameSels = {name_sels!r};
  const titleSels = {title_sels!r};
  let containers = [];
  for (const sel of rowSels) {{
    const found = [...document.querySelectorAll(sel)];
    if (found.length >= 2) {{ containers = found; break; }}
  }}
  if (!containers.length) {{
    containers = [...document.querySelectorAll('a[href*="linkedin.com/in/"]')]
      .map(a => a.closest('tr') || a.closest('[role="row"]') || a.closest('div[class*="zp_"]') || a.parentElement);
  }}
  for (const row of containers) {{
    if (!row) continue;
    let name = '', title = '', linkedin = '';
    for (const sel of nameSels) {{
      const el = row.querySelector(sel);
      if (el) {{
        name = (el.textContent || '').trim();
        if (el.href && el.href.includes('linkedin.com/in/')) linkedin = el.href;
        if (name && name.length > 1) break;
      }}
    }}
    for (const sel of titleSels) {{
      const el = row.querySelector(sel);
      if (el) {{ title = (el.textContent || '').trim(); if (title) break; }}
    }}
    if (!linkedin) {{
      const li = row.querySelector('a[href*="linkedin.com/in/"]');
      if (li) {{ linkedin = li.href; if (!name) name = (li.textContent || '').trim(); }}
    }}
    if (!name) {{
      const parts = (row.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
      const skip = /^(access|qualify|actions|links|save|select|search|filter|export|net new|saved|total|add column|name|job title|company|emails|phone)/i;
      for (const p of parts) {{
        if (p.length < 2 || p.length > 80 || skip.test(p)) continue;
        if (!name) {{ name = p; continue; }}
        if (!title && p.length < 60) {{ title = p; break; }}
      }}
    }}
    if (!name || name.length < 2 || name.length > 80) continue;
    if (/^(search|filter|export|save|select|people auto-score|add column)$/i.test(name)) continue;
    const key = name + '|' + (title || '');
    if (seen.has(key)) continue;
    seen.add(key);
    rows.push({{name, title, linkedin_url: linkedin}});
  }}
  if (!rows.length) {{
    for (const li of document.querySelectorAll('a[href*="linkedin.com/in/"]')) {{
      const row = li.closest('[role="row"]') || li.closest('tr') || li.closest('div[class*="zp_"]');
      if (!row) continue;
      let name = '', title = '', linkedin = li.href || '';
      const parts = (row.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
      const skip = /^(access|qualify|actions|links|save|select|search|filter|export|net new|saved|total|add column|name|job title|company|emails|phone)/i;
      for (const p of parts) {{
        if (p.length < 2 || p.length > 80 || skip.test(p)) continue;
        if (!name) {{ name = p; continue; }}
        if (!title && p.length < 60) {{ title = p; break; }}
      }}
      if (!name || name.length < 2) continue;
      const key = name + '|' + (title || '');
      if (seen.has(key)) continue;
      seen.add(key);
      rows.push({{name, title, linkedin_url: linkedin}});
    }}
  }}
  return rows;
}}"""


def _page_html(page) -> str:
    try:
        return page.content()
    except Exception:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=8000)
            return page.content()
        except Exception:
            return ""


def _is_login_page(url: str, html: str) -> bool:
    u = (url or "").lower()
    h = (html or "").lower()
    if any(x in u for x in ("/login", "/sign-in", "/sign_up", "/signup", "/register")):
        return True
    if 'type="password"' in h and any(x in h for x in ("log in", "sign in", "apollo", "password")):
        return True
    # Logged-in app shell markers
    if any(x in u for x in ("#/people", "#/home", "#/onboarding", "#/companies", "#/sequences")):
        if any(x in h for x in ("people", "finder", "prospect", "data-cy", "sidebar", "navigation")):
            return False
    if "app.apollo.io" in u and "#/login" not in u:
        if any(x in h for x in ("data-cy=", "zp_", "finder", "prospect", "people search")):
            return False
    if u.rstrip("/").endswith("apollo.io/#") or u.rstrip("/").endswith("apollo.io"):
        if any(x in h for x in ("log in", "sign in", "sign up", "create account")):
            return True
    return False


def _ui_search_domain(page, domain: str) -> None:
    """Try to apply company-domain filter via Apollo UI when URL params don't stick."""
    selectors = [
        "input[placeholder*='domain' i]",
        "input[placeholder*='company' i]",
        "input[aria-label*='company' i]",
        "input[aria-label*='domain' i]",
        "[data-cy='company-filter'] input",
        "input[type='search']",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=2000):
                loc.click(timeout=3000)
                loc.fill(domain, timeout=3000)
                page.keyboard.press("Enter")
                time.sleep(2)
                return
        except Exception:
            continue
    # Omnibox / global search fallback
    try:
        box = page.get_by_placeholder(re.compile("search", re.I)).first
        if box.is_visible(timeout=2000):
            box.fill(f"{domain} CEO CTO", timeout=3000)
            page.keyboard.press("Enter")
            time.sleep(2)
    except Exception:
        pass


_ACCESS_EMAIL_LABELS = ("Access email", "View email", "Unlock email")
_TOS_COMPLY_LABELS = ("I will comply",)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

_CLOSE_MODALS_JS = """() => {
  let closed = 0;
  const isVisible = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  for (const el of document.querySelectorAll('button, [role="button"], svg, span')) {
    const aria = (el.getAttribute && el.getAttribute('aria-label')) || '';
    const txt = (el.textContent || '').trim();
    if (!isVisible(el)) continue;
    if (/close/i.test(aria) || txt === '\u00d7' || txt === 'x' || txt === 'X') {
      try { el.click(); closed++; } catch (e) { /* ignore */ }
    }
  }
  return closed;
}"""

_DISMISS_ERRORS_JS = """() => {
  let closed = 0;
  const isVisible = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  for (const el of document.querySelectorAll('button, [role="button"]')) {
    const txt = (el.textContent || '').trim();
    if (!isVisible(el)) continue;
    if (/^(ok|got it|dismiss|close)$/i.test(txt)) {
      try { el.click(); closed++; } catch (e) { /* ignore */ }
    }
  }
  return closed;
}"""

_LOOKALIKES_BLOCKED_HINT = (
    "Apollo free plan blocks LinkedIn URL / people-lookalikes search. "
    "Automation uses name search + MIT email fallback instead."
)


def _dismiss_apollo_errors(page) -> int:
    """Dismiss Error dialogs (OK/Got it) and close buttons. Never raises."""
    closed = _close_apollo_modals(page)
    try:
        closed += int(page.evaluate(_DISMISS_ERRORS_JS) or 0)
    except Exception:
        pass
    return closed


def _apollo_lookalikes_blocked(page) -> bool:
    """True when Apollo shows a lookalikes error modal (not sidebar filter labels)."""
    try:
        body = page.inner_text("body")[:4000].lower()
        return bool(re.search(
            r"cannot access people lookalike|people lookalikes is a paid|"
            r"lookalikes is not available|people-lookalikes is not",
            body,
        ))
    except Exception:
        return False


def _recover_from_lookalikes(page) -> None:
    """Dismiss lookalikes error and return to plain people search. Never raises."""
    _dismiss_apollo_errors(page)
    try:
        page.goto(f"{APOLLO_APP}/#/people", wait_until="domcontentloaded", timeout=WEB_TIMEOUT_MS)
        time.sleep(1)
    except Exception:
        pass
    _dismiss_apollo_errors(page)


def _goto_clean_people_page(page) -> None:
    """Navigate to plain people search, clearing lookalikes trap. Never raises."""
    try:
        page.evaluate("""() => {
          try { localStorage.removeItem('recentOmnisearchData'); } catch (e) {}
        }""")
        if _apollo_lookalikes_blocked(page):
            _recover_from_lookalikes(page)
        page.goto(
            f"{APOLLO_APP}/#/people?page=1&perPage=25",
            wait_until="domcontentloaded", timeout=WEB_TIMEOUT_MS,
        )
        time.sleep(2)
        _dismiss_apollo_errors(page)
        if "recommendationconfigid" in (page.url or "").lower():
            for label in ("Clear all", "Clear filters", "Remove all"):
                try:
                    page.get_by_text(label, exact=False).first.click(timeout=2000)
                    time.sleep(1)
                    break
                except Exception:
                    pass
            _dismiss_apollo_errors(page)
            if "recommendationconfigid" in (page.url or "").lower():
                page.goto(
                    f"{APOLLO_APP}/#/people?page=1&perPage=25",
                    wait_until="domcontentloaded", timeout=WEB_TIMEOUT_MS,
                )
                time.sleep(2)
                _dismiss_apollo_errors(page)
    except Exception:
        pass


def _fill_people_search_box(page, search_text: str) -> bool:
    """Clear and fill Apollo people quick-search. Returns False on failure."""
    try:
        box = page.get_by_placeholder(re.compile("search", re.I)).first
        try:
            box.click(timeout=3000)
        except Exception:
            _dismiss_apollo_errors(page)
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            time.sleep(0.5)
            box.click(timeout=3000, force=True)
        box.fill("", timeout=3000)
        time.sleep(0.3)
        box.fill(search_text, timeout=3000)
        return True
    except Exception:
        return False


def _close_apollo_modals(page) -> int:
    """Close promo/free-trial dialogs blocking interaction (best effort, never raises)."""
    try:
        return int(page.evaluate(_CLOSE_MODALS_JS) or 0)
    except Exception:
        return 0


def _accept_tos_if_present(page) -> bool:
    """Apollo shows a one-time 'Agree to Terms of Service' dialog before first email reveal."""
    return _click_label(page, _TOS_COMPLY_LABELS, timeout=2000)


def _find_row_by_name(page, person_name: str):
    """Locate the [role=row] element whose text includes person_name."""
    try:
        rows = page.locator("[role='row']")
        count = rows.count()
        for i in range(count):
            row = rows.nth(i)
            try:
                text = row.inner_text(timeout=1000)
            except Exception:
                continue
            if person_name.lower() in text.lower():
                return row
    except Exception:
        pass
    return None


def _reveal_email_for_row_sync(page, person_name: str) -> dict:
    """Click Apollo's 'Access email' for one row (spends 1 credit). Never re-clicks if already
    revealed or already known-absent. Returns {email, no_email_on_file, clicked, row_text}."""
    _close_apollo_modals(page)
    row = _find_row_by_name(page, person_name)
    if row is None:
        return {"email": None, "no_email_on_file": False, "clicked": False,
                "error": "row not found", "hint": "Person not visible in current Apollo search results."}

    try:
        row_text_before = row.inner_text(timeout=2000)
    except Exception:
        row_text_before = ""

    existing = _EMAIL_RE.search(row_text_before)
    if existing:
        return {"email": existing.group(0), "no_email_on_file": False, "clicked": False,
                "row_text": row_text_before[:200]}
    if re.search(r"\bno email\b", row_text_before, re.I):
        return {"email": None, "no_email_on_file": True, "clicked": False,
                "row_text": row_text_before[:200]}

    clicked = False
    try:
        btn = row.get_by_role("button", name=re.compile("|".join(re.escape(l) for l in _ACCESS_EMAIL_LABELS), re.I)).first
        if btn.is_visible(timeout=2000):
            btn.click(timeout=5000)
            clicked = True
    except Exception:
        pass
    if not clicked:
        return {"email": None, "no_email_on_file": False, "clicked": False,
                "error": "access email button not found", "row_text": row_text_before[:200]}

    time.sleep(2)
    _close_apollo_modals(page)
    if _accept_tos_if_present(page):
        time.sleep(1.5)
        _close_apollo_modals(page)
        # First click after ToS may only have dismissed the dialog — try once more.
        row = _find_row_by_name(page, person_name) or row
        try:
            btn = row.get_by_role("button", name=re.compile("|".join(re.escape(l) for l in _ACCESS_EMAIL_LABELS), re.I)).first
            if btn.is_visible(timeout=2000):
                btn.click(timeout=5000)
        except Exception:
            pass

    time.sleep(3)
    row = _find_row_by_name(page, person_name) or row
    try:
        row_text_after = row.inner_text(timeout=3000)
    except Exception:
        row_text_after = ""

    revealed = _EMAIL_RE.search(row_text_after)
    if revealed:
        return {"email": revealed.group(0), "no_email_on_file": False, "clicked": True,
                "row_text": row_text_after[:200]}
    if re.search(r"\bno email\b", row_text_after, re.I):
        return {"email": None, "no_email_on_file": True, "clicked": True,
                "row_text": row_text_after[:200]}
    return {"email": None, "no_email_on_file": False, "clicked": True,
            "error": "email not revealed after click", "row_text": row_text_after[:200]}


def _reveal_apollo_email_sync(domain: str, person_name: str, titles: list[str] | None = None) -> dict:
    """Navigate Apollo people search for domain, click 'Access email' for person_name ONCE.
    Spends at most 1 Apollo credit per call — never bulk-reveals a whole result list."""
    ok_pw, pw_hint = _playwright_available()
    if not ok_pw:
        return {"error": "playwright is not installed", "hint": pw_hint}

    def work() -> dict:
        def run(page, backend: str) -> dict:
            for apollo_url in _apollo_people_urls(domain, titles, 25):
                page.goto(apollo_url, wait_until="domcontentloaded", timeout=WEB_TIMEOUT_MS)
                try:
                    page.wait_for_load_state("networkidle", timeout=12000)
                except Exception:
                    pass
                time.sleep(2)
                if _is_login_page(page.url, _page_html(page)):
                    return {"error": "apollo not logged in", "hint": _profile_login_hint(), "backend": backend}
                if _find_row_by_name(page, person_name) is not None:
                    result = _reveal_email_for_row_sync(page, person_name)
                    result["backend"] = backend
                    result["apollo_url"] = page.url
                    return result
            return {"email": None, "no_email_on_file": False, "clicked": False,
                    "error": "person not found in Apollo search results", "backend": backend}

        return _with_background_page(run, reuse_tab=True)

    return _run_playwright(work, (WEB_TIMEOUT_MS / 1000) + 45)


_PERSON_RESULT_JS = """(args) => {
  const nameL = args[0], companyL = args[1], slugL = (args[2] || '').toLowerCase();
  const requireCompany = args[3] !== false;
  const nameParts = nameL.split(' ').filter(p => p.length > 1);
  const bad = /recently searched|filters?\\s*\\u00b7|keywords contain|company keywords|view 60\\+|lookalike|recommendation|free plan|ask assistant/i;
  const nodes = [...document.querySelectorAll('div,span,li,a')];
  let best = null, bestScore = -1;
  for (const el of nodes) {
    const t = (el.textContent || '').trim();
    if (!t || t.length > 120 || t.length < 8) continue;
    if (t.startsWith('"') || t.startsWith('\\u201c')) continue;
    const tl = t.toLowerCase();
    if (bad.test(t)) continue;
    const nameOk = nameParts.length > 0 && nameParts.every(p => tl.includes(p));
    const slugOk = slugL && tl.includes(slugL);
    if (!nameOk && !slugOk) continue;
    if (requireCompany && companyL && !tl.includes(companyL)) {
      const first = companyL.split(' ')[0];
      if (!(first && first.length > 3 && tl.includes(first))) continue;
    }
    let score = (nameOk ? nameParts.length * 10 : 0) + (slugOk ? 40 : 0);
    if (companyL && tl.includes(companyL)) score += 30;
    if (tl.includes('ceo') || tl.includes('founder') || tl.includes('cto')) score += 20;
    if (tl.includes('mit') || tl.includes('research affiliate')) score += 15;
    score += Math.max(0, 120 - t.length);
    if (score <= bestScore) continue;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    bestScore = score;
    best = { x: r.left + r.width / 2, y: r.top + r.height / 2, text: t.slice(0, 150) };
  }
  return best;
}"""


def _normalize_search_name(name: str) -> str:
    """'Dave B. Blundin' -> 'dave blundin' for Apollo quick-search matching."""
    parts = re.sub(r"[.\s]+", " ", (name or "").strip()).split()
    if len(parts) >= 3 and len(parts[1]) == 1:
        parts.pop(1)  # drop middle initial
    return " ".join(parts).lower()


def _profile_matches_hint(page, company_hint: str = "", title_hint: str = "") -> bool:
    """After navigating to a profile, confirm company_hint appears on the page."""
    if not company_hint:
        return True
    try:
        body = page.inner_text("body")[:4000].lower()
    except Exception:
        return False
    hint = company_hint.lower()
    if hint in body:
        return True
    if "liquid" in hint and "liquid" in body:
        return True
    # Also accept first word of multi-word company (e.g. "link" for Link Ventures).
    first = hint.split()[0] if hint.split() else ""
    if len(first) > 3 and first in body:
        return True
    # Dual affiliation: accept MIT/CSAIL when title mentions academic role
    title_l = (title_hint or "").lower()
    academic_markers = ("mit", "csail", "research affiliate", "university", "institute")
    if any(m in title_l for m in academic_markers):
        if "mit" in body or "massachusetts institute" in body or "csail" in body:
            return True
        if len(first) > 3 and first in body:
            return True
    return False


def _search_person_profile_url_sync(
    page, name: str, company_hint: str = "", title_hint: str = "",
) -> dict:
    """Use Apollo's quick search (top search bar) to locate one specific person's profile page.
    Requires landing on an actual /people/<id> profile — never mistakes a saved-search-filter
    suggestion (which also matches company text) for a real navigation."""

    def _try_quick_search(search_text: str, hint: str) -> dict:
        _goto_clean_people_page(page)
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        time.sleep(0.5)
        _dismiss_apollo_errors(page)
        if not _fill_people_search_box(page, search_text):
            return {"ok": False, "error": "quick search box not found"}
        time.sleep(2)
        _dismiss_apollo_errors(page)
        if _apollo_lookalikes_blocked(page):
            _recover_from_lookalikes(page)
            return {
                "ok": False,
                "error": "Apollo people lookalikes filter blocked (free plan)",
                "apollo_status": "lookalikes_blocked",
                "hint": _LOOKALIKES_BLOCKED_HINT,
            }

        try:
            target = page.evaluate(
                _PERSON_RESULT_JS,
                [_normalize_search_name(name), (hint or "").lower(), "", True],
            )
        except Exception as ex:  # noqa: BLE001
            return {"ok": False, "error": f"result lookup failed: {ex}"}
        if not target:
            target = page.evaluate(
                _PERSON_RESULT_JS,
                [_normalize_search_name(name), (hint or "").lower(), "", False],
            )
        if not target:
            return {"ok": False, "error": "no matching Apollo quick-search result for this name"}
        matched = (target.get("text") or "").strip()
        if matched.startswith('"') or matched.startswith('\u201c') or re.search(
            r"filter|lookalike|keywords contain|ask assistant", matched, re.I,
        ):
            return {"ok": False, "error": "quick search matched a filter suggestion, not a person",
                    "matched_text": matched}

        url_before = page.url
        try:
            page.mouse.click(target["x"], target["y"])
        except Exception as ex:  # noqa: BLE001
            return {"ok": False, "error": f"click failed: {ex}", "matched_text": matched}
        time.sleep(3)
        _close_apollo_modals(page)
        if page.url == url_before or not re.search(r"#/(?:people|contacts)/[A-Za-z0-9]+", page.url):
            return {"ok": False, "error": "click did not navigate to a profile page",
                    "url": page.url, "matched_text": matched}
        if hint and not _profile_matches_hint(page, hint, title_hint):
            return {"ok": False, "error": f"profile does not match company hint '{hint}'",
                    "url": page.url, "matched_text": matched, "profile_reached": True}
        return {"ok": True, "url": page.url, "matched_text": matched}

    out = _try_quick_search(_normalize_search_name(name).title(), company_hint)
    if out.get("ok"):
        return out
    if company_hint:
        combined = f"{_normalize_search_name(name).title()} {company_hint.strip()}"
        combined_out = _try_quick_search(combined, company_hint)
        if combined_out.get("ok"):
            return combined_out
        if not out.get("matched_text") and combined_out.get("matched_text"):
            out = combined_out
    return out


def _reveal_email_on_profile_sync(page) -> dict:
    """Click the 'Access email' button inside a person profile page's Contact information block
    (spends 1 credit) — never clicks the 'Similar people' Access email buttons further down."""
    _close_apollo_modals(page)
    try:
        body_before = page.inner_text("body")[:800]
    except Exception:
        body_before = ""
    existing = _EMAIL_RE.search(body_before)
    if existing:
        return {"email": existing.group(0), "no_email_on_file": False, "clicked": False}
    if re.search(r"\bno email\b", body_before, re.I):
        return {"email": None, "no_email_on_file": True, "clicked": False}

    clicked = False
    try:
        btn = page.get_by_text("Contact information").first.locator(
            "xpath=ancestor::*[position()<=6]//button[contains(., 'Access email')]"
        ).first
        if btn.is_visible(timeout=3000):
            btn.click(timeout=5000)
            clicked = True
    except Exception:
        pass
    if not clicked:
        return {"email": None, "no_email_on_file": False, "clicked": False,
                "error": "Access email button not found in Contact information"}

    time.sleep(2)
    _close_apollo_modals(page)
    if _accept_tos_if_present(page):
        time.sleep(1.5)
        _close_apollo_modals(page)

    time.sleep(2.5)
    try:
        body_after = page.inner_text("body")[:800]
    except Exception:
        body_after = ""
    revealed = _EMAIL_RE.search(body_after)
    if revealed:
        return {"email": revealed.group(0), "no_email_on_file": False, "clicked": True}
    if re.search(r"\bno email\b", body_after, re.I):
        return {"email": None, "no_email_on_file": True, "clicked": True}
    return {"email": None, "no_email_on_file": False, "clicked": True,
            "error": "email not revealed after click"}


def _build_person_search_url(company: str = "", title: str = "", limit: int = 25) -> str:
    """Apollo people-search URL with optional company keyword + title filters."""
    params: list[str] = [
        "sortByField=recommendations_score",
        "sortAscending=false",
        f"perPage={_clamp(limit, 5, 25)}",
    ]
    if company:
        params.append(f"qOrganizationKeywordTags[]={quote(company)}")
    if title:
        params.append(f"personTitles[]={quote(title)}")
    return f"{APOLLO_PEOPLE}?{'&'.join(params)}"


def _name_parts_for_match(name: str) -> list[str]:
    return [p for p in _normalize_search_name(name).split() if len(p) > 1]


def _normalize_linkedin_slug(url: str) -> str:
    if not url:
        return ""
    m = re.search(r"linkedin\.com/in/([^/?#]+)", url.lower())
    return (m.group(1).rstrip("/") if m else url.lower().rstrip("/"))


def _text_overlap_score(haystack: str, needle: str) -> int:
    if not needle or not haystack:
        return 0
    h, n = haystack.lower(), needle.lower()
    if n in h:
        return 30
    words = [w for w in re.split(r"[\s,/\-]+", n) if len(w) > 2]
    if not words:
        return 0
    hits = sum(1 for w in words if w in h)
    return int(30 * hits / len(words))


def _search_person_candidates_sync(page, name: str, company: str = "", title: str = "") -> list[dict]:
    """Structured Apollo people search — returns all name-matching rows from the results table."""
    name_parts = _name_parts_for_match(name)
    if not name_parts:
        return []

    urls_to_try: list[str] = []
    if company:
        urls_to_try.append(_build_person_search_url(company, title))
        if title:
            urls_to_try.append(_build_person_search_url(company, ""))
    else:
        urls_to_try.append(APOLLO_PEOPLE)

    all_candidates: list[dict] = []
    seen_keys: set[tuple] = set()

    for url in urls_to_try:
        for page_num in range(1, 6):
            page_url = url if page_num == 1 else f"{url}&page={page_num}"
            page.goto(page_url, wait_until="domcontentloaded", timeout=WEB_TIMEOUT_MS)
            try:
                page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass
            time.sleep(3)
            _close_apollo_modals(page)
            raw = page.evaluate(_extract_people_js()) or []
            if not raw:
                break
            for row in raw:
                rn = (row.get("name") or "").lower()
                if not all(p in rn for p in name_parts):
                    continue
                key = (row.get("name"), row.get("title"), row.get("linkedin_url"))
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                enriched = dict(row)
                enriched["company_hint"] = company or ""
                enriched["search_url"] = page.url
                all_candidates.append(enriched)
            if all_candidates:
                break
        if all_candidates:
            break

    return all_candidates


def _score_person_candidates(
    candidates: list[dict],
    name: str,
    company: str = "",
    title: str = "",
    location: str = "",
    linkedin_url: str = "",
) -> dict:
    """Rank candidates by multi-signal score; return confidence verdict."""
    if not candidates:
        return {"ranked": [], "confidence": "none", "reason": "no candidates"}

    target_li = _normalize_linkedin_slug(linkedin_url)
    name_parts = _name_parts_for_match(name)
    scored: list[dict] = []

    for c in candidates:
        score = 0
        reasons: list[str] = []
        c_li = _normalize_linkedin_slug(c.get("linkedin_url") or "")
        if target_li and c_li and target_li == c_li:
            score = 1000
            reasons.append("linkedin_exact")
        else:
            rn = (c.get("name") or "").lower()
            if name_parts and all(p in rn for p in name_parts):
                score += 40
                reasons.append("name_match")
            blob = " ".join([
                c.get("title") or "",
                c.get("company_hint") or "",
                c.get("name") or "",
            ])
            cs = _text_overlap_score(blob, company)
            if cs:
                score += cs
                reasons.append("company_match")
            ts = _text_overlap_score(blob, title)
            if ts:
                score += ts
                reasons.append("title_match")
            if location:
                ls = _text_overlap_score(blob, location)
                if ls:
                    score += ls
                    reasons.append("location_match")

        scored.append({**c, "match_score": score, "score_reasons": reasons})

    scored.sort(key=lambda x: x["match_score"], reverse=True)

    if target_li and scored and scored[0]["match_score"] >= 1000:
        confidence, reason = "decisive", "linkedin_url exact match"
    elif len(scored) == 1:
        confidence = "clear" if scored[0]["match_score"] >= 50 else "ambiguous"
        reason = "single candidate"
    elif len(scored) >= 2:
        margin = scored[0]["match_score"] - scored[1]["match_score"]
        if margin >= 40:
            confidence, reason = "decisive", f"score margin {margin}"
        elif margin >= 15:
            confidence, reason = "clear", f"score margin {margin}"
        else:
            confidence, reason = "ambiguous", (
                f"close scores: {scored[0]['match_score']} vs {scored[1]['match_score']}"
            )
    else:
        confidence, reason = "none", "no candidates"

    return {"ranked": scored, "confidence": confidence, "reason": reason}


def _llm_pick_best_candidate(target: dict, candidates: list[dict]) -> dict | None:
    """Use configured LLM provider chain to break ties when rule-based scoring is ambiguous."""
    try:
        from mcp_base.llm import llm_chat, parse_json_object
    except ImportError:
        return None

    lines = []
    for i, c in enumerate(candidates[:8]):
        lines.append(
            f"{i}: name={c.get('name')!r} title={c.get('title')!r} "
            f"linkedin={c.get('linkedin_url') or 'n/a'}"
        )
    prompt = (
        "Pick the best Apollo contact match for the target person. "
        "Reply ONLY with JSON:\n"
        '{"best_index": <int or null>, "confidence": "high|medium|low", "reason": "<brief>"}\n\n'
        f"Target: name={target.get('name')!r} company={target.get('company')!r} "
        f"title={target.get('title')!r} location={target.get('location')!r} "
        f"linkedin={target.get('linkedin_url') or 'n/a'}\n\n"
        "Candidates:\n" + "\n".join(lines)
    )
    raw, provider_id = llm_chat([{"role": "user", "content": prompt}], max_tokens=200)
    data = parse_json_object(raw or "")
    if not data:
        return None
    idx = data.get("best_index")
    if idx is None or not isinstance(idx, int) or idx < 0 or idx >= len(candidates):
        return None
    return {
        "best_index": idx,
        "confidence": data.get("confidence", "medium"),
        "reason": data.get("reason", "llm pick"),
        "llm_provider": provider_id,
    }


def _navigate_to_candidate_profile_sync(page, candidate: dict) -> dict:
    """From Apollo search results, click into one candidate's profile page."""
    person_name = candidate.get("name") or ""
    if not person_name:
        return {"ok": False, "error": "candidate has no name"}

    search_url = candidate.get("search_url")
    if search_url and search_url not in page.url:
        page.goto(search_url, wait_until="domcontentloaded", timeout=WEB_TIMEOUT_MS)
        time.sleep(2)
        _close_apollo_modals(page)

    row = _find_row_by_name(page, person_name)
    if row is None:
        return {"ok": False, "error": f"row not found for {person_name}"}

    clicked = False
    li = candidate.get("linkedin_url") or ""
    if li:
        slug = _normalize_linkedin_slug(li)
        try:
            row.locator(f'a[href*="{slug}"]').first.click(timeout=5000)
            clicked = True
        except Exception:
            pass
    if not clicked:
        try:
            first = person_name.split()[0]
            row.get_by_role("link", name=re.compile(re.escape(first), re.I)).first.click(timeout=5000)
            clicked = True
        except Exception:
            try:
                row.click(timeout=3000)
                clicked = True
            except Exception:
                pass

    if not clicked:
        return {"ok": False, "error": "could not click candidate row"}

    time.sleep(3)
    _close_apollo_modals(page)
    if not re.search(r"#/(?:people|contacts)/[A-Za-z0-9]+", page.url):
        return {"ok": False, "error": "click did not navigate to profile", "url": page.url}

    company_hint = candidate.get("company_hint") or ""
    title_hint = candidate.get("title") or ""
    if company_hint and not _profile_matches_hint(page, company_hint, title_hint):
        return {
            "ok": False,
            "error": f"profile does not match company hint '{company_hint}'",
            "url": page.url,
        }
    return {"ok": True, "url": page.url, "matched_text": person_name}


def _open_apollo_profile_by_linkedin_sync(
    page, linkedin_url: str, name: str = "", company_hint: str = "", title_hint: str = "",
) -> dict:
    """Open Apollo profile using linkedin_url for identity only — never paste URL into search (free-plan lookalikes trap)."""
    slug = _normalize_linkedin_slug(linkedin_url)
    if not slug and not name:
        return {"ok": False, "error": "invalid linkedin url and no name"}

    # Free-safe search order: name, name+company only (never slug or linkedin.com URLs — lookalikes trap)
    search_terms: list[str] = []
    if name:
        search_terms.append(_normalize_search_name(name).title())
        if company_hint:
            search_terms.append(f"{_normalize_search_name(name).title()} {company_hint.strip()}")

    lookalikes_hit = False

    def _try_search(search_text: str) -> dict | None:
        nonlocal lookalikes_hit
        if "linkedin.com" in (search_text or "").lower():
            return None
        _goto_clean_people_page(page)
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        if not _fill_people_search_box(page, search_text):
            return None
        time.sleep(2)
        _dismiss_apollo_errors(page)
        if _apollo_lookalikes_blocked(page):
            lookalikes_hit = True
            _recover_from_lookalikes(page)
            return None

        hint_name = _normalize_search_name(name or slug.replace("-", " "))
        try:
            target = page.evaluate(
                _PERSON_RESULT_JS,
                [hint_name, (company_hint or "").lower(), "", True],
            )
        except Exception:
            return None
        if not target:
            return None
        matched = (target.get("text") or "").strip()
        if re.search(r"lookalike|recommendation|free plan", matched, re.I):
            return None

        url_before = page.url
        try:
            page.mouse.click(target["x"], target["y"])
        except Exception:
            return None
        time.sleep(3)
        _dismiss_apollo_errors(page)
        if _apollo_lookalikes_blocked(page):
            lookalikes_hit = True
            _recover_from_lookalikes(page)
            return None
        if page.url == url_before or not re.search(r"#/(?:people|contacts)/[A-Za-z0-9]+", page.url):
            return None
        if company_hint and not _profile_matches_hint(page, company_hint, title_hint):
            return {
                "ok": False,
                "error": f"profile does not match company hint '{company_hint}'",
                "url": page.url,
                "apollo_status": "wrong_person_rejected",
            }
        return {"ok": True, "url": page.url, "matched_text": target.get("text")}

    for term in search_terms:
        nav = _try_search(term)
        if nav and nav.get("ok"):
            return nav

    if name:
        nav = _search_person_profile_url_sync(page, name, company_hint)
        if nav.get("ok"):
            return nav
        if nav.get("apollo_status") == "lookalikes_blocked":
            lookalikes_hit = True

    if lookalikes_hit:
        return {
            "ok": False,
            "error": "Apollo people lookalikes filter blocked (free plan)",
            "apollo_status": "lookalikes_blocked",
            "hint": _LOOKALIKES_BLOCKED_HINT,
        }
    return {"ok": False, "error": "no Apollo result for person (name search)"}


@lru_cache(maxsize=1)
def _email_finder_mod():
    root = Path(__file__).resolve().parents[2]
    path = root / "servers" / "email-finder" / "server.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("email_finder_apollo", path)
    if not spec or not spec.loader:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _academic_domains_from_identity(identity: dict) -> list[str]:
    """Infer academic email domains from title/bio evidence."""
    blob = " ".join([
        str(identity.get("title") or ""),
        str(identity.get("company") or ""),
        " ".join(str(e.get("snippet") or "") for e in (identity.get("evidence") or [])[:5]),
    ]).lower()
    domains: list[str] = []
    if any(m in blob for m in ("mit", "csail", "massachusetts institute")):
        domains.append("mit.edu")
    if "stanford" in blob:
        domains.append("stanford.edu")
    if "harvard" in blob:
        domains.append("harvard.edu")
    if "berkeley" in blob:
        domains.append("berkeley.edu")
    return domains


def _pick_best_pattern_email(name: str, candidates: list[dict], academic: bool = False) -> str | None:
    """Pick best pattern from candidates; for academic emails prefer firstInitial+last (e.g. mlechner)."""
    if not candidates:
        return None
    parts = re.sub(r"[.\s]+", " ", (name or "").strip()).split()
    if not parts:
        return candidates[0].get("email")
    first, last = parts[0].lower(), (parts[-1].lower() if len(parts) > 1 else "")
    fi_last = f"{first[:1]}{last}" if last else ""
    if academic and fi_last:
        for c in candidates:
            em = (c.get("email") or "").lower()
            local = em.split("@")[0] if "@" in em else ""
            if local == fi_last:
                return c.get("email")
    return candidates[0].get("email")


def _resolve_email_fallback_sync(
    name: str, company: str = "", domain: str = "", identity: dict | None = None,
) -> dict:
    """Stage 2: web-published email, academic domains, then pattern guess. Never raises."""
    identity = identity or {}
    dom = (domain or identity.get("domain") or "").strip().lower().lstrip("@")
    ef = _email_finder_mod()
    if not ef:
        return {}

    academic = _academic_domains_from_identity(identity)
    for acad_dom in academic:
        try:
            acad_hits = ef._web_search_emails(name, acad_dom, company, 6)  # noqa: SLF001
        except Exception:
            acad_hits = []
        if acad_hits:
            best = acad_hits[0]
            return {
                "email": best.get("email"),
                "email_source": "web_published",
                "email_confidence": "medium",
                "email_hint": f"Academic email on {acad_dom}: {best.get('source_url')}",
            }

    try:
        web_hits = ef._web_search_emails(name, dom, company, 8)  # noqa: SLF001
    except Exception:
        web_hits = []
    if web_hits:
        best = web_hits[0]
        return {
            "email": best.get("email"),
            "email_source": "web_published",
            "email_confidence": "medium",
            "email_hint": f"Found on web: {best.get('source_url')}",
        }

    # Pattern guess: prefer academic domain when affiliation detected
    if academic:
        try:
            found_acad = ef.find(name=name, company=company, domain=academic[0], scrape=False)
        except Exception:
            found_acad = {}
        if found_acad.get("best"):
            cands = found_acad.get("candidates") or []
            best_em = _pick_best_pattern_email(name, cands, academic=True) or found_acad["best"]
            return {
                "email": best_em,
                "email_source": "pattern_guess",
                "email_confidence": found_acad.get("confidence"),
                "email_hint": f"Pattern guess on academic domain {academic[0]}",
                "candidates": cands,
            }

    # Liquid AI founders often use MIT emails — try mit.edu pattern before company domain
    blob = " ".join([
        str(identity.get("title") or ""),
        " ".join(str(e.get("snippet") or "") for e in (identity.get("evidence") or [])[:5]),
    ]).lower()
    if not academic and ("liquid" in blob or dom == "liquid.ai"):
        try:
            mit_guess = ef.find(name=name, company=company, domain="mit.edu", scrape=False)
        except Exception:
            mit_guess = {}
        if mit_guess.get("best"):
            cands = mit_guess.get("candidates") or []
            best_em = _pick_best_pattern_email(name, cands, academic=True) or mit_guess["best"]
            return {
                "email": best_em,
                "email_source": "pattern_guess",
                "email_confidence": mit_guess.get("confidence"),
                "email_hint": "Pattern guess on mit.edu (common for Liquid AI/MIT affiliates)",
                "candidates": cands,
            }

    try:
        found = ef.find(name=name, company=company, domain=dom, scrape=True)
    except Exception:
        found = {}
    if found.get("best"):
        src = found.get("source") or "pattern"
        label = "web_published" if src not in ("pattern",) else "pattern_guess"
        return {
            "email": found["best"],
            "email_source": label,
            "email_confidence": found.get("confidence"),
            "candidates": found.get("candidates"),
        }
    return {"email": None, "email_source": None}


_APOLLO_EXT_FAB_JS = """() => {
  for (const el of document.querySelectorAll('button, a, div[role="button"], img')) {
    const label = (el.getAttribute('aria-label') || el.title || el.alt || '').toLowerCase();
    if (label.includes('apollo')) {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) {
        return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
      }
    }
  }
  for (const el of document.querySelectorAll('button, div')) {
    const r = el.getBoundingClientRect();
    if (r.left > window.innerWidth * 0.82 && r.top > 80 && r.width >= 28 && r.width <= 90) {
      const html = (el.innerHTML || '').toLowerCase();
      if (html.includes('apollo') || el.querySelector('img')) {
        return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
      }
    }
  }
  return null;
}"""


def _normalize_linkedin_profile_url(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return ""
    if not u.startswith("http"):
        u = "https://" + u.lstrip("/")
    m = re.search(r"(https?://(?:[a-z]+\.)?linkedin\.com/in/[^/?#]+)", u, re.I)
    return (m.group(1).rstrip("/") + "/") if m else u


def _wait_linkedin_profile(page, name: str = "", timeout_ms: int = 15000) -> bool:
    try:
        page.wait_for_url(re.compile(r"linkedin\.com/in/", re.I), timeout=timeout_ms)
        if name:
            first = name.split()[0]
            page.get_by_role(
                "heading", name=re.compile(re.escape(first), re.I),
            ).first.wait_for(timeout=5000)
        return True
    except Exception:
        return False


def _extension_context(page):
    """Frame or page containing the Apollo extension sidebar."""
    for frame in page.frames:
        try:
            if "apollo" in (frame.url or "").lower():
                return frame
        except Exception:
            pass
    for ctx in [page, *page.frames]:
        try:
            if ctx.get_by_text(re.compile(r"Apollo\.io", re.I)).first.is_visible(timeout=800):
                return ctx
        except Exception:
            pass
    return page


def _scrape_extension_email(ctx) -> str | None:
    try:
        text = ctx.inner_text("body")[:8000]
    except Exception:
        text = ""
    m = re.search(
        r"Contact information[\s\S]{0,500}?(" + _EMAIL_RE.pattern + r")",
        text, re.I,
    )
    if m:
        return m.group(1)
    for em in _EMAIL_RE.findall(text):
        low = em.lower()
        if any(x in low for x in ("linkedin.com", "noreply", "example.com")):
            continue
        return em
    return None


def _click_apollo_extension_fab(page) -> bool:
    ctx = _extension_context(page)
    try:
        if ctx.get_by_text(re.compile(r"Contact information", re.I)).first.is_visible(timeout=1000):
            return True
    except Exception:
        pass
    try:
        target = page.evaluate(_APOLLO_EXT_FAB_JS)
        if target:
            page.mouse.click(target["x"], target["y"])
            time.sleep(2)
            return True
    except Exception:
        pass
    for sel in ('[title*="Apollo"]', '[aria-label*="Apollo"]'):
        try:
            page.locator(sel).first.click(timeout=3000)
            time.sleep(2)
            return True
        except Exception:
            pass
    return False


def _click_extension_access_email(ctx) -> bool:
    for getter in (
        lambda: ctx.get_by_role("button", name=re.compile(r"Access email", re.I)),
        lambda: ctx.get_by_text(re.compile(r"^Access email$", re.I)),
        lambda: ctx.get_by_text(re.compile(r"Access email", re.I)),
    ):
        try:
            btn = getter().first
            if btn.is_visible(timeout=2000):
                btn.click(timeout=5000)
                return True
        except Exception:
            pass
    return False


def _apollo_extension_email_on_linkedin(page, linkedin_url: str, name: str = "") -> dict:
    """Path 2: LinkedIn profile + Apollo extension side-panel Access email."""
    li = _normalize_linkedin_profile_url(linkedin_url)
    if not li:
        return {"ok": False, "error": "invalid linkedin url"}
    try:
        page.goto(li, wait_until="domcontentloaded", timeout=WEB_TIMEOUT_MS)
        time.sleep(2)
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "error": f"linkedin navigation failed: {ex}"}
    if not _wait_linkedin_profile(page, name):
        return {"ok": False, "error": "linkedin profile did not load"}

    ctx = page.context
    frame = wait_for_sidebar_frame(ctx, name_hint=name, timeout_s=90)
    if not frame:
        return {
            "ok": False,
            "waiting_for_fab": True,
            "needs_manual_fab_click": True,
            "error": (
                "apollo extension sidebar not open — click the Apollo FAB on the "
                "LinkedIn tab once, then retry"
            ),
            "hint": "Click the yellow Apollo button on the right edge of the LinkedIn profile.",
        }

    email = _scrape_extension_email(frame)
    if not email:
        if _click_extension_access_email(frame):
            time.sleep(4)
            _accept_tos_if_present(page)
            time.sleep(2)
            frame = find_extension_sidebar_frame(ctx) or frame
            email = _scrape_extension_email(frame)

    out: dict = {
        "ok": bool(email),
        "email": email,
        "linkedin_url": li.rstrip("/"),
    }
    if email:
        out["email_source"] = "apollo_extension_linkedin"
    else:
        out["error"] = "email not found in apollo extension panel"
    return out


def _apollo_simple_person_email(page, name: str, company: str = "", title: str = "") -> dict:
    """Path 1: Apollo app name+company quick search → profile → Access Email."""
    nav = _search_person_profile_url_sync(page, name, company, title_hint=title)
    if not nav.get("ok") and company:
        list_hit = _find_person_via_company_list_sync(page, name, company)
        if list_hit.get("ok"):
            reveal = _reveal_email_for_row_sync(page, name)
            out = {
                "ok": bool(reveal.get("email")),
                "profile_url": list_hit.get("apollo_url"),
                "path_detail": "company_table",
            }
            if reveal.get("email"):
                out["email"] = reveal["email"]
                out["email_source"] = "apollo_access_email"
                out["apollo_status"] = "ok"
            elif reveal.get("no_email_on_file"):
                out["apollo_status"] = "not_on_file"
                out["error"] = "Apollo has no email on file for this person"
            else:
                out["apollo_status"] = "reveal_failed"
                out["error"] = reveal.get("error") or "Could not reveal email via Access Email"
            return out

    if nav.get("apollo_status") == "lookalikes_blocked":
        return {
            "ok": False,
            "apollo_status": "lookalikes_blocked",
            "hint": nav.get("hint", _LOOKALIKES_BLOCKED_HINT),
            "error": nav.get("error"),
        }
    on_profile = bool(nav.get("url") and re.search(r"#/(?:people|contacts)/[A-Za-z0-9]+", nav.get("url", "")))
    if not nav.get("ok") and not (on_profile and nav.get("profile_reached")):
        return {
            "ok": False,
            "apollo_status": "not_found",
            "error": nav.get("error", "person not found in apollo"),
        }

    reveal = _reveal_email_on_profile_sync(page)
    out: dict = {
        "ok": bool(reveal.get("email")),
        "profile_url": nav.get("url"),
    }
    if nav.get("profile_reached") and not nav.get("ok"):
        out["company_hint_warning"] = nav.get("error")
    if reveal.get("email"):
        out["email"] = reveal["email"]
        out["email_source"] = "apollo_access_email"
        out["apollo_status"] = "ok"
    elif reveal.get("no_email_on_file"):
        out["apollo_status"] = "not_on_file"
        out["error"] = "Apollo has no email on file for this person"
    else:
        out["apollo_status"] = "reveal_failed"
        out["error"] = reveal.get("error") or "Could not reveal email via Access Email"
    return out


def _find_person_email_flow_sync(
    name: str,
    company: str = "",
    title: str = "",
    linkedin_url: str = "",
    allow_fallback: bool = False,
    identity: dict | None = None,
) -> dict:
    """Apollo app first (fresh tab) → LinkedIn extension side-panel fallback (persistent tab)."""
    identity = identity or {}

    app = _with_background_page(
        lambda page, backend: _apollo_simple_person_email(page, name, company, title),
        reuse_tab=False,
        prefer_fresh_tab=True,
    )
    if isinstance(app, dict) and app.get("email"):
        app["path"] = "apollo_app"
        app.setdefault("identity", identity)
        return app
    if not isinstance(app, dict):
        app = {"ok": False, "apollo_status": "not_found", "error": str(app)}

    from mcp_base.person_identity import confirm_identity_web

    li = (linkedin_url or identity.get("linkedin_url") or "").strip()
    if not li:
        confirmed = confirm_identity_web(name, company)
        li = (confirmed.get("linkedin_url") or "").strip()
        if confirmed.get("title") and not title:
            identity.setdefault("title", confirmed["title"])
        identity["linkedin_confirm"] = confirmed

    if li:
        ext = _with_linkedin_extension_page(
            lambda page, backend: _apollo_extension_email_on_linkedin(page, li, name),
        )
        if isinstance(ext, dict) and ext.get("email"):
            ext["path"] = "linkedin_extension"
            ext["profile_url"] = ext.get("linkedin_url")
            ext["apollo_status"] = "ok"
            ext["identity"] = identity
            return ext
        if isinstance(ext, dict):
            app.setdefault("extension_error", ext.get("error"))
            if ext.get("needs_manual_fab_click"):
                app["needs_manual_fab_click"] = True
                app["waiting_for_fab"] = True

    app["identity"] = identity
    if allow_fallback and not app.get("email"):
        dom = (identity.get("domain") or "").strip()
        fb = _resolve_email_fallback_sync(name, company, dom, identity)
        if fb.get("email"):
            app.update(fb)
            app["path"] = "fallback"
            return app

    if not app.get("apollo_status"):
        app["apollo_status"] = "not_on_file"
    app.setdefault("path", "apollo_app")
    return app


def _resolve_person_sync(
    page,
    name: str,
    company: str = "",
    title: str = "",
    location: str = "",
    linkedin_url: str = "",
    identity: dict | None = None,
) -> dict:
    """Structured search -> score -> LLM tiebreak -> linkedin nav -> reveal email."""
    identity = identity or {}
    company = (company or identity.get("company") or "").strip()
    title = (title or identity.get("title") or "").strip()
    location = (location or identity.get("location") or "").strip()
    linkedin_url = (linkedin_url or identity.get("linkedin_url") or "").strip()

    target = {
        "name": name,
        "company": company,
        "title": title,
        "location": location,
        "linkedin_url": linkedin_url,
    }

    try:
        if _apollo_lookalikes_blocked(page):
            _recover_from_lookalikes(page)
            _goto_clean_people_page(page)
    except Exception:
        pass

    candidates = _search_person_candidates_sync(page, name, company, title)
    match_confidence = "none"
    match_reason = ""
    llm_provider: str | None = identity.get("llm_provider")
    llm_stage: str | None = identity.get("llm_stage")
    picked: dict | None = None
    nav: dict | None = None
    apollo_status = "not_on_file"

    if candidates:
        scoring = _score_person_candidates(
            candidates, name, company, title, location, linkedin_url,
        )
        ranked = scoring["ranked"]
        match_confidence = scoring["confidence"]
        match_reason = scoring["reason"]

        pick_idx = 0
        if scoring["confidence"] == "ambiguous" and len(ranked) > 1:
            llm = _llm_pick_best_candidate(target, ranked[:8])
            if llm and llm.get("best_index") is not None:
                pick_idx = llm["best_index"]
                llm_provider = llm.get("llm_provider")
                llm_stage = "tiebreak"
                prov_tag = f" ({llm_provider})" if llm_provider else ""
                match_reason = f"llm{prov_tag}: {llm.get('reason', '')}"
                if llm.get("confidence") == "high":
                    match_confidence = "clear"

        if ranked:
            picked = ranked[pick_idx]
            nav = _navigate_to_candidate_profile_sync(page, picked)
            if not nav.get("ok"):
                for alt in ranked[1:4]:
                    nav = _navigate_to_candidate_profile_sync(page, alt)
                    if nav.get("ok"):
                        picked = alt
                        match_reason = f"{match_reason}; fallback candidate"
                        break

    if (not nav or not nav.get("ok")) and linkedin_url:
        nav = _open_apollo_profile_by_linkedin_sync(page, linkedin_url, name, company, title)

    if not nav or not nav.get("ok"):
        if company:
            nav = _search_person_profile_url_sync(page, name, company)
        else:
            nav = {"ok": False, "error": "no company hint for safe quick-search"}

    if nav and nav.get("apollo_status") == "wrong_person_rejected":
        return {
            "ok": False,
            "email": None,
            "error": nav.get("error"),
            "apollo_status": "wrong_person_rejected",
            "match_confidence": match_confidence,
            "match_reason": match_reason,
            "llm_provider": llm_provider,
            "llm_stage": llm_stage,
        }

    if nav and nav.get("apollo_status") == "lookalikes_blocked":
        return {
            "ok": False,
            "email": None,
            "error": nav.get("error"),
            "hint": nav.get("hint", _LOOKALIKES_BLOCKED_HINT),
            "apollo_status": "lookalikes_blocked",
            "match_confidence": match_confidence,
            "match_reason": match_reason,
            "llm_provider": llm_provider,
            "llm_stage": llm_stage,
        }

    if not nav or not nav.get("ok"):
        return {
            "ok": False,
            "email": None,
            "error": nav.get("error", "person not found in Apollo") if nav else "person not found",
            "apollo_status": nav.get("apollo_status") if nav else apollo_status,
            "match_confidence": match_confidence,
            "match_reason": match_reason,
            "llm_provider": llm_provider,
            "llm_stage": llm_stage,
        }

    if company and not _profile_matches_hint(page, company, title):
        return {
            "ok": False,
            "email": None,
            "error": f"profile does not match company '{company}'",
            "apollo_status": "wrong_person_rejected",
            "profile_url": nav.get("url"),
            "match_confidence": match_confidence,
            "match_reason": match_reason,
            "llm_provider": llm_provider,
            "llm_stage": llm_stage,
        }

    reveal = _reveal_email_on_profile_sync(page)
    apollo_status = "found" if reveal.get("email") else (
        "not_on_file" if reveal.get("no_email_on_file") else "found"
    )
    reveal["ok"] = True
    reveal["profile_url"] = nav.get("url")
    reveal["matched_text"] = nav.get("matched_text") or (picked or {}).get("name")
    reveal["match_confidence"] = match_confidence
    reveal["match_reason"] = match_reason or "apollo profile opened"
    reveal["llm_provider"] = llm_provider
    reveal["llm_stage"] = llm_stage
    reveal["apollo_status"] = apollo_status
    reveal["candidate"] = picked
    return reveal


def _find_person_via_company_list_sync(page, name: str, company: str) -> dict:
    """Fallback: search company keyword list and match person by name in results table."""
    name_parts = [p.lower() for p in re.sub(r"[.\s]+", " ", name.strip()).split() if len(p) > 1]
    if len(name_parts) >= 3 and len(name_parts[1]) == 1:
        name_parts.pop(1)
    url = (f"{APOLLO_PEOPLE}?qOrganizationKeywordTags[]={quote(company)}"
           "&perPage=25&sortByField=recommendations_score&sortAscending=false")
    page.goto(url, wait_until="domcontentloaded", timeout=WEB_TIMEOUT_MS)
    try:
        page.wait_for_load_state("networkidle", timeout=12000)
    except Exception:
        pass
    time.sleep(3)
    _close_apollo_modals(page)
    raw = page.evaluate(_extract_people_js())
    for row in (raw or []):
        rn = (row.get("name") or "").lower()
        if all(p in rn for p in name_parts):
            return {"ok": True, "person": row, "apollo_url": page.url}
    return {"ok": False, "error": f"{name} not found in Apollo company list for {company}"}


def _find_person_email_sync(
    name: str,
    company: str = "",
    title: str = "",
    location: str = "",
    linkedin_url: str = "",
    identity: dict | None = None,
    allow_fallback: bool = False,
) -> dict:
    """Apollo app search first, then LinkedIn extension fallback."""
    _ = location
    ok_pw, pw_hint = _playwright_available()
    if not ok_pw:
        return {"error": "playwright is not installed", "hint": pw_hint}

    def work() -> dict:
        return _find_person_email_flow_sync(
            name, company, title, linkedin_url,
            allow_fallback=allow_fallback, identity=identity,
        )

    return _run_playwright(work, (WEB_TIMEOUT_MS / 1000) + 120)


def _web_find_ceo_name_sync(company: str, domain: str) -> dict:
    """Best-effort CEO name discovery via keyless DuckDuckGo search — only used when Apollo has
    zero people on file for the domain."""
    query = f'"{company}" CEO'
    try:
        resp = http.request(
            "GET", "https://html.duckduckgo.com/html/",
            params={"q": query}, timeout=10,
            headers={"User-Agent": _BROWSER_UA},
        )
    except Exception as ex:  # noqa: BLE001
        return {"name": None, "error": str(ex)}
    html = resp.get("text") or "" if resp.get("ok") else ""
    if not html:
        return {"name": None, "error": resp.get("error") or "search failed"}
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html, re.S)
    titles = re.findall(r'class="result__a"[^>]*>(.*?)</a>', html, re.S)
    text_blob = " ".join(re.sub("<[^>]+>", "", t) for t in (titles + snippets)[:10])
    m = re.search(
        r"([A-Z][a-zA-Z'\-]+ [A-Z][a-zA-Z'\-]+)(?:,)?\s+(?:is\s+the\s+)?(?:CEO|Chief Executive Officer|Founder)\s+(?:of|at)\s+" + re.escape(company),
        text_blob,
    )
    if not m:
        m = re.search(r"(CEO|Chief Executive Officer|Founder)\s*[:\-]?\s+([A-Z][a-zA-Z'\-]+ [A-Z][a-zA-Z'\-]+)", text_blob)
        name = m.group(2) if m else None
    else:
        name = m.group(1)
    return {"name": name, "query": query, "source": "duckduckgo_web_search" if name else None}


def _run_playwright(work, timeout_s: float) -> dict:
    """Run sync playwright work in a thread; never raises."""
    result: dict = {}
    def target() -> None:
        try:
            result.update(work() or {})
        except Exception as ex:  # noqa: BLE001
            msg = str(ex)
            out: dict = {"error": msg, "source": "web"}
            if "Executable doesn't exist" in msg or "playwright install" in msg.lower():
                out["hint"] = "uv run playwright install chromium"
            elif "profile" in msg.lower() and "lock" in msg.lower():
                out["hint"] = "Close any open browser MCP window, then retry."
            result.update(out)
    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout_s)
    if not result:
        return {"error": "timed out", "source": "web",
                "hint": "Retry or run start_background_browser() to refresh session."}
    return result


def _session_check_sync() -> dict:
    ok_pw, pw_hint = _playwright_available()
    if not ok_pw:
        return {"logged_in": False, "error": "playwright is not installed", "hint": pw_hint}
    if not _cdp_alive() and not PROFILE_DIR.exists():
        return {
            "logged_in": False,
            "cdp_url": _cdp_url(),
            "cdp_alive": False,
            "hint": "Run start_background_browser() — launches minimized Chrome/Edge in background.",
        }

    def work() -> dict:
        def run(page, backend: str) -> dict:
            page.goto(APOLLO_APP, wait_until="domcontentloaded", timeout=WEB_TIMEOUT_MS)
            try:
                page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass
            time.sleep(1)
            url, html = page.url, _page_html(page)
            logged_in = not _is_login_page(url, html)
            return {
                "logged_in": logged_in,
                "url": url,
                "backend": backend,
                "cdp_url": _cdp_url(),
                "cdp_alive": _cdp_alive(),
                "user_data_dir": str(_browser_user_data()),
                "hint": None if logged_in else _profile_login_hint(),
            }
        out = _with_background_page(run)
        if isinstance(out, dict) and out.get("error") == "CDP browser not running":
            out["logged_in"] = False
        return out

    return _run_playwright(work, (WEB_TIMEOUT_MS / 1000) + 20)


def _find_people_web_scrape_sync(domain: str, titles: list[str] | None, limit: int) -> dict:
    """Scrape Apollo people rows (caller must ensure session is ready)."""
    company_url, err = _safe_company_url(domain)
    if err:
        return {"error": err}
    ok_pw, pw_hint = _playwright_available()
    if not ok_pw:
        return {"error": "playwright is not installed", "hint": pw_hint}

    def work() -> dict:
        def run(page, backend: str) -> dict:
            page.goto(company_url, wait_until="domcontentloaded", timeout=WEB_TIMEOUT_MS)
            time.sleep(0.8)
            people: list[dict] = []
            title_filter = [t.lower() for t in (titles or DEFAULT_TITLES)]
            last_url = ""
            for apollo_url in _apollo_people_urls(domain, titles, limit):
                page.goto(apollo_url, wait_until="domcontentloaded", timeout=WEB_TIMEOUT_MS)
                try:
                    page.wait_for_load_state("networkidle", timeout=12000)
                except Exception:
                    pass
                time.sleep(2)
                last_url = page.url
                html = _page_html(page)
                if _is_login_page(last_url, html):
                    return {
                        "error": "apollo not logged in",
                        "hint": _profile_login_hint(),
                        "source": "web",
                        "backend": backend,
                    }
                if apollo_url == APOLLO_PEOPLE:
                    _ui_search_domain(page, domain)
                    time.sleep(2)
                raw = page.evaluate(_extract_people_js())
                for row in (raw or []):
                    name = (row.get("name") or "").strip()
                    if not name or len(name) < 2:
                        continue
                    title = (row.get("title") or "").strip()
                    if title_filter and title:
                        tl = title.lower()
                        if not any(tf in tl for tf in title_filter):
                            continue
                    people.append(_web_person_row(
                        name, title, row.get("linkedin_url") or "", domain))
                if people:
                    break
            people = people[:limit]
            out: dict = {
                "domain": domain,
                "count": len(people),
                "people": people,
                "source": "web",
                "backend": backend,
                "apollo_url": last_url,
                "note": "Scraped from Apollo in background — resolve with email-finder.find(name, domain).",
            }
            if not people:
                out["hint"] = "No rows found — confirm Apollo login via ensure_ready()."
            return out
        out = _with_background_page(run, reuse_tab=True)
        if isinstance(out, dict) and out.get("error") == "CDP browser not running":
            out["source"] = "web"
        return out

    return _run_playwright(work, (WEB_TIMEOUT_MS / 1000) + 45)


def _find_people_web_sync(domain: str, titles: list[str] | None, limit: int,
                          *, skip_ready: bool = False) -> dict:
    if not skip_ready:
        ready = _ensure_ready_sync()
        if not ready.get("ready"):
            return {
                "error": ready.get("error", "apollo not ready"),
                "hint": ready.get("hint", _missing_creds_hint()),
                "source": "web",
                "logged_in": ready.get("logged_in", False),
                "ensure_ready": ready,
            }
    return _find_people_web_scrape_sync(domain, titles, limit)


def _find_people_api(domain: str, titles: list[str] | None, limit: int,
                     seniorities: list[str] | None, locations: list[str] | None,
                     page: int, key: str) -> dict:
    body: dict = {
        "q_organization_domains_list": [domain],
        "person_titles": titles or DEFAULT_TITLES,
        "page": _clamp(page, 1, 50000),
        "per_page": _clamp(limit, 5),
    }
    if seniorities:
        body["person_seniorities"] = seniorities
    if locations:
        body["person_locations"] = locations
    data, err_ = _api("POST", "/mixed_people/api_search", key, json_body=body)
    if err_:
        return err_
    people = [_person_row(p) for p in (data.get("people", []) or [])]
    pg = data.get("pagination", {}) or {}
    return {"domain": domain, "count": len(people), "people": people, "source": "api",
            "pagination": {"page": pg.get("page"), "per_page": pg.get("per_page"),
                           "total_entries": pg.get("total_entries"),
                           "total_pages": pg.get("total_pages")},
            "note": "Emails not included — resolve with email-finder.find(name, domain)."}


# ---------- web-first tools ----------

@mcp.tool
def check_session() -> dict:
    """Check Apollo login via background Chrome/Edge (CDP). Never opens a foreground window."""
    return _session_check_sync()


@mcp.tool
def auto_login_google() -> dict:
    """Sign in to Apollo via Google using APOLLO_GOOGLE_EMAIL/PASSWORD from .env (CDP background tab)."""
    return _auto_login_google_sync()


@mcp.tool
def wait_for_manual_login(wait_seconds: int = 300) -> dict:
    """Wait for Apollo login in the apollo profile browser (restore window, poll session)."""
    return _wait_for_manual_login_sync(wait_seconds)


@mcp.tool
def ensure_ready() -> dict:
    """Start CDP browser, verify session, auto/manual/hybrid login per APOLLO_LOGIN_MODE."""
    return _ensure_ready_sync()


@mcp.tool
def start_background_browser() -> dict:
    """Start Chrome/Edge minimized with CDP debugging. Use ensure_ready() for automatic Google login."""
    return _launch_background_browser(APOLLO_APP)


@mcp.tool
def open_login(wait_seconds: int = 300) -> dict:
    """Alias for ensure_ready() — starts background browser and auto Google login from .env."""
    _ = wait_seconds
    return ensure_ready()


@mcp.tool
def find_people_web(domain: str, titles: list[str] | None = None, limit: int = 5) -> dict:
    """Background Apollo scrape: company site → Apollo search → extract rows (CDP tabs)."""
    domain = (domain or "").strip()
    if not domain:
        return {"error": "domain is required"}
    return _find_people_web_sync(domain, titles, _clamp(limit, 5, 25))


def _find_ceo_email_sync(
    domain: str,
    company: str,
    titles: list[str] | None,
    *,
    skip_extension: bool = False,
) -> dict:
    ready = _ensure_ready_sync()
    if not ready.get("ready"):
        return {
            "error": ready.get("error", "apollo not ready"),
            "hint": ready.get("hint", _missing_creds_hint()),
            "ensure_ready": ready,
        }

    ceo_titles = titles or ["CEO", "Chief Executive Officer", "Founder", "Co-Founder"]
    people = _find_people_web_scrape_sync(domain, ceo_titles, 10)
    rows = people.get("people") or []

    candidate = None
    for p in rows:
        title = (p.get("title") or "").lower()
        if any(k in title for k in ("ceo", "chief executive", "founder")):
            candidate = p
            break
    if not candidate and rows:
        candidate = rows[0]

    name_source = "apollo_people_search"
    if not candidate:
        company_name = company or _company_keyword_from_domain(domain)
        web = _web_find_ceo_name_sync(company_name, domain)
        if web.get("name"):
            candidate = {
                "name": web["name"],
                "title": "CEO (web search, unconfirmed)",
                "linkedin_url": web.get("linkedin_url"),
                "domain": domain,
            }
            name_source = "web_search_fallback"
        else:
            return {
                "domain": domain,
                "candidate": None,
                "email": None,
                "error": "no CEO/founder found via Apollo or web search",
                "apollo_people_result": people,
                "web_search": web,
            }

    reveal = _reveal_apollo_email_sync(domain, candidate["name"], ceo_titles)
    out = {
        "domain": domain,
        "company": company or None,
        "candidate": candidate,
        "name_source": name_source,
        "apollo_reveal": reveal,
        "path": "apollo_app",
    }
    if reveal.get("email"):
        out["email"] = reveal["email"]
        out["email_source"] = "apollo_access_email"
        out["note"] = "Email revealed directly from Apollo's own database via Access Email."
        return out

    if skip_extension:
        out["email"] = None
        out["email_source"] = None
        if reveal.get("no_email_on_file"):
            out["hint"] = "Apollo has no verified email on file for this person."
        else:
            out["hint"] = reveal.get("error") or reveal.get("hint") or "Could not reveal email via Apollo."
        return out

    li = (candidate.get("linkedin_url") or "").strip()
    if not li:
        from mcp_base.person_identity import confirm_identity_web

        confirmed = confirm_identity_web(candidate["name"], company or _company_keyword_from_domain(domain))
        li = (confirmed.get("linkedin_url") or "").strip()

    if li:
        ext = _with_linkedin_extension_page(
            lambda page, backend: _apollo_extension_email_on_linkedin(
                page, li, candidate["name"],
            ),
        )
        if isinstance(ext, dict) and ext.get("email"):
            out["email"] = ext["email"]
            out["email_source"] = ext.get("email_source", "apollo_extension_linkedin")
            out["path"] = "linkedin_extension"
            out["linkedin_url"] = ext.get("linkedin_url")
            out["note"] = "Email revealed from Apollo extension on LinkedIn profile."
            return out
        if isinstance(ext, dict):
            out["extension_error"] = ext.get("error")
            if ext.get("needs_manual_fab_click"):
                out["needs_manual_fab_click"] = True
                out["waiting_for_fab"] = True

    out["email"] = None
    out["email_source"] = None
    if reveal.get("no_email_on_file"):
        out["hint"] = (
            "Apollo has no verified email on file for this person (Access Email returned "
            "'No email'). Extension fallback also missed."
        )
    else:
        out["hint"] = (
            out.get("extension_error")
            or reveal.get("error")
            or reveal.get("hint")
            or "Could not reveal email via Apollo or extension."
        )
    return out


_CONTACTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT, email TEXT, company TEXT, domain TEXT, role TEXT, title TEXT,
  linkedin TEXT, github TEXT, twitter TEXT, phone TEXT, tags TEXT,
  source TEXT, confidence TEXT, notes TEXT, created_at TEXT, updated_at TEXT,
  UNIQUE(email, company)
);
"""


def _persist_contact_row(
    name: str,
    email: str,
    company: str = "",
    domain: str = "",
    linkedin: str = "",
    source: str = "apollo",
) -> int | None:
    if not email:
        return None
    store = BaseStore(db_path("contacts"), schema=_CONTACTS_SCHEMA)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    em = email.strip().lower()
    dom = (domain or "").strip().lower() or (em.split("@", 1)[1] if "@" in em else "")
    return store.execute(
        "INSERT INTO contacts(name,email,company,domain,role,title,linkedin,github,twitter,phone,"
        "tags,source,confidence,notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(email,company) DO UPDATE SET name=excluded.name,domain=excluded.domain,"
        "linkedin=excluded.linkedin,source=excluded.source,updated_at=excluded.updated_at",
        (name, em, company, dom, "CEO", "", linkedin, "", "", "", "",
         source, "high", "", now, now),
    )


def _campaign_contacted(company: str = "", domain: str = "") -> bool:
    from mcp_base.crossdb import open_ro, table_exists

    try:
        conn = open_ro("campaign")
        if not table_exists(conn, "ledger"):
            return False
        dom = (domain or "").strip().lower()
        co = (company or "").strip().lower()
        row = conn.execute(
            "SELECT 1 FROM ledger WHERE LOWER(COALESCE(domain,''))=? OR LOWER(COALESCE(company,''))=? "
            "LIMIT 1",
            (dom, co),
        ).fetchone()
        return bool(row)
    except Exception:
        return False


def _bulk_find_ceo_email_sync(
    companies: list[dict],
    titles: list[str] | None = None,
    max_credits: int = 25,
    skip_extension: bool = False,
    persist_contacts: bool = False,
    skip_contacted: bool = True,
) -> dict:
    if not isinstance(companies, list):
        return {"error": "companies must be a list of {domain, company?}"}
    companies = companies[:100]
    results: list[dict] = []
    stats = {"apollo_app": 0, "extension": 0, "miss": 0, "skipped": 0}
    credits = 0

    for row in companies:
        if not isinstance(row, dict):
            continue
        domain = (row.get("domain") or "").strip().lower().lstrip("@")
        company = (row.get("company") or "").strip()
        if not domain:
            results.append({"domain": domain, "company": company, "error": "domain required", "skipped": True})
            stats["skipped"] += 1
            continue
        if skip_contacted and _campaign_contacted(company, domain):
            results.append({
                "domain": domain, "company": company, "skipped": True,
                "reason": "already_contacted",
            })
            stats["skipped"] += 1
            continue
        if credits >= max_credits:
            results.append({
                "domain": domain, "company": company, "skipped": True,
                "reason": "max_credits_reached",
            })
            stats["skipped"] += 1
            continue

        hit = _find_ceo_email_sync(domain, company, titles, skip_extension=skip_extension)
        credits += 1
        entry: dict = {
            "domain": domain,
            "company": company,
            "name": (hit.get("candidate") or {}).get("name"),
            "email": hit.get("email"),
            "email_source": hit.get("email_source"),
            "path": hit.get("path"),
            "linkedin_url": hit.get("linkedin_url")
            or (hit.get("candidate") or {}).get("linkedin_url"),
            "error": hit.get("error") or hit.get("extension_error"),
            "needs_manual_fab_click": hit.get("needs_manual_fab_click"),
        }
        if hit.get("email"):
            if hit.get("path") == "linkedin_extension":
                stats["extension"] += 1
            else:
                stats["apollo_app"] += 1
            if persist_contacts:
                cid = _persist_contact_row(
                    entry["name"] or "",
                    hit["email"],
                    company,
                    domain,
                    entry.get("linkedin_url") or "",
                    hit.get("email_source") or "apollo",
                )
                entry["contact_id"] = cid
        else:
            stats["miss"] += 1
        results.append(entry)

    found = sum(1 for r in results if r.get("email"))
    return {
        "count": len(results),
        "found": found,
        "results": results,
        "stats": stats,
        "max_credits": max_credits,
        "credits_used": credits,
    }


def _bulk_find_person_email_sync(
    people: list[dict],
    max_credits: int = 25,
    allow_fallback: bool = False,
    persist_contacts: bool = False,
) -> dict:
    if not isinstance(people, list):
        return {"error": "people must be a list of {name, company?, linkedin_url?}"}
    people = people[:100]
    results: list[dict] = []
    stats = {"apollo_app": 0, "extension": 0, "miss": 0, "fallback": 0}
    credits = 0

    for row in people:
        if not isinstance(row, dict):
            continue
        name = (row.get("name") or "").strip()
        if not name:
            results.append({"error": "name required", "skipped": True})
            continue
        if credits >= max_credits:
            results.append({"name": name, "skipped": True, "reason": "max_credits_reached"})
            continue
        company = (row.get("company") or "").strip()
        title = (row.get("title") or "").strip()
        li = (row.get("linkedin_url") or "").strip()
        domain = (row.get("domain") or "").strip()
        hit = _find_person_email_flow_sync(
            name, company, title, li, allow_fallback=allow_fallback,
        )
        credits += 1
        entry = {
            "name": name,
            "company": company or None,
            "email": hit.get("email"),
            "email_source": hit.get("email_source"),
            "path": hit.get("path"),
            "linkedin_url": hit.get("linkedin_url") or li or None,
            "apollo_status": hit.get("apollo_status"),
            "error": hit.get("error") or hit.get("extension_error"),
            "needs_manual_fab_click": hit.get("needs_manual_fab_click"),
        }
        if hit.get("email"):
            p = hit.get("path") or "apollo_app"
            if p == "linkedin_extension":
                stats["extension"] += 1
            elif p == "fallback":
                stats["fallback"] += 1
            else:
                stats["apollo_app"] += 1
            if persist_contacts:
                cid = _persist_contact_row(
                    name, hit["email"], company, domain, entry.get("linkedin_url") or "",
                    hit.get("email_source") or "apollo",
                )
                entry["contact_id"] = cid
        else:
            stats["miss"] += 1
        results.append(entry)

    found = sum(1 for r in results if r.get("email"))
    return {
        "count": len(results),
        "found": found,
        "results": results,
        "stats": stats,
        "max_credits": max_credits,
        "credits_used": credits,
    }


@mcp.tool
def discover_person_identity(
    name: str,
    company: str = "",
    domain: str = "",
    title: str = "",
    linkedin_url: str = "",
) -> dict:
    """Web-only identity discovery (no Apollo credits). Multi-engine search + LLM resolver."""
    from mcp_base.person_identity import enrich_person_identity, guess_domain_from_company

    name = (name or "").strip()
    if not name:
        return {"error": "name is required"}
    dom = (domain or "").strip().lower().lstrip("@") or guess_domain_from_company(company)
    return enrich_person_identity(name, company, dom, title, linkedin_url)


@mcp.tool
def find_person_email(
    name: str,
    company: str = "",
    domain: str = "",
    title: str = "",
    linkedin_url: str = "",
    allow_fallback: bool = False,
) -> dict:
    """Apollo-first person email: app name+company search -> Access Email; if miss, LinkedIn
    profile + Apollo extension sidebar. Never pastes LinkedIn URLs into Apollo search.
    Set allow_fallback=True for pattern/web guess when Apollo has no email."""
    from mcp_base.person_identity import guess_domain_from_company

    name = (name or "").strip()
    if not name:
        return {"error": "name is required"}
    ready = _ensure_ready_sync()
    if not ready.get("ready"):
        return {
            "error": ready.get("error", "apollo not ready"),
            "hint": ready.get("hint", _missing_creds_hint()),
            "ensure_ready": ready,
        }

    dom = (domain or "").strip().lower().lstrip("@") or guess_domain_from_company(company)
    co = (company or "").strip()
    ti = (title or "").strip()
    li = (linkedin_url or "").strip()
    identity: dict = {
        "name": name,
        "company": co or None,
        "title": ti or None,
        "linkedin_url": li.rstrip("/") if li else None,
        "domain": dom or None,
        "source": "user" if li else None,
    }

    apollo = _find_person_email_sync(
        name, co, ti, "", li, identity=identity, allow_fallback=allow_fallback,
    )

    flow_identity = apollo.pop("identity", None) or identity
    out: dict = {
        "name": name,
        "company_hint": co or None,
        "domain": dom,
        "title_hint": ti or flow_identity.get("title") or None,
        "identity": flow_identity,
        "path": apollo.get("path"),
        "apollo_status": apollo.get("apollo_status", "not_on_file"),
        "profile_url": apollo.get("profile_url"),
        "linkedin_url": apollo.get("linkedin_url") or flow_identity.get("linkedin_url"),
        "backend": apollo.get("backend"),
    }

    if apollo.get("email"):
        out["email"] = apollo["email"]
        out["email_source"] = apollo.get("email_source", "apollo_access_email")
        if out["email_source"] == "apollo_access_email":
            out["note"] = "Email revealed from Apollo app via Access Email."
        elif out["email_source"] == "apollo_extension_linkedin":
            out["note"] = "Email revealed from Apollo extension on LinkedIn profile."
        return out

    if apollo.get("apollo_status") == "lookalikes_blocked":
        out["hint"] = apollo.get("hint") or _LOOKALIKES_BLOCKED_HINT
    elif apollo.get("extension_error"):
        out["hint"] = (
            f"Apollo app search did not return email; extension fallback: "
            f"{apollo.get('extension_error')}"
        )
    else:
        out["hint"] = apollo.get("error") or "No email found via Apollo app or LinkedIn extension."

    out["email"] = None
    out["email_source"] = apollo.get("email_source")
    if allow_fallback and apollo.get("email_source") in ("pattern_guess", "web_published"):
        out["email"] = apollo.get("email")
        out["email_hint"] = apollo.get("email_hint")
        out["candidates"] = apollo.get("candidates")
    return out


@mcp.tool
def find_ceo_email(domain: str, company: str = "", titles: list[str] | None = None) -> dict:
    """CEO email: Path 1 Apollo app Access Email (fast); Path 2 extension only if Path 1 misses.
    Spends at most 1 Apollo credit. Never returns a guessed pattern."""
    domain = (domain or "").strip()
    if not domain:
        return {"error": "domain is required"}
    return _find_ceo_email_sync(domain, (company or "").strip(), titles)


@mcp.tool
def bulk_find_ceo_email(
    companies: list[dict],
    titles: list[str] | None = None,
    max_credits: int = 25,
    skip_extension: bool = False,
    persist_contacts: bool = False,
    skip_contacted: bool = True,
) -> dict:
    """Batch CEO emails for outreach. Each item: {domain, company?}. Path 1 first; extension only on miss.
    skip_extension=True for fast bulk (Apollo app only). persist_contacts writes hits to contacts CRM."""
    ready = _ensure_ready_sync()
    if not ready.get("ready"):
        return {
            "error": ready.get("error", "apollo not ready"),
            "hint": ready.get("hint", _missing_creds_hint()),
            "ensure_ready": ready,
        }
    return _bulk_find_ceo_email_sync(
        companies, titles, max_credits, skip_extension, persist_contacts, skip_contacted,
    )


@mcp.tool
def bulk_find_person_email(
    people: list[dict],
    max_credits: int = 25,
    allow_fallback: bool = False,
    persist_contacts: bool = False,
) -> dict:
    """Batch person emails. Each item: {name, company?, linkedin_url?, domain?, title?}.
    Path 1 Apollo app first; extension only on miss per person."""
    ready = _ensure_ready_sync()
    if not ready.get("ready"):
        return {
            "error": ready.get("error", "apollo not ready"),
            "hint": ready.get("hint", _missing_creds_hint()),
            "ensure_ready": ready,
        }
    return _bulk_find_person_email_sync(people, max_credits, allow_fallback, persist_contacts)


@mcp.tool
def find_people(domain: str, titles: list[str] | None = None, limit: int = 5,
                seniorities: list[str] | None = None, locations: list[str] | None = None,
                page: int = 1) -> dict:
    """Find people at a company domain. Web mode (default): headless company site + Apollo UI.
    API mode (APOLLO_MODE=api + APOLLO_API_KEY): REST search. No emails — use email-finder.find()."""
    domain = (domain or "").strip()
    if not domain:
        return {"error": "domain is required"}
    limit = _clamp(limit, 5)
    if _web_only():
        return _find_people_web_sync(domain, titles, limit)
    key = get_env("APOLLO_API_KEY")
    if not key:
        return _find_people_web_sync(domain, titles, limit)
    api_result = _find_people_api(domain, titles, limit, seniorities, locations, page, key)
    if "error" not in api_result:
        return api_result
    web_result = _find_people_web_sync(domain, titles, limit)
    if web_result.get("people"):
        web_result["api_error"] = api_result.get("error")
        return web_result
    return api_result if api_result.get("error") else web_result


@mcp.tool
def find_people_paged(domain: str, titles: list[str] | None = None, per_page: int = 10,
                      max_pages: int = 3, seniorities: list[str] | None = None) -> dict:
    """Auto-paginate find_people (web mode returns one page; API mode paginates)."""
    domain = (domain or "").strip()
    if not domain:
        return {"error": "domain is required"}
    if _web_only():
        return find_people(domain, titles, per_page, seniorities, None, 1)
    per_page = _clamp(per_page, 10)
    max_pages = _clamp(max_pages, 3, 100)
    seen: set[tuple] = set()
    people: list[dict] = []
    page = 1
    for page in range(1, max_pages + 1):
        res = find_people(domain, titles, per_page, seniorities, None, page)
        if "error" in res and not res.get("people"):
            return res if not people else {"domain": domain, "count": len(people),
                                           "people": people, "partial_error": res["error"]}
        for p in res.get("people", []):
            k = (p.get("name"), p.get("title"))
            if k not in seen:
                seen.add(k)
                people.append(p)
        total_pages = res.get("pagination", {}).get("total_pages")
        if total_pages and page >= total_pages:
            break
        if not res.get("people") or res.get("source") == "web":
            break
    return {"domain": domain, "count": len(people), "people": people, "pages_fetched": page}


@mcp.tool
def find_company(domain: str = "", name: str = "") -> dict:
    """Look up an organization by domain or name (requires APOLLO_MODE=api + API key)."""
    if _web_only():
        return {"error": "find_company requires APOLLO_MODE=api and APOLLO_API_KEY",
                "hint": "Use find_people(domain) in web mode instead."}
    domain = (domain or "").strip()
    name = (name or "").strip()
    if not domain and not name:
        return {"error": "provide a domain or name"}
    key = get_env("APOLLO_API_KEY")
    if not key:
        return {"error": "no APOLLO_API_KEY", "hint": "Set APOLLO_MODE=api and APOLLO_API_KEY in .env"}
    body = {"q_organization_name": name} if name else {"q_organization_domains_list": [domain]}
    data, err_ = _api("POST", "/mixed_companies/search", key, json_body=body)
    if err_:
        return err_
    orgs = [{
        "name": o.get("name"), "domain": o.get("primary_domain"),
        "industry": o.get("industry"), "employees": o.get("estimated_num_employees"),
        "founded_year": o.get("founded_year"),
        "linkedin_url": o.get("linkedin_url"),
        "keywords": (o.get("keywords") or [])[:8],
        "total_funding": o.get("total_funding_printed") or o.get("total_funding"),
        "latest_funding": o.get("latest_funding_stage"),
    } for o in (data.get("organizations", []) or [])[:5]]
    return {"organizations": orgs}


@mcp.tool
def enrich_org(domain: str) -> dict:
    """Enrich organization (requires APOLLO_MODE=api + API key)."""
    if _web_only():
        return {"error": "enrich_org requires APOLLO_MODE=api and APOLLO_API_KEY",
                "hint": "Use find_people(domain) in web mode."}
    domain = (domain or "").strip()
    if not domain:
        return {"error": "domain is required"}
    key = get_env("APOLLO_API_KEY")
    if not key:
        return {"error": "no APOLLO_API_KEY"}
    data, err_ = _api("GET", "/organizations/enrich", key, params={"domain": domain})
    if err_:
        return err_
    o = (data or {}).get("organization", {}) or {}
    if not o:
        return {"domain": domain, "found": False}
    return {"domain": domain, "found": True, "organization": {
        "name": o.get("name"), "website": o.get("website_url"),
        "industry": o.get("industry"), "employees": o.get("estimated_num_employees"),
        "founded_year": o.get("founded_year"),
        "location": ", ".join(x for x in (o.get("city"), o.get("state"), o.get("country")) if x),
        "linkedin_url": o.get("linkedin_url"), "twitter_url": o.get("twitter_url"),
        "total_funding": o.get("total_funding_printed") or o.get("total_funding"),
        "latest_funding": o.get("latest_funding_stage"),
        "keywords": (o.get("keywords") or [])[:12],
        "description": (o.get("short_description") or "")[:400],
    }}


@mcp.tool
def enrich_person(name: str = "", domain: str = "", linkedin_url: str = "") -> dict:
    """Match a person (requires APOLLO_MODE=api + API key)."""
    if _web_only():
        return {"error": "enrich_person requires APOLLO_MODE=api and APOLLO_API_KEY",
                "hint": "Use find_people(domain) + email-finder.find() in web mode."}
    key = get_env("APOLLO_API_KEY")
    if not key:
        return {"error": "no APOLLO_API_KEY"}
    body: dict = {}
    if name:
        body["name"] = name
    if domain:
        body["domain"] = domain
    if linkedin_url:
        body["linkedin_url"] = linkedin_url
    if not body:
        return {"error": "provide name+domain or linkedin_url"}
    data, err_ = _api("POST", "/people/match", key, json_body=body)
    if err_:
        return err_
    p = (data or {}).get("person", {}) or {}
    if not p:
        return {"found": False}
    row = _person_row(p)
    row["found"] = True
    return row


@mcp.tool
def seniorities() -> dict:
    return {"seniorities": SENIORITIES}


@mcp.tool
def titles_catalog() -> dict:
    return {"presets": TITLE_PRESETS}


@mcp.tool
def has_key() -> dict:
    return {"configured": bool(get_env("APOLLO_API_KEY")), "mode": _mode()}


@mcp.tool
def has_browser() -> dict:
    ok_pw, hint = _playwright_available()
    cdp = _cdp_alive()
    session = _session_check_sync() if ok_pw and (cdp or PROFILE_DIR.exists()) else {}
    return {
        "mode": _mode(),
        "backend": "cdp" if cdp else "headless",
        "cdp_url": _cdp_url(),
        "cdp_alive": cdp,
        "playwright_installed": ok_pw,
        "user_data_dir": str(_browser_user_data()),
        "logged_in": session.get("logged_in"),
        "hint": hint if not ok_pw else session.get("hint") or (
            None if session.get("logged_in") else
            "Run start_background_browser() — minimized Chrome/Edge in background."
        ),
    }


if __name__ == "__main__":
    mcp.run()
